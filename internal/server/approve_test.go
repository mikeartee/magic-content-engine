package server_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/mikeartee/magic-content-engine/console/internal/run"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// fakeApprover doubles for the Run_Manager in the approve/reject handler tests.
// It satisfies both RunStarter (so SetRunManager wires it) and the approval
// surface, recording each Decide call and returning a canned error so the
// server's status-code mapping can be exercised without spawning python.
type fakeApprover struct {
	decideErr error
	decided   []bool
}

func (f *fakeApprover) Start(req run.StartRequest) (run.RunHandle, error) {
	return run.RunHandle{RunID: "active"}, nil
}

func (f *fakeApprover) Decide(approved bool) error {
	f.decided = append(f.decided, approved)
	return f.decideErr
}

func newApproveServer(t *testing.T, fa *fakeApprover) http.Handler {
	t.Helper()
	ui := fstest.MapFS{
		"index.html": &fstest.MapFile{Data: []byte("<!doctype html><title>Bullpen Console</title>")},
	}
	s := server.New(ui)
	s.SetRunManager(fa)
	return s.Routes()
}

func postPath(t *testing.T, h http.Handler, path string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, path, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// Requirement 2 / 12.3: POST /api/run/approve resolves the gate with approved
// = true and returns 200.
func TestApproveResolvesGate(t *testing.T) {
	fa := &fakeApprover{}
	h := newApproveServer(t, fa)

	rec := postPath(t, h, "/api/run/approve")

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (%s)", rec.Code, rec.Body.String())
	}
	if len(fa.decided) != 1 || fa.decided[0] != true {
		t.Errorf("decided = %v, want [true]", fa.decided)
	}
}

// Requirement 2 / 12.3: POST /api/run/reject resolves the gate with approved
// = false and returns 200.
func TestRejectResolvesGate(t *testing.T) {
	fa := &fakeApprover{}
	h := newApproveServer(t, fa)

	rec := postPath(t, h, "/api/run/reject")

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (%s)", rec.Code, rec.Body.String())
	}
	if len(fa.decided) != 1 || fa.decided[0] != false {
		t.Errorf("decided = %v, want [false]", fa.decided)
	}
}

// Requirement 12.4: approve with no active gate returns HTTP 409 with the
// canonical conflict body.
func TestApproveNoGateConflict(t *testing.T) {
	fa := &fakeApprover{decideErr: run.ErrNoGate}
	h := newApproveServer(t, fa)

	rec := postPath(t, h, "/api/run/approve")

	assertGateConflict(t, rec)
}

// Requirement 12.4: reject with no active gate returns HTTP 409.
func TestRejectNoGateConflict(t *testing.T) {
	fa := &fakeApprover{decideErr: run.ErrNoGate}
	h := newApproveServer(t, fa)

	rec := postPath(t, h, "/api/run/reject")

	assertGateConflict(t, rec)
}

func assertGateConflict(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409 (%s)", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
	body := decodeError(t, rec)
	if body["error"] != "conflict" {
		t.Errorf("error code = %v, want conflict", body["error"])
	}
	if d, _ := body["detail"].(string); d != "No approval gate is currently waiting." {
		t.Errorf("detail = %q, want the canonical no-gate message", d)
	}
}
