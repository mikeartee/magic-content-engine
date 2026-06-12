package files_test

import (
	"errors"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/mikeartee/magic-content-engine/console/internal/files"
)

// writeFile is a test helper that creates a file (and parents) with content.
func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// newServiceWithOutput builds a Service rooted at a temp output directory and
// returns the service plus the output root path.
func newServiceWithOutput(t *testing.T) (*files.Service, string) {
	t.Helper()
	root := t.TempDir()
	output := filepath.Join(root, "output")
	if err := os.MkdirAll(output, 0o755); err != nil {
		t.Fatalf("mkdir output: %v", err)
	}
	return files.New(output), output
}

// Requirement 4.2: ListRuns walks output/ one level deep, includes files
// directly in the run dir plus one level of subdir entries as subdir/filename,
// and excludes agent-log.jsonl and checkpoints.json.
func TestListRunsOneLevelDeepWithExclusions(t *testing.T) {
	svc, output := newServiceWithOutput(t)

	// Run "aaa11111": a direct file, the two excluded files, and a subdir file.
	writeFile(t, filepath.Join(output, "aaa11111", "post.md"), "hello")
	writeFile(t, filepath.Join(output, "aaa11111", "agent-log.jsonl"), "{}")
	writeFile(t, filepath.Join(output, "aaa11111", "checkpoints.json"), "{}")
	writeFile(t, filepath.Join(output, "aaa11111", "2026-01-01-slug", "post.md"), "nested")
	writeFile(t, filepath.Join(output, "aaa11111", "2026-01-01-slug", "checkpoints.json"), "{}")

	// Run "bbb22222": just one file.
	writeFile(t, filepath.Join(output, "bbb22222", "summary.txt"), "world")

	runs, err := svc.ListRuns()
	if err != nil {
		t.Fatalf("ListRuns: %v", err)
	}

	got := map[string][]string{}
	for _, r := range runs {
		got[r.ID] = r.Files
	}

	wantAAA := []string{"2026-01-01-slug/post.md", "post.md"}
	sort.Strings(wantAAA)
	if !equalStrings(got["aaa11111"], wantAAA) {
		t.Errorf("aaa11111 files = %v, want %v", got["aaa11111"], wantAAA)
	}
	if !equalStrings(got["bbb22222"], []string{"summary.txt"}) {
		t.Errorf("bbb22222 files = %v, want [summary.txt]", got["bbb22222"])
	}

	// The excluded files must never appear, even inside a subdirectory.
	for _, r := range runs {
		for _, f := range r.Files {
			if base := filepath.Base(f); base == "agent-log.jsonl" || base == "checkpoints.json" {
				t.Errorf("run %s exposed excluded file %q", r.ID, f)
			}
		}
	}
}

// Requirement 4.2: run directories are listed by name descending and the
// listing tolerates a missing output directory (Requirement 5.1).
func TestListRunsOrderingAndMissingDir(t *testing.T) {
	svc, output := newServiceWithOutput(t)
	writeFile(t, filepath.Join(output, "aaa", "f.md"), "a")
	writeFile(t, filepath.Join(output, "ccc", "f.md"), "c")
	writeFile(t, filepath.Join(output, "bbb", "f.md"), "b")

	runs, err := svc.ListRuns()
	if err != nil {
		t.Fatalf("ListRuns: %v", err)
	}
	ids := []string{}
	for _, r := range runs {
		ids = append(ids, r.ID)
	}
	want := []string{"ccc", "bbb", "aaa"}
	if !equalStrings(ids, want) {
		t.Errorf("run order = %v, want %v (name descending)", ids, want)
	}

	// A service pointed at a non-existent output dir lists no runs, no error.
	missing := files.New(filepath.Join(t.TempDir(), "does-not-exist"))
	runs, err = missing.ListRuns()
	if err != nil {
		t.Fatalf("ListRuns(missing) error = %v, want nil", err)
	}
	if len(runs) != 0 {
		t.Errorf("ListRuns(missing) = %v, want empty", runs)
	}
}

// Requirement 4.3: ReadFile returns content including one subdir segment.
func TestReadFileIncludingSubdir(t *testing.T) {
	svc, output := newServiceWithOutput(t)
	writeFile(t, filepath.Join(output, "run1", "post.md"), "top-level")
	writeFile(t, filepath.Join(output, "run1", "2026-01-01-slug", "post.md"), "nested-body")

	top, err := svc.ReadFile("run1", "post.md")
	if err != nil {
		t.Fatalf("ReadFile top: %v", err)
	}
	if string(top) != "top-level" {
		t.Errorf("top content = %q, want top-level", top)
	}

	nested, err := svc.ReadFile("run1", "2026-01-01-slug/post.md")
	if err != nil {
		t.Fatalf("ReadFile nested: %v", err)
	}
	if string(nested) != "nested-body" {
		t.Errorf("nested content = %q, want nested-body", nested)
	}
}

// Requirement 4.1: ReadFile of a missing file reports ErrNotFound, and a
// traversal name reports ErrForbidden.
func TestReadFileNotFoundAndForbidden(t *testing.T) {
	svc, output := newServiceWithOutput(t)
	writeFile(t, filepath.Join(output, "run1", "post.md"), "x")

	if _, err := svc.ReadFile("run1", "missing.md"); !errors.Is(err, files.ErrNotFound) {
		t.Errorf("ReadFile(missing) err = %v, want ErrNotFound", err)
	}
	if _, err := svc.ReadFile("run1", "../../etc/passwd"); !errors.Is(err, files.ErrForbidden) {
		t.Errorf("ReadFile(traversal) err = %v, want ErrForbidden", err)
	}
}

// Requirement 4.4: SaveFile writes atomically (round-trips) and creates parent
// subdirectories, leaving no .tmp residue behind.
func TestSaveFileAtomicRoundTrip(t *testing.T) {
	svc, output := newServiceWithOutput(t)
	if err := os.MkdirAll(filepath.Join(output, "run1"), 0o755); err != nil {
		t.Fatal(err)
	}

	if err := svc.SaveFile("run1", "post.md", []byte("v1")); err != nil {
		t.Fatalf("SaveFile v1: %v", err)
	}
	// Overwrite to exercise rename-over-existing.
	if err := svc.SaveFile("run1", "post.md", []byte("v2")); err != nil {
		t.Fatalf("SaveFile v2: %v", err)
	}
	got, err := svc.ReadFile("run1", "post.md")
	if err != nil {
		t.Fatalf("ReadFile after save: %v", err)
	}
	if string(got) != "v2" {
		t.Errorf("content = %q, want v2", got)
	}

	// SaveFile into a not-yet-existing subdirectory must create it.
	if err := svc.SaveFile("run1", "sub/dir/note.md", []byte("deep")); err != nil {
		t.Fatalf("SaveFile nested: %v", err)
	}
	got, err = svc.ReadFile("run1", "sub/dir/note.md")
	if err != nil {
		t.Fatalf("ReadFile nested: %v", err)
	}
	if string(got) != "deep" {
		t.Errorf("nested content = %q, want deep", got)
	}

	// No temporary files should remain in the run directory.
	entries, err := os.ReadDir(filepath.Join(output, "run1"))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if len(e.Name()) >= 4 && e.Name()[:4] == ".tmp" {
			t.Errorf("leftover temp file: %s", e.Name())
		}
	}
}

// Requirement 4.1: SaveFile rejects a traversal name with ErrForbidden and
// writes nothing.
func TestSaveFileForbidden(t *testing.T) {
	svc, output := newServiceWithOutput(t)
	if err := os.MkdirAll(filepath.Join(output, "run1"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := svc.SaveFile("run1", "../escape.md", []byte("nope")); !errors.Is(err, files.ErrForbidden) {
		t.Errorf("SaveFile(traversal) err = %v, want ErrForbidden", err)
	}
	if _, err := os.Stat(filepath.Join(output, "escape.md")); !os.IsNotExist(err) {
		t.Errorf("traversal save created a file outside the run dir")
	}
}

// Requirement 4.5: ResolveDownload returns the on-disk path for a valid file,
// ErrNotFound for a missing file, and ErrForbidden for a traversal name.
func TestResolveDownload(t *testing.T) {
	svc, output := newServiceWithOutput(t)
	writeFile(t, filepath.Join(output, "run1", "post.md"), "body")

	path, err := svc.ResolveDownload("run1", "post.md")
	if err != nil {
		t.Fatalf("ResolveDownload: %v", err)
	}
	if filepath.Base(path) != "post.md" {
		t.Errorf("resolved path = %q, want basename post.md", path)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "body" {
		t.Errorf("resolved path content = %q (err %v), want body", data, err)
	}

	if _, err := svc.ResolveDownload("run1", "nope.md"); !errors.Is(err, files.ErrNotFound) {
		t.Errorf("ResolveDownload(missing) err = %v, want ErrNotFound", err)
	}
	if _, err := svc.ResolveDownload("run1", "..\\..\\secret"); !errors.Is(err, files.ErrForbidden) {
		t.Errorf("ResolveDownload(traversal) err = %v, want ErrForbidden", err)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
