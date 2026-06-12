package run

import (
	"bufio"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
)

// Reconciled terminal statuses carried by the SSE synthetic terminal frame. The
// Console settles into exactly one of these per Run (Requirement 3.2). The empty
// string means "not yet terminal" (the runner has not exited and no terminal
// event has been observed). These string values are what the client reads from
// the `pipeline_complete` frame's `status` field.
const (
	TerminalComplete  = "complete"  // happy path: published or finished cleanly
	TerminalEscalated = "escalated" // no publish verdict + a file_escalated event
	TerminalErrored   = "error"     // agent_error / status error|halted / nonzero exit
)

// exitResult records how the runner subprocess finished, captured by the
// completion watch so the terminal status can reconcile the exit code against
// the terminal event (Requirement 3.7).
type exitResult struct {
	exited   bool
	exitCode int
}

// TerminalStatus returns the reconciled terminal status for runID, or "" while
// the Run is still active (its runner has not yet exited). Once the completion
// watch observes the exit, the status reconciles the subprocess exit code with
// the agent log: a nonzero exit, an agent_error, or a pipeline_complete of
// status error/halted yields Errored; no publish verdict with a file_escalated
// event yields Escalated; otherwise Complete (Requirement 3.4, 3.5, 3.6, 3.7).
func (m *Manager) TerminalStatus(runID string) string {
	m.mu.Lock()
	outputDir := ""
	if m.handle.RunID == runID {
		outputDir = m.handle.OutputDir
	}
	res, ok := m.exits[runID]
	m.mu.Unlock()

	if outputDir == "" || !ok || !res.exited {
		return ""
	}
	return reconcileTerminalStatus(outputDir, res.exited, res.exitCode)
}

// reconcileTerminalStatus derives the single terminal status from the run
// directory's agent-log.jsonl and the runner's exit outcome. It is pure (reads
// only files) so it is unit-testable with crafted log contents and a simulated
// exit code, with no real subprocess (Requirement 3.2, 3.4, 3.5, 3.6).
func reconcileTerminalStatus(outputDir string, exited bool, exitCode int) string {
	if !exited {
		return ""
	}

	var (
		sawAgentError     bool
		sawPipelineError  bool // pipeline_complete with status error|halted
		sawPublishVerdict bool
		sawFileEscalated  bool
	)

	f, err := os.Open(filepath.Join(outputDir, agentLogFilename))
	if err == nil {
		defer f.Close()
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
		for scanner.Scan() {
			var ev struct {
				EventType string `json:"event_type"`
				Details   struct {
					Status  string `json:"status"`
					Verdict string `json:"verdict"`
				} `json:"details"`
			}
			if err := json.Unmarshal(scanner.Bytes(), &ev); err != nil {
				continue // tolerate a partially written trailing line
			}
			switch ev.EventType {
			case "agent_error":
				sawAgentError = true
			case "pipeline_complete":
				if ev.Details.Status == "error" || ev.Details.Status == "halted" {
					sawPipelineError = true
				}
			case "verdict":
				if ev.Details.Verdict == "publish" {
					sawPublishVerdict = true
				}
			case "file_escalated":
				sawFileEscalated = true
			}
		}
	}

	// Errored dominates: a nonzero exit (Requirement 3.5/3.6), an agent_error
	// halt, or a terminal event reporting error/halted (Requirement 3.5).
	switch {
	case exitCode != 0:
		return TerminalErrored
	case sawAgentError:
		return TerminalErrored
	case sawPipelineError:
		return TerminalErrored
	case !sawPublishVerdict && sawFileEscalated:
		return TerminalEscalated
	default:
		return TerminalComplete
	}
}

// exitCodeOf maps a RunnerWaiter result to an exit code: nil is 0, an
// *exec.ExitError carries the real code, and any other error is treated as a
// generic nonzero exit (1) so a failed wait still reconciles to Errored.
func exitCodeOf(err error) int {
	if err == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		if code := ee.ExitCode(); code != 0 {
			return code
		}
		return 1
	}
	return 1
}
