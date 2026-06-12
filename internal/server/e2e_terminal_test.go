package server_test

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/run"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// runStubTerminalRunner is the deterministic stand-in that exercises the
// non-gate terminal states. It dispatches on the topic:
//
//   - contains "escalate"    -> write a file_escalated event, no publish
//     verdict, exit 0 (Escalated terminal state, Requirement 3.4).
//   - contains "error-event" -> write a pipeline_complete status=error event,
//     exit 0 (Errored by terminal event, Requirement 3.5).
//   - contains "error-exit"  -> write normal events, NO terminal event, exit
//     nonzero (Errored synthesised by reconciliation, Requirement 3.6).
//
// It performs no AWS call and reads no credentials.
func runStubTerminalRunner(argv []string, dir string) {
	runID := argValue(argv, "--run-id")
	topic := argValue(argv, "--topic")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		os.Exit(1)
	}
	logPath := filepath.Join(dir, "agent-log.jsonl")
	appendEvent := func(eventType, agent, ts, details string) {
		f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			os.Exit(1)
		}
		line := fmt.Sprintf(
			`{"event_type":%q,"timestamp":%q,"agent_type":%q,"run_id":%q,"details":%s}`,
			eventType, ts, agent, runID, details)
		_, _ = io.WriteString(f, line+"\n")
		_ = f.Sync()
		_ = f.Close()
		time.Sleep(10 * time.Millisecond)
	}

	appendEvent("agent_completed", "researcher", "2026-06-02T10:00:00Z", "{}")

	switch {
	case strings.Contains(topic, "escalate"):
		appendEvent("file_escalated", "subeditor", "2026-06-02T10:00:01Z",
			`{"filename":"post.md","reason":"failed fact-check twice"}`)
		os.Exit(0)
	case strings.Contains(topic, "error-event"):
		appendEvent("pipeline_complete", "editor_in_chief", "2026-06-02T10:00:01Z",
			`{"status":"error","error":"writer raised","traceback":"..."}`)
		os.Exit(0)
	case strings.Contains(topic, "error-exit"):
		// No terminal event; a nonzero exit must be reconciled into Errored.
		os.Exit(3)
	default:
		os.Exit(0)
	}
}

// newTerminalServer wires a real run.Manager that re-executes this test binary
// as the terminal stub runner, with the active and terminal probes the SSE hub
// needs to reconcile exit vs event and settle into one terminal frame.
func newTerminalServer(t *testing.T, root string) string {
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
		server.WithTerminalProbe(rm.TerminalStatus),
		server.WithSSETiming(15*time.Millisecond, 2),
	)
	srv.SetRunManager(rm)
	return startGateHTTPServer(t, srv.Routes())
}

// assertSingleTerminalFrame asserts the streamed body ended in exactly one
// terminal frame carrying the wanted status (Requirement 3.2: exactly one).
func assertSingleTerminalFrame(t *testing.T, body, wantStatus string) {
	t.Helper()
	if got := strings.Count(body, "event: pipeline_complete"); got != 1 {
		t.Errorf("terminal frame count = %d, want exactly 1\n%s", got, body)
	}
	want := `data: {"status":"` + wantStatus + `"}`
	if !strings.Contains(body, want) {
		t.Errorf("terminal frame did not carry %q\n%s", wantStatus, body)
	}
}

// Requirement 3.6 + 3.7 end-to-end: a runner that exits nonzero with NO
// terminal pipeline_complete event is reconciled into a single Errored terminal
// frame, so the UI never hangs. No AWS, no real Python.
func TestEndToEndErroredByNonzeroExitNoEvent(t *testing.T) {
	root := t.TempDir()
	baseURL := newTerminalServer(t, root)

	runID := postStartRun(t, baseURL, `{"topic":"error-exit boom","outputs":["all"]}`)
	body := streamRunStatus(t, baseURL, runID, 30*time.Second)
	assertSingleTerminalFrame(t, body, "error")
}

// Requirement 3.5 end-to-end: a pipeline_complete event reporting status=error
// settles into a single Errored terminal frame.
func TestEndToEndErroredByTerminalEvent(t *testing.T) {
	root := t.TempDir()
	baseURL := newTerminalServer(t, root)

	runID := postStartRun(t, baseURL, `{"topic":"error-event case","outputs":["all"]}`)
	body := streamRunStatus(t, baseURL, runID, 30*time.Second)
	assertSingleTerminalFrame(t, body, "error")
	if !strings.Contains(body, `"event_type":"pipeline_complete"`) {
		t.Errorf("expected the runner's pipeline_complete event in the stream\n%s", body)
	}
}

// Requirement 3.4 end-to-end: no publish verdict plus a file_escalated event
// settles into a single Escalated terminal frame listing the escalated file.
func TestEndToEndEscalatedState(t *testing.T) {
	root := t.TempDir()
	baseURL := newTerminalServer(t, root)

	runID := postStartRun(t, baseURL, `{"topic":"escalate this","outputs":["all"]}`)
	body := streamRunStatus(t, baseURL, runID, 30*time.Second)
	assertSingleTerminalFrame(t, body, "escalated")
	if !strings.Contains(body, `"event_type":"file_escalated"`) {
		t.Errorf("expected the file_escalated event in the stream\n%s", body)
	}
}
