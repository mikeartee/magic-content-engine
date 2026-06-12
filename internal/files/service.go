// Package files backs the run-bundle file API of the Bullpen Console: listing
// runs, reading a file, saving a file atomically, and resolving a download
// path. Every path the service touches is confined to output/<run_id>/ by a
// path-traversal guard, so no request can escape the run directory.
//
// This implements Requirement 4 of the bullpen-console-go spec. It reproduces
// the Flask Console's _safe_file_path / /api/runs behaviour in Go and carries
// no AWS dependency.
package files

import (
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// excludedFiles are never exposed through the file API: the live event log and
// the pipeline checkpoint store are internal plumbing, not reviewable output
// (Requirement 4.2).
var excludedFiles = map[string]struct{}{
	"agent-log.jsonl":  {},
	"checkpoints.json": {},
}

// Sentinel errors. The HTTP layer maps these to status codes: ErrForbidden ->
// 403 forbidden (Requirement 4.1) and ErrNotFound -> 404.
var (
	// ErrForbidden indicates a name that would resolve outside output/<run_id>/.
	ErrForbidden = errors.New("files: path traversal detected")
	// ErrNotFound indicates the resolved path is not an existing file.
	ErrNotFound = errors.New("files: file not found")
)

// RunListing is one run directory and the reviewable files it contains. Subdir
// entries are stored as "subdir/filename" so they round-trip through the file
// API (Requirement 4.2).
type RunListing struct {
	ID    string   `json:"id"`
	Files []string `json:"files"`
}

// Service resolves and serves run-bundle files under a single output root (the
// parent of every output/<run_id>/ directory). It is safe for concurrent use:
// it holds no mutable state.
type Service struct {
	outputDir string
}

// New constructs a Service rooted at outputDir.
func New(outputDir string) *Service {
	return &Service{outputDir: outputDir}
}

// ListRuns walks the output directory one level deep. For each run directory it
// collects files directly inside it plus one level of subdirectory entries
// (stored as "subdir/filename"), excluding agent-log.jsonl and checkpoints.json.
// Runs are ordered by id descending. A missing output directory yields an empty
// list and no error (Requirement 4.2, Requirement 5.1).
func (s *Service) ListRuns() ([]RunListing, error) {
	entries, err := os.ReadDir(s.outputDir)
	if err != nil {
		if os.IsNotExist(err) {
			return []RunListing{}, nil
		}
		return nil, err
	}

	// os.ReadDir returns entries sorted by name ascending; reverse for descending.
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() > entries[j].Name() })

	runs := make([]RunListing, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		runDir := filepath.Join(s.outputDir, entry.Name())
		files, err := s.collectRunFiles(runDir)
		if err != nil {
			return nil, err
		}
		sort.Strings(files)
		runs = append(runs, RunListing{ID: entry.Name(), Files: files})
	}
	return runs, nil
}

// collectRunFiles gathers the reviewable files for a single run directory: the
// files directly inside it plus one level of subdirectory entries as
// "subdir/filename".
func (s *Service) collectRunFiles(runDir string) ([]string, error) {
	items, err := os.ReadDir(runDir)
	if err != nil {
		return nil, err
	}
	var files []string
	for _, item := range items {
		if item.IsDir() {
			subItems, err := os.ReadDir(filepath.Join(runDir, item.Name()))
			if err != nil {
				return nil, err
			}
			for _, sub := range subItems {
				if sub.IsDir() || excluded(sub.Name()) {
					continue
				}
				// Forward slash so the entry round-trips through the file API
				// regardless of host separator (matches the Flask Console).
				files = append(files, item.Name()+"/"+sub.Name())
			}
			continue
		}
		if !excluded(item.Name()) {
			files = append(files, item.Name())
		}
	}
	return files, nil
}

// ReadFile returns the content of name within output/<run_id>/. name may include
// a single subdirectory segment (for example "subdir/file.md"). It returns
// ErrForbidden for a traversal name and ErrNotFound when no such file exists
// (Requirement 4.1, Requirement 4.3).
func (s *Service) ReadFile(runID, name string) ([]byte, error) {
	path, err := s.safePath(runID, name)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return nil, ErrNotFound
	}
	return os.ReadFile(path)
}

// SaveFile writes content to name within output/<run_id>/ atomically: it writes
// a temporary sibling file and renames it over the target, so a reader never
// observes a partially written file. Parent subdirectories are created as
// needed. It returns ErrForbidden for a traversal name (Requirement 4.1,
// Requirement 4.4).
func (s *Service) SaveFile(runID, name string, content []byte) error {
	path, err := s.safePath(runID, name)
	if err != nil {
		return err
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}

	tmp, err := os.CreateTemp(dir, ".tmp_*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	// On any failure past this point, remove the temp file so no residue leaks.
	cleanup := func() { _ = os.Remove(tmpName) }

	if _, err := tmp.Write(content); err != nil {
		_ = tmp.Close()
		cleanup()
		return err
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		cleanup()
		return err
	}
	return nil
}

// ResolveDownload returns the on-disk path of name within output/<run_id>/ so
// the caller can serve it as an attachment. It returns ErrForbidden for a
// traversal name and ErrNotFound when no such file exists (Requirement 4.1,
// Requirement 4.5).
func (s *Service) ResolveDownload(runID, name string) (string, error) {
	path, err := s.safePath(runID, name)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return "", ErrNotFound
	}
	return path, nil
}

// safePath resolves name against output/<run_id>/ and confirms the result stays
// inside that directory, reproducing the Flask _safe_file_path guard. Any name
// that would escape — via "..", an absolute path, or mixed separators — is
// rejected with ErrForbidden (Requirement 4.1).
func (s *Service) safePath(runID, name string) (string, error) {
	base, err := filepath.Abs(filepath.Join(s.outputDir, runID))
	if err != nil {
		return "", ErrForbidden
	}
	base = filepath.Clean(base)

	// filepath.Join cleans the combined path, collapsing ".." segments and
	// treating an absolute name as a component appended under base.
	candidate, err := filepath.Abs(filepath.Join(base, name))
	if err != nil {
		return "", ErrForbidden
	}
	candidate = filepath.Clean(candidate)

	rel, err := filepath.Rel(base, candidate)
	if err != nil {
		return "", ErrForbidden
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", ErrForbidden
	}
	return candidate, nil
}

// excluded reports whether a base filename is one of the never-exposed files.
func excluded(name string) bool {
	_, ok := excludedFiles[name]
	return ok
}
