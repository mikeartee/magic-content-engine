// Package run owns the lifecycle of the single active Bullpen Run. It generates
// the run_id, creates the run directory, spawns the headless Python runner as an
// argument vector (never a shell string, so a free-text topic cannot inject
// commands), enforces single-active-run, and captures the runner's stdout and
// stderr to runner.stderr.log for diagnosis.
//
// This implements Requirement 7 of the bullpen-console-go spec. AWS stays
// entirely on the Python side; this package carries no AWS dependency.
//
// The actual process launch is injected via a RunnerStarter so unit tests can
// supply a fake and never require a real python interpreter. The default
// starter (DefaultStarter) simply calls cmd.Start().
package run

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Sentinel errors returned by Start. Callers (the HTTP server) map these to
// status codes: ErrRunInProgress -> 409, the validation errors -> 422, and a
// *SpawnError -> 500 spawn_failed.
var (
	// ErrRunInProgress is returned when a Run is already active (Requirement 7.3).
	ErrRunInProgress = errors.New("run: a run is already in progress")
	// ErrEmptyTopic is returned when the topic is empty or whitespace-only
	// (Requirement 7.4).
	ErrEmptyTopic = errors.New("run: topic must not be empty")
	// ErrInvalidOutputs is returned when outputs is neither ["all"] nor a subset
	// of the known output set (Requirement 7.4).
	ErrInvalidOutputs = errors.New(`run: outputs must be ["all"] or a subset of blog,youtube,cfp,usergroup,digest`)
)

// validOutputs is the set of selectable pipeline outputs (Requirement 7.4).
var validOutputs = map[string]struct{}{
	"blog":      {},
	"youtube":   {},
	"cfp":       {},
	"usergroup": {},
	"digest":    {},
}

// SpawnError wraps the underlying cause of a failed runner launch so the server
// can distinguish a spawn failure (HTTP 500 spawn_failed, Requirement 7.6) from
// validation and single-active-run errors.
type SpawnError struct {
	Err error
}

func (e *SpawnError) Error() string {
	return "run: failed to spawn headless runner: " + e.Err.Error()
}

// Unwrap exposes the underlying cause for errors.Is/As.
func (e *SpawnError) Unwrap() error { return e.Err }

// StartRequest is the validated input to Start.
type StartRequest struct {
	Topic   string   // free-text, required, non-empty
	Outputs []string // ["all"] or a subset of {blog,youtube,cfp,usergroup,digest}
}

// RunHandle describes a started Run.
type RunHandle struct {
	RunID     string
	OutputDir string // absolute-or-rooted path to output/<run_id>/
	LogPath   string // OutputDir/runner.stderr.log
	StartedAt time.Time

	cmd *exec.Cmd // the headless runner subprocess (nil when injected by a fake)
}

// RunnerStarter launches the prepared command. The default implementation calls
// cmd.Start(); tests inject a fake that records the command and/or returns an
// error to exercise the spawn-failure path.
type RunnerStarter func(cmd *exec.Cmd) error

// RunnerWaiter blocks until the spawned runner exits. Production wiring passes
// (*exec.Cmd).Wait so the Manager learns when a Run finishes and releases the
// single-active slot; tests may inject their own. When no waiter is configured
// the Manager never auto-completes a Run (the historical behaviour relied on by
// the manager unit tests, which inject fake starters that never spawn a real
// process).
type RunnerWaiter func(cmd *exec.Cmd) error

// DefaultStarter starts the subprocess for real. Production wiring uses this.
func DefaultStarter(cmd *exec.Cmd) error { return cmd.Start() }

// Manager owns the single active Run. It is safe for concurrent use.
type Manager struct {
	outputRoot string
	starter    RunnerStarter
	waiter     RunnerWaiter
	python     string
	script     string
	idgen      func() string

	mu     sync.Mutex
	active bool
	handle RunHandle
	exits  map[string]exitResult // per-run terminal exit outcome (Requirement 3.7)
}

// Option customises a Manager.
type Option func(*Manager)

// WithIDGenerator overrides the run_id generator (used by tests for determinism).
func WithIDGenerator(f func() string) Option {
	return func(m *Manager) { m.idgen = f }
}

// WithPython overrides the interpreter and headless-runner script path.
func WithPython(python, script string) Option {
	return func(m *Manager) {
		m.python = python
		m.script = script
	}
}

// WithCompletionWatch makes the Manager observe runner completion: after a
// successful spawn it runs waiter(cmd) on a background goroutine and, when that
// returns, marks the Run inactive so a new Run may start and the SSE hub can
// emit its terminal frame. Production wires (*exec.Cmd).Wait. Without this
// option the Manager never auto-completes a Run.
func WithCompletionWatch(waiter RunnerWaiter) Option {
	return func(m *Manager) { m.waiter = waiter }
}

// New constructs a Manager rooted at outputRoot (the parent of every
// output/<run_id>/ directory), launching runners via starter.
func New(outputRoot string, starter RunnerStarter, opts ...Option) *Manager {
	m := &Manager{
		outputRoot: outputRoot,
		starter:    starter,
		python:     "python",
		script:     "scripts/run_headless.py",
		idgen:      generateRunID,
		exits:      make(map[string]exitResult),
	}
	for _, opt := range opts {
		opt(m)
	}
	return m
}

// Start validates the request, generates a run_id, creates output/<run_id>/,
// spawns the headless runner as an argv vector, and returns a RunHandle. It
// enforces single-active-run, returning ErrRunInProgress when one is already
// active and a *SpawnError when the launch fails (leaving no active Run).
func (m *Manager) Start(req StartRequest) (RunHandle, error) {
	if err := ValidateStartRequest(req); err != nil {
		return RunHandle{}, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	if m.active {
		return RunHandle{}, ErrRunInProgress
	}

	runID := m.idgen()
	outputDir := filepath.Join(m.outputRoot, runID)
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return RunHandle{}, fmt.Errorf("run: create run directory: %w", err)
	}

	logPath := filepath.Join(outputDir, "runner.stderr.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		_ = os.RemoveAll(outputDir)
		return RunHandle{}, fmt.Errorf("run: create runner log: %w", err)
	}

	// Build the command as an argument vector. A free-text topic with shell
	// metacharacters can never be interpreted as a command (Requirement 7.2).
	args := []string{m.script, "--run-id", runID, "--topic", req.Topic, "--outputs"}
	args = append(args, req.Outputs...)
	args = append(args, "--output-dir", outputDir)
	cmd := exec.Command(m.python, args...)

	// Capture stdout and stderr to runner.stderr.log (Requirement 7.5).
	cmd.Stdout = logFile
	cmd.Stderr = logFile

	if err := m.starter(cmd); err != nil {
		// Spawn failed: leave no active Run and clean up the empty dir so a
		// retry starts fresh (Requirement 7.6).
		_ = logFile.Close()
		_ = os.RemoveAll(outputDir)
		return RunHandle{}, &SpawnError{Err: err}
	}

	// The child has inherited the log file descriptor; the parent closes its
	// own copy so the handle is not leaked. The runner keeps writing to its
	// inherited descriptor (Requirement 7.5).
	_ = logFile.Close()

	h := RunHandle{
		RunID:     runID,
		OutputDir: outputDir,
		LogPath:   logPath,
		StartedAt: time.Now().UTC(),
		cmd:       cmd,
	}
	m.active = true
	m.handle = h

	// When a completion watch is configured, learn of the runner's exit so the
	// single-active slot is released and the SSE hub can settle into its
	// terminal frame. The goroutine blocks on m.mu only briefly (after waiter
	// returns), so launching it under the lock is safe.
	if m.waiter != nil {
		go m.awaitCompletion(runID, cmd)
	}
	return h, nil
}

// awaitCompletion blocks until the runner exits, then records the exit outcome
// (so TerminalStatus can reconcile exit vs event, Requirement 3.7) and clears
// the active Run if it is still the one identified by runID (a later Run must
// not be cancelled by an earlier runner's exit).
func (m *Manager) awaitCompletion(runID string, cmd *exec.Cmd) {
	err := m.waiter(cmd)
	m.mu.Lock()
	defer m.mu.Unlock()
	m.exits[runID] = exitResult{exited: true, exitCode: exitCodeOf(err)}
	if m.active && m.handle.RunID == runID {
		m.active = false
	}
}

// Active returns the current RunHandle, or ok=false if no Run is active.
func (m *Manager) Active() (RunHandle, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.active {
		return RunHandle{}, false
	}
	return m.handle, true
}

// ValidateStartRequest reports whether req is a well-formed start request. It is
// exported so the HTTP layer can return 422 before involving the Manager, and is
// also applied defensively inside Start.
func ValidateStartRequest(req StartRequest) error {
	if strings.TrimSpace(req.Topic) == "" {
		return ErrEmptyTopic
	}
	if !validOutputsValue(req.Outputs) {
		return ErrInvalidOutputs
	}
	return nil
}

// validOutputsValue reports whether outputs is exactly ["all"] or a non-empty
// subset of the known output set.
func validOutputsValue(outputs []string) bool {
	if len(outputs) == 0 {
		return false
	}
	if len(outputs) == 1 && outputs[0] == "all" {
		return true
	}
	for _, o := range outputs {
		if _, ok := validOutputs[o]; !ok {
			return false
		}
	}
	return true
}

// generateRunID returns an 8-hex-character run id, matching the Flask Console's
// uuid4().hex[:8] convention.
func generateRunID() string {
	var b [4]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failure is effectively impossible on supported platforms;
		// fall back to a timestamp so a Run can still start.
		return fmt.Sprintf("%08x", time.Now().UnixNano()&0xffffffff)
	}
	return hex.EncodeToString(b[:])
}
