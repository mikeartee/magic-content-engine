package run

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
)

// writeLog writes the given raw JSONL lines as agent-log.jsonl into dir.
func writeLog(t *testing.T, dir string, lines ...string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	var body string
	for _, l := range lines {
		body += l + "\n"
	}
	if err := os.WriteFile(filepath.Join(dir, agentLogFilename), []byte(body), 0o644); err != nil {
		t.Fatalf("write log: %v", err)
	}
}

// The four terminal-state cases plus the happy paths, derived purely from the
// log contents and a simulated exit. No real subprocess is involved
// (Requirement 3.2, 3.4, 3.5, 3.6, 3.7).
func TestReconcileTerminalStatus(t *testing.T) {
	const (
		evVerdictPublish   = `{"event_type":"verdict","timestamp":"2026-06-02T10:00:00Z","agent_type":"subeditor","run_id":"r","details":{"filename":"post.md","verdict":"publish"}}`
		evGatePresented    = `{"event_type":"approval_gate_presented","timestamp":"2026-06-02T10:00:01Z","agent_type":"editor_in_chief","run_id":"r","details":{"files_pending_approval":["post.md"]}}`
		evApprovalDecision = `{"event_type":"approval_decision","timestamp":"2026-06-02T10:00:02Z","agent_type":"editor_in_chief","run_id":"r","details":{"approved":true}}`
		evPublished        = `{"event_type":"agent_completed","timestamp":"2026-06-02T10:00:03Z","agent_type":"publisher","run_id":"r","details":{"published":true}}`
		evCompleteOK       = `{"event_type":"pipeline_complete","timestamp":"2026-06-02T10:00:04Z","agent_type":"editor_in_chief","run_id":"r","details":{"status":"complete"}}`
		evCompleteError    = `{"event_type":"pipeline_complete","timestamp":"2026-06-02T10:00:04Z","agent_type":"editor_in_chief","run_id":"r","details":{"status":"error","error":"boom","traceback":"..."}}`
		evCompleteHalted   = `{"event_type":"pipeline_complete","timestamp":"2026-06-02T10:00:04Z","agent_type":"editor_in_chief","run_id":"r","details":{"status":"halted"}}`
		evAgentError       = `{"event_type":"agent_error","timestamp":"2026-06-02T10:00:02Z","agent_type":"writer","run_id":"r","details":{"step":"writer","error":"model timed out"}}`
		evEscalated        = `{"event_type":"file_escalated","timestamp":"2026-06-02T10:00:02Z","agent_type":"subeditor","run_id":"r","details":{"filename":"post.md","reason":"failed fact-check twice"}}`
		evNormal           = `{"event_type":"agent_completed","timestamp":"2026-06-02T10:00:00Z","agent_type":"researcher","run_id":"r","details":{}}`
	)

	tests := []struct {
		name     string
		lines    []string
		exited   bool
		exitCode int
		want     string
	}{
		{
			name:     "errored by pipeline_complete status=error",
			lines:    []string{evNormal, evCompleteError},
			exited:   true,
			exitCode: 1,
			want:     TerminalErrored,
		},
		{
			name:     "errored by pipeline_complete status=halted",
			lines:    []string{evNormal, evCompleteHalted},
			exited:   true,
			exitCode: 0,
			want:     TerminalErrored,
		},
		{
			name:     "errored by agent_error halt",
			lines:    []string{evNormal, evAgentError},
			exited:   true,
			exitCode: 0,
			want:     TerminalErrored,
		},
		{
			name:     "errored by nonzero exit with no terminal event",
			lines:    []string{evNormal},
			exited:   true,
			exitCode: 2,
			want:     TerminalErrored,
		},
		{
			name:     "escalated: no publish verdict and a file_escalated event",
			lines:    []string{evNormal, evEscalated, evCompleteOK},
			exited:   true,
			exitCode: 0,
			want:     TerminalEscalated,
		},
		{
			name:     "complete: published after approval",
			lines:    []string{evVerdictPublish, evGatePresented, evApprovalDecision, evPublished, evCompleteOK},
			exited:   true,
			exitCode: 0,
			want:     TerminalComplete,
		},
		{
			name:     "complete: nothing escalated, clean exit",
			lines:    []string{evNormal, evCompleteOK},
			exited:   true,
			exitCode: 0,
			want:     TerminalComplete,
		},
		{
			name:     "escalation events but a publish verdict present is not escalated",
			lines:    []string{evVerdictPublish, evEscalated, evCompleteOK},
			exited:   true,
			exitCode: 0,
			want:     TerminalComplete,
		},
		{
			name:     "nonzero exit dominates even with a clean pipeline_complete",
			lines:    []string{evNormal, evCompleteOK},
			exited:   true,
			exitCode: 1,
			want:     TerminalErrored,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			writeLog(t, dir, tc.lines...)
			got := reconcileTerminalStatus(dir, tc.exited, tc.exitCode)
			if got != tc.want {
				t.Errorf("reconcileTerminalStatus = %q, want %q", got, tc.want)
			}
		})
	}
}

// While the runner has not yet exited, the reconciled status is empty so the
// SSE hub does not prematurely settle the Run (Requirement 3.7: terminal on the
// first signal to arrive — here neither has arrived).
func TestReconcileTerminalStatusNotExitedIsEmpty(t *testing.T) {
	dir := t.TempDir()
	writeLog(t, dir,
		`{"event_type":"approval_gate_presented","timestamp":"2026-06-02T10:00:01Z","agent_type":"editor_in_chief","run_id":"r","details":{"files_pending_approval":["post.md"]}}`,
	)
	if got := reconcileTerminalStatus(dir, false, 0); got != "" {
		t.Errorf("reconcileTerminalStatus(active) = %q, want empty", got)
	}
}

// TerminalStatus returns "" for an active Run and the reconciled status once the
// completion watch observes the runner exit. A nonzero exit with no terminal
// event reconciles to Errored (Requirement 3.6) without any real subprocess.
func TestManagerTerminalStatusReflectsExit(t *testing.T) {
	root := t.TempDir()

	// release lets the test control when the simulated runner "exits".
	var wg sync.WaitGroup
	wg.Add(1)
	release := make(chan error, 1)

	m := New(root, func(cmd *exec.Cmd) error { return nil },
		fixedID("term1"),
		WithCompletionWatch(func(cmd *exec.Cmd) error {
			defer wg.Done()
			return <-release // block until the test signals an exit
		}),
	)

	h, err := m.Start(validReq())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}

	// While active, status is empty (not yet terminal).
	if got := m.TerminalStatus(h.RunID); got != "" {
		t.Errorf("TerminalStatus(active) = %q, want empty", got)
	}

	// Write a log with a normal event but no terminal pipeline_complete, then
	// simulate a nonzero exit.
	writeLog(t, h.OutputDir,
		`{"event_type":"agent_completed","timestamp":"2026-06-02T10:00:00Z","agent_type":"researcher","run_id":"term1","details":{}}`,
	)
	release <- errors.New("exit status 1")
	wg.Wait()

	if got := m.TerminalStatus(h.RunID); got != TerminalErrored {
		t.Errorf("TerminalStatus(after nonzero exit) = %q, want %q", got, TerminalErrored)
	}
}

// A clean exit (nil error) with a successful pipeline_complete reconciles to
// Complete.
func TestManagerTerminalStatusCleanExit(t *testing.T) {
	root := t.TempDir()
	var wg sync.WaitGroup
	wg.Add(1)

	m := New(root, func(cmd *exec.Cmd) error { return nil },
		fixedID("term2"),
		WithCompletionWatch(func(cmd *exec.Cmd) error { defer wg.Done(); return nil }),
	)

	h, err := m.Start(validReq())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	wg.Wait()

	writeLog(t, h.OutputDir,
		`{"event_type":"pipeline_complete","timestamp":"2026-06-02T10:00:04Z","agent_type":"editor_in_chief","run_id":"term2","details":{"status":"complete"}}`,
	)

	if got := m.TerminalStatus(h.RunID); got != TerminalComplete {
		t.Errorf("TerminalStatus(clean exit) = %q, want %q", got, TerminalComplete)
	}
}
