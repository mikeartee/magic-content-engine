package server_test

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/run"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// startGateHTTPServer starts an httptest server for h and registers its shutdown.
func startGateHTTPServer(t *testing.T, h http.Handler) string {
	t.Helper()
	ts := httptest.NewServer(h)
	t.Cleanup(ts.Close)
	return ts.URL
}

// postRunPath POSTs an empty body to path and returns the response (caller
// closes the body).
func postRunPath(t *testing.T, baseURL, path string) *http.Response {
	t.Helper()
	resp, err := http.Post(baseURL+path, "application/json", nil)
	if err != nil {
		t.Fatalf("POST %s: %v", path, err)
	}
	return resp
}

// runStubGateRunner is the deterministic stand-in for the Python headless runner
// exercising the approval gate across the process boundary. It writes a publish
// verdict and an approval_gate_presented event, then polls
// approval-decision.json using the SAME read-and-delete semantics as the real
// control_file_gate.py poller: on "approved" it resumes to the publisher; on
// "rejected" it retains the files (no publisher event). It performs no AWS call.
func runStubGateRunner(argv []string, dir string) {
	runID := argValue(argv, "--run-id")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		os.Exit(1)
	}
	logPath := filepath.Join(dir, "agent-log.jsonl")
	appendEvent := func(eventType, agent, details string) {
		f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			os.Exit(1)
		}
		line := `{"event_type":"` + eventType + `","timestamp":"2026-06-02T10:00:00Z","agent_type":"` +
			agent + `","run_id":"` + runID + `","details":` + details + "}\n"
		_, _ = io.WriteString(f, line)
		_ = f.Sync()
		_ = f.Close()
		time.Sleep(10 * time.Millisecond)
	}

	appendEvent("verdict", "subeditor", `{"filename":"post.md","verdict":"publish"}`)
	appendEvent("approval_gate_presented", "editor_in_chief", `{"files_pending_approval":["post.md"]}`)

	// Poll for the Console's decision, consuming (deleting) it like the real
	// poller so no stale decision is ever honoured.
	decisionPath := filepath.Join(dir, "approval-decision.json")
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		raw, err := os.ReadFile(decisionPath)
		if err != nil {
			time.Sleep(10 * time.Millisecond)
			continue
		}
		var d struct {
			Decision string `json:"decision"`
		}
		if json.Unmarshal(raw, &d) != nil {
			time.Sleep(10 * time.Millisecond)
			continue
		}
		_ = os.Remove(decisionPath) // consume the decision
		if d.Decision == "approved" {
			appendEvent("agent_completed", "publisher", `{"published":true}`)
		} else {
			appendEvent("approval_rejected", "editor_in_chief", `{"files_retained":["post.md"]}`)
		}
		os.Exit(0)
	}
	os.Exit(1) // timed out waiting for a decision
}

// waitForLogContains blocks until logPath contains substr or the timeout fires.
func waitForLogContains(t *testing.T, logPath, substr string, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if data, err := os.ReadFile(logPath); err == nil && strings.Contains(string(data), substr) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("log %s never contained %q within %s", logPath, substr, timeout)
}

// newGateServer wires a real run.Manager that re-executes this test binary as
// the gate stub runner, with a server tuned for fast, deterministic SSE timing.
func newGateServer(t *testing.T, root string) (*run.Manager, string) {
	t.Helper()
	rm := run.New(root, run.DefaultStarter,
		run.WithPython(os.Args[0], "stub-runner"),
		run.WithCompletionWatch(func(c *exec.Cmd) error { return c.Wait() }),
	)
	srv := server.New(e2eTestUI(),
		server.WithOutputDir(root),
		server.WithActiveProbe(func(runID string) bool {
			h, ok := rm.Active()
			return ok && h.RunID == runID
		}),
		server.WithSSETiming(15*time.Millisecond, 2),
	)
	srv.SetRunManager(rm)
	ts := startGateHTTPServer(t, srv.Routes())
	return rm, ts
}

// TestEndToEndApprovalGateApproveResumesToPublisher is the approve tracer
// bullet: POST /api/run spawns the (stub) runner which presents a gate; POST
// /api/run/approve writes approval-decision.json atomically; the stub consumes
// the decision and resumes to the publisher; the SSE timeline shows the
// publisher event and a single terminal frame; the decision file is consumed.
//
// Requirements 2 and 3 end-to-end. No AWS, no network beyond loopback.
func TestEndToEndApprovalGateApproveResumesToPublisher(t *testing.T) {
	root := t.TempDir()
	_, baseURL := newGateServer(t, root)

	runID := postStartRun(t, baseURL, `{"topic":"gate approve flow","outputs":["all"]}`)

	logPath := filepath.Join(root, runID, "agent-log.jsonl")
	waitForLogContains(t, logPath, "approval_gate_presented", 10*time.Second)

	resp := postRunPath(t, baseURL, "/api/run/approve")
	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		t.Fatalf("POST /api/run/approve code = %d, want 200 (%s)", resp.StatusCode, raw)
	}
	resp.Body.Close()

	body := streamRunStatus(t, baseURL, runID, 30*time.Second)
	if !strings.Contains(body, `"agent_type":"publisher"`) {
		t.Errorf("approve did not resume to publisher\n%s", body)
	}
	if got := strings.Count(body, "event: pipeline_complete"); got != 1 {
		t.Errorf("terminal frame count = %d, want exactly 1\n%s", got, body)
	}

	decPath := filepath.Join(root, runID, "approval-decision.json")
	if _, err := os.Stat(decPath); !os.IsNotExist(err) {
		t.Errorf("decision file not consumed (no stale decision allowed): stat err=%v", err)
	}
}

// TestEndToEndApprovalGateRejectRetainsFiles is the reject tracer bullet: the
// same flow, but POST /api/run/reject leaves the files retained — no publisher
// event is ever emitted — and the decision file is still consumed.
//
// Requirements 2 and 3 end-to-end.
func TestEndToEndApprovalGateRejectRetainsFiles(t *testing.T) {
	root := t.TempDir()
	_, baseURL := newGateServer(t, root)

	runID := postStartRun(t, baseURL, `{"topic":"gate reject flow","outputs":["all"]}`)

	logPath := filepath.Join(root, runID, "agent-log.jsonl")
	waitForLogContains(t, logPath, "approval_gate_presented", 10*time.Second)

	resp := postRunPath(t, baseURL, "/api/run/reject")
	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		t.Fatalf("POST /api/run/reject code = %d, want 200 (%s)", resp.StatusCode, raw)
	}
	resp.Body.Close()

	body := streamRunStatus(t, baseURL, runID, 30*time.Second)
	if strings.Contains(body, `"agent_type":"publisher"`) {
		t.Errorf("reject must not publish, but a publisher event appeared\n%s", body)
	}
	if !strings.Contains(body, "approval_rejected") {
		t.Errorf("reject did not record approval_rejected\n%s", body)
	}

	decPath := filepath.Join(root, runID, "approval-decision.json")
	if _, err := os.Stat(decPath); !os.IsNotExist(err) {
		t.Errorf("decision file not consumed: stat err=%v", err)
	}
}

// TestEndToEndApproveWithNoActiveGateConflicts asserts the 409 path end-to-end:
// approve before any Run is active returns HTTP 409.
func TestEndToEndApproveWithNoActiveGateConflicts(t *testing.T) {
	root := t.TempDir()
	_, baseURL := newGateServer(t, root)

	resp := postRunPath(t, baseURL, "/api/run/approve")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("approve with no active run code = %d, want 409", resp.StatusCode)
	}
}
