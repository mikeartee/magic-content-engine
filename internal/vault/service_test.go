package vault_test

import (
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/vault"
)

// writeNote creates a markdown note (and parents) with content and an explicit
// modification time so ordering-by-mtime tests are deterministic.
func writeNote(t *testing.T, path, content string, mtime time.Time) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatalf("chtimes %s: %v", path, err)
	}
}

// newVault points the service at a fresh temp vault root via VAULT_PATH and
// returns the service plus the root. VAULT_PATH is read at call time, so
// setting it through the environment is the supported override.
func newVault(t *testing.T) (*vault.Service, string) {
	t.Helper()
	root := t.TempDir()
	t.Setenv("VAULT_PATH", root)
	return vault.New(), root
}

func topics(items []vault.Suggestion) []string {
	out := make([]string, len(items))
	for i, it := range items {
		out[i] = it.Topic
	}
	return out
}

// Requirement 6.2 + 6.3: the recency list spans 06-permanent/ and 00-inbox/,
// is ordered by modification time descending, derives the topic from the
// permanent-note filename (leading numeric ID stripped) or the inbox note's
// first heading, and dedups by lowercased topic.
func TestRecencyOrdersByMtimeAndDerivesTopics(t *testing.T) {
	svc, root := newVault(t)

	base := time.Date(2026, 1, 10, 12, 0, 0, 0, time.UTC)
	// Permanent notes: topic comes from the filename, leading ID stripped.
	writeNote(t, filepath.Join(root, "06-permanent", "202604050001 AgentCore in Sydney.md"),
		"body", base.Add(-3*24*time.Hour)) // oldest
	writeNote(t, filepath.Join(root, "06-permanent", "Strands Agents SDK.md"),
		"body", base.Add(-1*24*time.Hour))
	// Inbox notes: topic comes from the first "# " heading.
	writeNote(t, filepath.Join(root, "00-inbox", "capture-one.md"),
		"# Kiro steering docs\n\nnotes", base) // newest
	writeNote(t, filepath.Join(root, "00-inbox", "capture-two.md"),
		"no heading here, fallback to filename", base.Add(-2*24*time.Hour))

	res, err := svc.Recency(10)
	if err != nil {
		t.Fatalf("Recency: %v", err)
	}
	got := topics(res.Items)
	want := []string{
		"Kiro steering docs",  // base (newest)
		"Strands Agents SDK",  // base-1d
		"capture two",         // base-2d, fallback filename, dashes -> spaces
		"AgentCore in Sydney", // base-3d, ID stripped
	}
	if len(got) != len(want) {
		t.Fatalf("recency topics = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("recency[%d] = %q, want %q (full: %v)", i, got[i], want[i], got)
		}
	}
}

// Requirement 6.3: entries are deduplicated by lowercased topic; the most
// recent occurrence wins.
func TestRecencyDedupsByLowercasedTopic(t *testing.T) {
	svc, root := newVault(t)
	base := time.Date(2026, 2, 1, 9, 0, 0, 0, time.UTC)

	// Two notes whose derived topics differ only by case.
	writeNote(t, filepath.Join(root, "06-permanent", "Bedrock Models.md"),
		"body", base.Add(-5*24*time.Hour))
	writeNote(t, filepath.Join(root, "00-inbox", "newer.md"),
		"# bedrock models\n", base)

	res, err := svc.Recency(10)
	if err != nil {
		t.Fatalf("Recency: %v", err)
	}
	if len(res.Items) != 1 {
		t.Fatalf("expected 1 deduped entry, got %v", topics(res.Items))
	}
	// The newest occurrence wins the slot.
	if res.Items[0].Topic != "bedrock models" {
		t.Errorf("deduped topic = %q, want %q (newest wins)", res.Items[0].Topic, "bedrock models")
	}
}

// Requirement 6.2: the limit caps the number of recency entries returned.
func TestRecencyRespectsLimit(t *testing.T) {
	svc, root := newVault(t)
	base := time.Date(2026, 3, 1, 0, 0, 0, 0, time.UTC)
	for i, name := range []string{"Alpha topic", "Beta topic", "Gamma topic", "Delta topic"} {
		writeNote(t, filepath.Join(root, "06-permanent", name+".md"),
			"body", base.Add(time.Duration(-i)*24*time.Hour))
	}
	res, err := svc.Recency(2)
	if err != nil {
		t.Fatalf("Recency: %v", err)
	}
	if len(res.Items) != 2 {
		t.Fatalf("limit not honoured: got %d entries (%v), want 2", len(res.Items), topics(res.Items))
	}
}

// Requirement 6.3: each recency entry carries the mtime as an ISO date plus the
// days-since and a vault-relative source path.
func TestRecencyPopulatesMetadata(t *testing.T) {
	svc, root := newVault(t)
	mtime := time.Now().AddDate(0, 0, -4)
	writeNote(t, filepath.Join(root, "06-permanent", "Metadata topic.md"), "body", mtime)

	res, err := svc.Recency(10)
	if err != nil {
		t.Fatalf("Recency: %v", err)
	}
	if len(res.Items) != 1 {
		t.Fatalf("got %d entries, want 1", len(res.Items))
	}
	item := res.Items[0]
	if item.LastCovered != mtime.Format("2006-01-02") {
		t.Errorf("last_covered = %q, want %q", item.LastCovered, mtime.Format("2006-01-02"))
	}
	if item.DaysSince != 4 {
		t.Errorf("days_since = %d, want 4", item.DaysSince)
	}
	if item.Source != filepath.ToSlash(filepath.Join("06-permanent", "Metadata topic.md")) {
		t.Errorf("source = %q, want 06-permanent/Metadata topic.md", item.Source)
	}
}

// Requirement 6.4: search matches the query case-insensitively against the
// derived title of every *.md note anywhere under the vault.
func TestSearchMatchesTitlesCaseInsensitivelyAcrossVault(t *testing.T) {
	svc, root := newVault(t)
	now := time.Now()
	writeNote(t, filepath.Join(root, "06-permanent", "202601010001 Bedrock AgentCore.md"), "x", now)
	writeNote(t, filepath.Join(root, "00-inbox", "note.md"), "# Strands Agents\n", now)
	writeNote(t, filepath.Join(root, "01-projects", "deep", "nested.md"), "# Kiro Power\n", now)
	writeNote(t, filepath.Join(root, "06-permanent", "Unrelated thing.md"), "x", now)

	res, err := svc.Search("agent", 10)
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	got := topics(res.Items)
	// "Bedrock AgentCore" and "Strands Agents" both contain "agent"
	// case-insensitively; the nested and unrelated notes do not.
	if len(got) != 2 {
		t.Fatalf("search results = %v, want 2 matches", got)
	}
	joined := strings.ToLower(strings.Join(got, "|"))
	if !strings.Contains(joined, "agentcore") || !strings.Contains(joined, "strands agents") {
		t.Errorf("search results = %v, want AgentCore and Strands Agents", got)
	}
}

// Requirement 6.4: search honours the result cap.
func TestSearchRespectsLimit(t *testing.T) {
	svc, root := newVault(t)
	now := time.Now()
	for _, n := range []string{"alpha match.md", "beta match.md", "gamma match.md"} {
		writeNote(t, filepath.Join(root, "06-permanent", n), "x", now)
	}
	res, err := svc.Search("match", 2)
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(res.Items) != 2 {
		t.Fatalf("search cap not honoured: got %d (%v), want 2", len(res.Items), topics(res.Items))
	}
}

// Requirement 6.5: a missing VAULT_PATH yields an empty list and a warning,
// never an error, for both capabilities.
func TestMissingVaultPathReturnsEmptyListWithWarning(t *testing.T) {
	t.Setenv("VAULT_PATH", filepath.Join(t.TempDir(), "does-not-exist"))
	svc := vault.New()

	rec, err := svc.Recency(10)
	if err != nil {
		t.Fatalf("Recency(missing) error = %v, want nil", err)
	}
	if len(rec.Items) != 0 {
		t.Errorf("Recency(missing) items = %v, want empty", topics(rec.Items))
	}
	if rec.Warning == "" {
		t.Errorf("Recency(missing) warning is empty, want a warning")
	}

	sr, err := svc.Search("anything", 10)
	if err != nil {
		t.Fatalf("Search(missing) error = %v, want nil", err)
	}
	if len(sr.Items) != 0 {
		t.Errorf("Search(missing) items = %v, want empty", topics(sr.Items))
	}
	if sr.Warning == "" {
		t.Errorf("Search(missing) warning is empty, want a warning")
	}
}

// Requirement 6.6: VAULT_PATH is read at call time, not cached at construction.
// The service is constructed before the env var points at a populated vault.
func TestVaultPathReadAtCallTime(t *testing.T) {
	// Construct first with VAULT_PATH pointing nowhere useful.
	t.Setenv("VAULT_PATH", filepath.Join(t.TempDir(), "empty-initially"))
	svc := vault.New()

	// Now repoint VAULT_PATH at a populated vault and confirm the service sees it.
	root := t.TempDir()
	t.Setenv("VAULT_PATH", root)
	writeNote(t, filepath.Join(root, "06-permanent", "Late binding topic.md"), "x", time.Now())

	res, err := svc.Recency(10)
	if err != nil {
		t.Fatalf("Recency: %v", err)
	}
	if len(res.Items) != 1 || res.Items[0].Topic != "Late binding topic" {
		t.Errorf("call-time VAULT_PATH not honoured: got %v", topics(res.Items))
	}
}

// Requirement 6.1 (Property 7): the suggestion service is vault-only and makes
// no AWS call. Structurally, no source file in the package may import an AWS
// package.
func TestPackageImportsNoAWS(t *testing.T) {
	fset := token.NewFileSet()
	pkgs, err := parser.ParseDir(fset, ".", nil, parser.ImportsOnly)
	if err != nil {
		t.Fatalf("parse package dir: %v", err)
	}
	for _, pkg := range pkgs {
		for name, file := range pkg.Files {
			for _, imp := range file.Imports {
				path := strings.Trim(imp.Path.Value, `"`)
				if strings.Contains(strings.ToLower(path), "aws") {
					t.Errorf("%s imports AWS package %q; suggestions must be vault-only", name, path)
				}
			}
		}
	}
}
