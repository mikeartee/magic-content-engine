package server_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/mikeartee/magic-content-engine/console/internal/files"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// newFileServer wires a real file service rooted at a temp output dir and
// returns the handler plus the output root so tests can seed run files.
func newFileServer(t *testing.T) (http.Handler, string) {
	t.Helper()
	output := filepath.Join(t.TempDir(), "output")
	if err := os.MkdirAll(output, 0o755); err != nil {
		t.Fatalf("mkdir output: %v", err)
	}
	ui := fstest.MapFS{
		"index.html": &fstest.MapFile{Data: []byte("<!doctype html><title>Bullpen Console</title>")},
	}
	s := server.New(ui)
	s.SetFileService(files.New(output))
	return s.Routes(), output
}

func seedFile(t *testing.T, output, rel, content string) {
	t.Helper()
	path := filepath.Join(output, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// Requirement 4.2: GET /api/runs lists runs one level deep with exclusions.
func TestGetRunsListsWithExclusions(t *testing.T) {
	h, output := newFileServer(t)
	seedFile(t, output, "run1/post.md", "x")
	seedFile(t, output, "run1/agent-log.jsonl", "{}")
	seedFile(t, output, "run1/checkpoints.json", "{}")
	seedFile(t, output, "run1/2026-01-01-slug/post.md", "y")

	req := httptest.NewRequest(http.MethodGet, "/api/runs", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var body struct {
		Runs []files.RunListing `json:"runs"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	if len(body.Runs) != 1 || body.Runs[0].ID != "run1" {
		t.Fatalf("runs = %+v, want one run 'run1'", body.Runs)
	}
	got := strings.Join(body.Runs[0].Files, ",")
	if !strings.Contains(got, "post.md") || !strings.Contains(got, "2026-01-01-slug/post.md") {
		t.Errorf("files = %v, want post.md and 2026-01-01-slug/post.md", body.Runs[0].Files)
	}
	if strings.Contains(got, "agent-log.jsonl") || strings.Contains(got, "checkpoints.json") {
		t.Errorf("files = %v, must exclude agent-log.jsonl and checkpoints.json", body.Runs[0].Files)
	}
}

// Requirement 4.3: GET /api/runs/{id}/file?name= reads a file including a
// subdir segment.
func TestGetFileReadsSubdir(t *testing.T) {
	h, output := newFileServer(t)
	seedFile(t, output, "run1/2026-01-01-slug/post.md", "nested-body")

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run1/file?name=2026-01-01-slug/post.md", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (%q)", rec.Code, rec.Body.String())
	}
	if rec.Body.String() != "nested-body" {
		t.Errorf("body = %q, want nested-body", rec.Body.String())
	}
}

// Requirement 4.1: GET file with a missing name yields 400; a missing file 404.
func TestGetFileMissingNameAndNotFound(t *testing.T) {
	h, output := newFileServer(t)
	seedFile(t, output, "run1/post.md", "x")

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run1/file", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("missing name: status = %d, want 400", rec.Code)
	}

	req = httptest.NewRequest(http.MethodGet, "/api/runs/run1/file?name=nope.md", nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Errorf("missing file: status = %d, want 404", rec.Code)
	}
}

// Requirement 4.4: POST /api/runs/{id}/file saves and round-trips.
func TestPostFileSavesAtomically(t *testing.T) {
	h, output := newFileServer(t)
	if err := os.MkdirAll(filepath.Join(output, "run1"), 0o755); err != nil {
		t.Fatal(err)
	}

	body := `{"name":"post.md","content":"hello world"}`
	req := httptest.NewRequest(http.MethodPost, "/api/runs/run1/file", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (%q)", rec.Code, rec.Body.String())
	}
	saved, err := os.ReadFile(filepath.Join(output, "run1", "post.md"))
	if err != nil {
		t.Fatalf("read saved: %v", err)
	}
	if string(saved) != "hello world" {
		t.Errorf("saved content = %q, want hello world", saved)
	}
}

// Requirement 4.4: POST with empty content/name is rejected with 422.
func TestPostFileValidation(t *testing.T) {
	h, _ := newFileServer(t)
	for _, body := range []string{
		`{"name":"","content":"x"}`,
		`{"name":"post.md","content":""}`,
		`{not json`,
	} {
		req := httptest.NewRequest(http.MethodPost, "/api/runs/run1/file", bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnprocessableEntity {
			t.Errorf("body %s: status = %d, want 422", body, rec.Code)
		}
	}
}

// Requirement 4.5: GET /api/runs/{id}/download/{file} sets Content-Disposition.
func TestDownloadSetsAttachmentHeader(t *testing.T) {
	h, output := newFileServer(t)
	seedFile(t, output, "run1/post.md", "downloadable")

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run1/download/post.md", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (%q)", rec.Code, rec.Body.String())
	}
	cd := rec.Header().Get("Content-Disposition")
	if !strings.HasPrefix(cd, "attachment") {
		t.Errorf("Content-Disposition = %q, want attachment", cd)
	}
	if !strings.Contains(cd, "post.md") {
		t.Errorf("Content-Disposition = %q, want filename post.md", cd)
	}
	if rec.Body.String() != "downloadable" {
		t.Errorf("body = %q, want downloadable", rec.Body.String())
	}
}

// Requirement 4.1: path traversal is rejected with HTTP 403 and code forbidden
// across every file endpoint.
func TestFileEndpointsRejectTraversalWith403(t *testing.T) {
	h, output := newFileServer(t)
	seedFile(t, output, "run1/post.md", "x")

	// GET read with a traversal name.
	req := httptest.NewRequest(http.MethodGet, "/api/runs/run1/file?name=../../etc/passwd", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	assertForbidden(t, rec, "GET file")

	// POST save with a traversal name.
	req = httptest.NewRequest(http.MethodPost, "/api/runs/run1/file",
		bytes.NewBufferString(`{"name":"../escape.md","content":"nope"}`))
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	assertForbidden(t, rec, "POST file")

	// Download with a traversal segment.
	req = httptest.NewRequest(http.MethodGet, "/api/runs/run1/download/..%2f..%2fsecret", nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	assertForbidden(t, rec, "download")
}

func assertForbidden(t *testing.T, rec *httptest.ResponseRecorder, label string) {
	t.Helper()
	if rec.Code != http.StatusForbidden {
		t.Errorf("%s: status = %d, want 403 (%q)", label, rec.Code, rec.Body.String())
		return
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Errorf("%s: body not JSON: %v (%q)", label, err, rec.Body.String())
		return
	}
	if body["error"] != "forbidden" {
		t.Errorf("%s: error = %v, want forbidden", label, body["error"])
	}
}
