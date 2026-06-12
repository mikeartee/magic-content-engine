package run

import (
	"bufio"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"
)

// ErrNoGate is returned by Decide when no Approval_Gate is currently awaiting a
// decision for the active Run. The HTTP server maps it to 409 conflict
// (Requirement 12.4).
var ErrNoGate = errors.New("run: no approval gate is currently waiting")

// decisionFilename is the control file the Console writes and the Bullpen
// approval_fn polls, reads, and deletes. It must match
// control_file_gate.DECISION_FILENAME on the Python side (Requirement 2).
const decisionFilename = "approval-decision.json"

// agentLogFilename is the append-only event stream the Bullpen pipeline writes.
// The Console derives gate state from it (Requirement 2/3).
const agentLogFilename = "agent-log.jsonl"

// ApprovalDecision is the schema of approval-decision.json (Requirement 2.3). It
// mirrors what the Python poller reads: a decision of "approved" or "rejected",
// an ISO 8601 decided_at, and the active run's id.
type ApprovalDecision struct {
	Decision  string `json:"decision"`   // "approved" | "rejected"
	DecidedAt string `json:"decided_at"` // ISO 8601, set by the Console
	RunID     string `json:"run_id"`
}

// Decide records the human approval decision for the active Run by writing
// approval-decision.json atomically into the run directory (Requirement 2.2,
// 2.3). It returns ErrNoGate when no Run is active or the active Run's log does
// not currently show an unresolved approval_gate_presented event, so a click
// with no waiting gate is a 409 rather than a spurious decision file
// (Requirement 12.4).
func (m *Manager) Decide(approved bool) error {
	h, ok := m.Active()
	if !ok {
		return ErrNoGate
	}
	if !gateOpen(h.OutputDir) {
		return ErrNoGate
	}

	decision := "rejected"
	if approved {
		decision = "approved"
	}
	payload, err := json.Marshal(ApprovalDecision{
		Decision:  decision,
		DecidedAt: time.Now().UTC().Format(time.RFC3339),
		RunID:     h.RunID,
	})
	if err != nil {
		return err
	}
	return atomicWriteFile(filepath.Join(h.OutputDir, decisionFilename), payload)
}

// gateOpen reports whether the run directory's agent-log.jsonl currently shows
// an approval_gate_presented event that has not been followed by a resolving
// event (approval_decision, approval_rejected, or pipeline_complete). State is
// derived from the file so it stays consistent with the Bullpen's view across
// the process boundary.
func gateOpen(outputDir string) bool {
	f, err := os.Open(filepath.Join(outputDir, agentLogFilename))
	if err != nil {
		return false
	}
	defer f.Close()

	open := false
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		var ev struct {
			EventType string `json:"event_type"`
		}
		if err := json.Unmarshal(scanner.Bytes(), &ev); err != nil {
			continue // tolerate a partially written trailing line
		}
		switch ev.EventType {
		case "approval_gate_presented":
			open = true
		case "approval_decision", "approval_rejected", "pipeline_complete":
			open = false
		}
	}
	return open
}

// atomicWriteFile writes data to a ".tmp" sibling of path and renames it into
// place, so the Bullpen poller never observes a partially written decision file
// (Requirement 2.2).
func atomicWriteFile(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}
