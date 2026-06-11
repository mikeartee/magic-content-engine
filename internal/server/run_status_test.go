package server_test

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// Requirement 1.2/1.3/1.5: GET /api/run/status?run_id= resolves
// output/<run_id>/agent-log.jsonl, sets the SSE headers, and replays the log.
func TestRunStatusStreamsSSE(t *testing.T) {
	root := t.TempDir()
	runDir := filepath.Join(root, "r1")
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	line := `{"event_type":"agent_completed","timestamp":"2026-06-02T10:00:00Z","agent_type":"writer","run_id":"r1","details":{}}`
	if err := os.WriteFile(filepath.Join(runDir, "agent-log.jsonl"), []byte(line+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	ui := fstest.MapFS{"index.html": &fstest.MapFile{Data: []byte("<title>Bullpen Console</title>")}}
	h := server.New(ui, server.WithOutputDir(root)).Routes()

	req := httptest.NewRequest(http.MethodGet, "/api/run/status?run_id=r1", nil)
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		h.ServeHTTP(rec, req)
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("run status stream did not terminate")
	}

	if ct := rec.Header().Get("Content-Type"); ct != "text/event-stream" {
		t.Errorf("Content-Type = %q, want text/event-stream", ct)
	}
	if cc := rec.Header().Get("Cache-Control"); cc != "no-cache" {
		t.Errorf("Cache-Control = %q, want no-cache", cc)
	}
	if xa := rec.Header().Get("X-Accel-Buffering"); xa != "no" {
		t.Errorf("X-Accel-Buffering = %q, want no", xa)
	}

	body := rec.Body.String()
	if !strings.Contains(body, "agent_completed") {
		t.Errorf("replayed event missing from stream body: %q", body)
	}
	if !strings.Contains(body, "event: pipeline_complete") {
		t.Errorf("synthetic terminal frame missing: %q", body)
	}
}
