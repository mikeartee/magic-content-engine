// Package vault backs the Bullpen Console's topic suggestions. It offers two
// vault-only capabilities: a recency list of the most-recently-modified notes
// from 06-permanent/ and 00-inbox/, and a title search across every *.md note
// under the vault. Suggestions only pre-fill the free-text topic field; the
// topic itself is never constrained.
//
// This implements Requirement 6 of the bullpen-console-go spec. It reproduces
// the Flask Console's vault suggestion behaviour in Go and, by design, carries
// no AWS dependency: the DynamoDB fallback of the legacy Console is dropped, so
// no suggestion request ever touches AWS (Requirement 6.1).
package vault

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// defaultVaultPath is the fallback vault root when VAULT_PATH is unset. It
// matches the legacy Flask Console default so existing setups keep working.
const defaultVaultPath = `C:\Users\Mike RT\Documents\second-brain`

// defaultLimit caps a recency list or search result when the caller does not
// supply a positive limit. It matches the legacy Console's cap of 10.
const defaultLimit = 10

// minTopicLen mirrors the legacy Console: topics shorter than this are noise
// (stray fragments, single words) and are skipped.
const minTopicLen = 5

// recencyDirs are the vault subdirectories the recency list draws from, in the
// order they are scanned. Ordering across the combined set is by mtime, so this
// slice only governs which directories contribute (Requirement 6.2).
var recencyDirs = []string{"06-permanent", "00-inbox"}

// leadingID strips a leading Zettelkasten-style numeric ID (8+ digits followed
// by whitespace) from a permanent-note filename, e.g.
// "202604050001 AgentCore" -> "AgentCore".
var leadingID = regexp.MustCompile(`^\d{8,}\s+`)

// Suggestion is a single topic suggestion derived from a vault note.
type Suggestion struct {
	Topic       string `json:"topic"`
	LastCovered string `json:"last_covered"` // ISO date (YYYY-MM-DD) from mtime
	DaysSince   int    `json:"days_since"`
	Source      string `json:"source"` // path relative to the vault root
}

// Suggestions is the result of a recency or search request: the matched items
// plus an optional warning. A missing vault yields an empty Items slice and a
// non-empty Warning rather than an error (Requirement 6.5).
type Suggestions struct {
	Items   []Suggestion `json:"suggestions"`
	Warning string       `json:"warning,omitempty"`
}

// Service produces vault-only topic suggestions. It holds no cached vault path:
// the root is resolved from VAULT_PATH on every call (Requirement 6.6), so it
// is safe for concurrent use.
type Service struct{}

// New constructs a Service. The vault root is read from VAULT_PATH at call time,
// not here, so the same Service follows VAULT_PATH changes.
func New() *Service { return &Service{} }

// vaultRoot resolves the vault root from VAULT_PATH at call time, falling back
// to the legacy default (Requirement 6.6).
func vaultRoot() string {
	if p := os.Getenv("VAULT_PATH"); p != "" {
		return p
	}
	return defaultVaultPath
}

// missingVault reports whether root is not an existing directory and, when so,
// returns the empty-list-with-warning result mandated by Requirement 6.5.
func missingVault(root string) (Suggestions, bool) {
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return Suggestions{
			Items:   []Suggestion{},
			Warning: "VAULT_PATH does not resolve to an existing directory: " + root,
		}, true
	}
	return Suggestions{}, false
}

// candidate is a note discovered during a scan, retained with its mtime so the
// combined recency set can be ordered by modification time descending.
type candidate struct {
	topic  string
	source string // vault-relative path
	mtime  time.Time
}

// Recency returns up to limit notes from 06-permanent/ and 00-inbox/ ordered by
// modification time descending. The topic is derived from the permanent-note
// filename (leading numeric ID stripped) or the inbox note's first "# " heading,
// and entries are deduplicated by lowercased topic (Requirement 6.2, 6.3). A
// missing vault yields an empty list and a warning, never an error
// (Requirement 6.5). No AWS call is made (Requirement 6.1).
func (s *Service) Recency(limit int) (Suggestions, error) {
	root := vaultRoot()
	if res, missing := missingVault(root); missing {
		return res, nil
	}
	if limit <= 0 {
		limit = defaultLimit
	}

	var found []candidate
	for _, dir := range recencyDirs {
		dirPath := filepath.Join(root, dir)
		entries, err := os.ReadDir(dirPath)
		if err != nil {
			// A missing or unreadable subdirectory simply contributes nothing.
			continue
		}
		for _, entry := range entries {
			if entry.IsDir() || !isMarkdown(entry.Name()) {
				continue
			}
			notePath := filepath.Join(dirPath, entry.Name())
			info, err := entry.Info()
			if err != nil {
				continue
			}
			topic := recencyTopic(dir, notePath, entry.Name())
			if topic == "" {
				continue
			}
			rel, err := filepath.Rel(root, notePath)
			if err != nil {
				rel = notePath
			}
			found = append(found, candidate{
				topic:  topic,
				source: filepath.ToSlash(rel),
				mtime:  info.ModTime(),
			})
		}
	}

	// Order the combined set by mtime descending; SliceStable keeps the scan
	// order for equal mtimes so results are deterministic (Requirement 6.2).
	sort.SliceStable(found, func(i, j int) bool {
		return found[i].mtime.After(found[j].mtime)
	})

	return Suggestions{Items: dedupCap(found, limit)}, nil
}

// Search matches query case-insensitively against the derived title of every
// *.md note anywhere under the vault and returns up to limit results
// (Requirement 6.4). A missing vault yields an empty list and a warning, never
// an error (Requirement 6.5). No AWS call is made (Requirement 6.1).
func (s *Service) Search(query string, limit int) (Suggestions, error) {
	root := vaultRoot()
	if res, missing := missingVault(root); missing {
		return res, nil
	}
	if limit <= 0 {
		limit = defaultLimit
	}
	needle := strings.ToLower(strings.TrimSpace(query))
	if needle == "" {
		// No query means nothing to match; return an empty list, not the vault.
		return Suggestions{Items: []Suggestion{}}, nil
	}

	var found []candidate
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !isMarkdown(d.Name()) {
			return nil
		}
		title := deriveTitle(path, d.Name())
		if title == "" || !strings.Contains(strings.ToLower(title), needle) {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			rel = path
		}
		found = append(found, candidate{
			topic:  title,
			source: filepath.ToSlash(rel),
			mtime:  info.ModTime(),
		})
		return nil
	})

	// Most-recent first so the freshest matching notes lead the result list.
	sort.SliceStable(found, func(i, j int) bool {
		return found[i].mtime.After(found[j].mtime)
	})

	return Suggestions{Items: dedupCap(found, limit)}, nil
}

// dedupCap converts ordered candidates into suggestions, dropping topics that
// are too short or repeat an already-seen lowercased topic, and stops once
// limit entries are collected (Requirement 6.3). The first occurrence in the
// ordered slice wins, so callers control precedence by pre-sorting.
func dedupCap(found []candidate, limit int) []Suggestion {
	items := make([]Suggestion, 0, limit)
	seen := make(map[string]struct{}, len(found))
	today := time.Now()
	for _, c := range found {
		if len(c.topic) < minTopicLen {
			continue
		}
		key := strings.ToLower(c.topic)
		if _, dup := seen[key]; dup {
			continue
		}
		seen[key] = struct{}{}
		items = append(items, Suggestion{
			Topic:       c.topic,
			LastCovered: c.mtime.Format("2006-01-02"),
			DaysSince:   daysBetween(c.mtime, today),
			Source:      c.source,
		})
		if len(items) >= limit {
			break
		}
	}
	return items
}

// recencyTopic derives a recency-list topic for a note: permanent notes use the
// filename with any leading numeric ID stripped; inbox notes use the first
// "# " heading, falling back to the filename with dashes turned to spaces
// (Requirement 6.3).
func recencyTopic(dir, notePath, name string) string {
	stem := strings.TrimSuffix(name, filepath.Ext(name))
	if dir == "06-permanent" {
		return strings.TrimSpace(leadingID.ReplaceAllString(stem, ""))
	}
	// Inbox: prefer the first heading.
	if heading := firstHeading(notePath); heading != "" {
		return heading
	}
	return strings.ReplaceAll(stem, "-", " ")
}

// deriveTitle derives a searchable title for any note: the first "# " heading
// if present, otherwise the filename with a leading numeric ID stripped and
// dashes turned to spaces (Requirement 6.4).
func deriveTitle(notePath, name string) string {
	if heading := firstHeading(notePath); heading != "" {
		return heading
	}
	stem := strings.TrimSuffix(name, filepath.Ext(name))
	stem = strings.TrimSpace(leadingID.ReplaceAllString(stem, ""))
	return strings.ReplaceAll(stem, "-", " ")
}

// firstHeading returns the text of the first "# " heading in the note, or "".
// Read errors are swallowed: a note we cannot read simply has no heading.
func firstHeading(notePath string) string {
	data, err := os.ReadFile(notePath)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "# ") {
			return strings.TrimSpace(line[2:])
		}
	}
	return ""
}

// isMarkdown reports whether name is a markdown note.
func isMarkdown(name string) bool {
	return strings.EqualFold(filepath.Ext(name), ".md")
}

// daysBetween returns the whole-day gap between an earlier mtime and now,
// floored at zero so a future-dated note never reports negative days.
func daysBetween(mtime, now time.Time) int {
	d := int(now.Sub(mtime).Hours() / 24)
	if d < 0 {
		return 0
	}
	return d
}
