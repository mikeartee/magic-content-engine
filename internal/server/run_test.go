package server_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/mikeartee/magic-content-engine/console/internal/run"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// fakeRunStarter is an injectable RunManager double for the POST /api/run
// handler tests. It records the last request and returns a canned result so
// the server's status-code mapping can be exercised without spawning python.
type fakeRunStarter struct {
	handle run.RunHandle
	err    error
	calls  int
	last   run.StartRequest
}

func (f *fakeRunStarter) Start(req run.StartRequest) (run.RunHandle, error) {
	f.calls++
	f.last = req
	return f.handle, f.err
}

func newRunServer(t *testing.T, rm server.RunStarter) http.Handler {
	t.Helper()
	ui := fstest.MapFS{
		"index.html": &fstest.MapFile{Data: []byte("<!doctype html><title>Bullpen Console</title>")},
	}
	s := server.New(ui)
	s.SetRunManager(rm)
	return s.Routes()
}

func postRun(t *testing.T, h http.Handler, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/run", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// Requirement 7.1: a valid request yields 202 with {run_id}.
func TestPostRunStartsAccepted(t *testing.T) {
	rm := &fakeRunStarter{handle: run.RunHandle{RunID: "deadbeef"}}
	h := newRunServer(t, rm)

	rec := postRun(t, h, `{"topic":"AgentCore in Sydney","outputs":["all"]}`)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	if body["run_id"] != "deadbeef" {
		t.Errorf("run_id = %v, want deadbeef", body["run_id"])
	}
	if rm.calls != 1 {
		t.Errorf("Start calls = %d, want 1", rm.calls)
	}
	if rm.last.Topic != "AgentCore in Sydney" {
		t.Errorf("forwarded topic = %q", rm.last.Topic)
	}
}

// Requirement 7.3: a Run already in progress maps to 409.
func TestPostRunConflictWhenActive(t *testing.T) {
	rm := &fakeRunStarter{err: run.ErrRunInProgress}
	h := newRunServer(t, rm)

	rec := postRun(t, h, `{"topic":"t","outputs":["all"]}`)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409", rec.Code)
	}
	if _, ok := decodeError(t, rec)["error"]; !ok {
		t.Error("missing error code in 409 body")
	}
}

// Requirement 7.4: an empty topic maps to 422 and never reaches the manager.
func TestPostRunEmptyTopicUnprocessable(t *testing.T) {
	rm := &fakeRunStarter{handle: run.RunHandle{RunID: "should-not-happen"}}
	h := newRunServer(t, rm)

	rec := postRun(t, h, `{"topic":"   ","outputs":["all"]}`)

	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422", rec.Code)
	}
	if rm.calls != 0 {
		t.Errorf("manager Start called %d times on invalid input, want 0", rm.calls)
	}
}

// Requirement 7.4: invalid outputs map to 422.
func TestPostRunInvalidOutputsUnprocessable(t *testing.T) {
	rm := &fakeRunStarter{handle: run.RunHandle{RunID: "nope"}}
	h := newRunServer(t, rm)

	for _, body := range []string{
		`{"topic":"t","outputs":["bogus"]}`,
		`{"topic":"t","outputs":[]}`,
		`{"topic":"t","outputs":["all","blog"]}`,
		`{"topic":"t"}`,
	} {
		rec := postRun(t, h, body)
		if rec.Code != http.StatusUnprocessableEntity {
			t.Errorf("body %s: status = %d, want 422", body, rec.Code)
		}
	}
	if rm.calls != 0 {
		t.Errorf("manager Start called %d times on invalid outputs, want 0", rm.calls)
	}
}

// Requirement 7.4: a malformed JSON body maps to 422.
func TestPostRunMalformedBodyUnprocessable(t *testing.T) {
	rm := &fakeRunStarter{}
	h := newRunServer(t, rm)

	rec := postRun(t, h, `{not json`)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422", rec.Code)
	}
}

// Requirement 7.6: a spawn failure maps to 500 with body
// {"error":"spawn_failed","detail":...} and no run_id.
func TestPostRunSpawnFailure(t *testing.T) {
	rm := &fakeRunStarter{err: &run.SpawnError{Err: errors.New("python not found")}}
	h := newRunServer(t, rm)

	rec := postRun(t, h, `{"topic":"t","outputs":["all"]}`)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	body := decodeError(t, rec)
	if body["error"] != "spawn_failed" {
		t.Errorf("error code = %v, want spawn_failed", body["error"])
	}
	if d, ok := body["detail"].(string); !ok || d == "" {
		t.Errorf("missing detail in spawn_failed body: %v", body)
	}
	if _, ok := body["run_id"]; ok {
		t.Errorf("spawn failure response must not carry a run_id: %v", body)
	}
}

// Requirement 12.2: POST /api/run error responses use the JSON error shape.
func TestPostRunErrorShapeIsJSON(t *testing.T) {
	rm := &fakeRunStarter{err: run.ErrRunInProgress}
	h := newRunServer(t, rm)

	rec := postRun(t, h, `{"topic":"t","outputs":["all"]}`)
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
}

func decodeError(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	return body
}
