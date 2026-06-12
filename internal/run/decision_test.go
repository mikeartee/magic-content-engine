package run

import (
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// noopStarter is a RunnerStarter that never spawns a real process, so the
// Manager records an active Run without launching python. The decision tests
// then write agent-log.jsonl by hand to control the observed gate state.
func noopStarter(*exec.Cmd) error { return nil }

// writeAgentLog writes the given JSONL lines as agent-log.jsonl in dir.
func writeAgentLog(t *testing.T, dir string, lines ...string) {
	t.Helper()
	path := filepath.Join(dir, "agent-log.jsonl")
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o644); err != nil {
		t.Fatalf("write agent-log.jsonl: %v", err)
	}
}

const (
	gatePresentedLine = `{"event_type":"approval_gate_presented","timestamp":"2026-06-02T10:00:05Z","agent_type":"editor_in_chief","run_id":"r","details":{"files_pending_approval":["post.md"]}}`
	gateResolvedLine  = `{"event_type":"approval_decision","timestamp":"2026-06-02T10:00:06Z","agent_type":"editor_in_chief","run_id":"r","details":{"approved":true}}`
	plainEventLine    = `{"event_type":"agent_completed","timestamp":"2026-06-02T10:00:01Z","agent_type":"writer","run_id":"r","details":{}}`
)

// startActiveRun starts a Run with a no-op starter and returns the Manager plus
// its handle so the test can manipulate the run directory's agent-log.jsonl and
// then call Decide.
func startActiveRun(t *testing.T, root, id string) (*Manager, RunHandle) {
	t.Helper()
	m := New(root, noopStarter, fixedID(id))
	h, err := m.Start(validReq())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	return m, h
}

// Requirement 2.4 / 12.4: Decide with no active Run returns ErrNoGate (the
// server maps this to HTTP 409).
func TestDecideNoActiveRunReturnsErrNoGate(t *testing.T) {
	m := New(t.TempDir(), noopStarter, fixedID("x"))
	if err := m.Decide(true); !errors.Is(err, ErrNoGate) {
		t.Fatalf("Decide with no active run err = %v, want ErrNoGate", err)
	}
}

// Requirement 12.4: an active Run whose log shows no approval_gate_presented is
// not awaiting a decision, so Decide returns ErrNoGate.
func TestDecideNoGateEventReturnsErrNoGate(t *testing.T) {
	root := t.TempDir()
	m, h := startActiveRun(t, root, "run1")
	writeAgentLog(t, h.OutputDir, plainEventLine)
	if err := m.Decide(true); !errors.Is(err, ErrNoGate) {
		t.Fatalf("Decide with no gate event err = %v, want ErrNoGate", err)
	}
	if _, err := os.Stat(filepath.Join(h.OutputDir, "approval-decision.json")); !os.IsNotExist(err) {
		t.Errorf("no decision file should be written when no gate is open (stat err=%v)", err)
	}
}

// Requirement 12.4: once the gate has been resolved (a resolving event follows
// approval_gate_presented), Decide again returns ErrNoGate.
func TestDecideAfterGateResolvedReturnsErrNoGate(t *testing.T) {
	root := t.TempDir()
	m, h := startActiveRun(t, root, "run1")
	writeAgentLog(t, h.OutputDir, gatePresentedLine, gateResolvedLine)
	if err := m.Decide(true); !errors.Is(err, ErrNoGate) {
		t.Fatalf("Decide after gate resolved err = %v, want ErrNoGate", err)
	}
}

// Requirement 2.2 / 2.3: an approved Decide writes approval-decision.json with
// decision="approved", a run_id matching the active Run, and an ISO 8601
// decided_at, written atomically (no .tmp sibling left behind).
func TestDecideApprovedWritesAtomicDecisionFile(t *testing.T) {
	root := t.TempDir()
	m, h := startActiveRun(t, root, "run-approve")
	writeAgentLog(t, h.OutputDir, gatePresentedLine)

	if err := m.Decide(true); err != nil {
		t.Fatalf("Decide(approved) err = %v, want nil", err)
	}

	decision := readDecision(t, h.OutputDir)
	if decision.Decision != "approved" {
		t.Errorf("decision = %q, want approved", decision.Decision)
	}
	if decision.RunID != "run-approve" {
		t.Errorf("run_id = %q, want run-approve", decision.RunID)
	}
	if decision.DecidedAt == "" {
		t.Error("decided_at is empty, want an ISO 8601 timestamp")
	}
	if _, err := os.Stat(filepath.Join(h.OutputDir, "approval-decision.json.tmp")); !os.IsNotExist(err) {
		t.Errorf("temp file left behind, atomic rename incomplete (stat err=%v)", err)
	}
}

// Requirement 2.2 / 2.3: a rejected Decide writes decision="rejected".
func TestDecideRejectedWritesDecisionFile(t *testing.T) {
	root := t.TempDir()
	m, h := startActiveRun(t, root, "run-reject")
	writeAgentLog(t, h.OutputDir, gatePresentedLine)

	if err := m.Decide(false); err != nil {
		t.Fatalf("Decide(rejected) err = %v, want nil", err)
	}
	decision := readDecision(t, h.OutputDir)
	if decision.Decision != "rejected" {
		t.Errorf("decision = %q, want rejected", decision.Decision)
	}
}

// readDecision parses approval-decision.json from dir.
func readDecision(t *testing.T, dir string) ApprovalDecision {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(dir, "approval-decision.json"))
	if err != nil {
		t.Fatalf("read approval-decision.json: %v", err)
	}
	var d ApprovalDecision
	if err := json.Unmarshal(raw, &d); err != nil {
		t.Fatalf("decision file not valid JSON: %v (%q)", err, raw)
	}
	return d
}
