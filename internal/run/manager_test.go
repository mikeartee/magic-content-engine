package run

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
)

// fixedID returns an ID generator that always yields the same id.
func fixedID(id string) Option {
	return WithIDGenerator(func() string { return id })
}

// countingID returns an ID generator that yields run-0, run-1, ... so
// concurrent Starts that race past the active check still get unique dirs.
func countingID() Option {
	var n int64
	return WithIDGenerator(func() string {
		return "run-" + strconv.FormatInt(atomic.AddInt64(&n, 1)-1, 10)
	})
}

func validReq() StartRequest {
	return StartRequest{Topic: "AgentCore in Sydney", Outputs: []string{"all"}}
}

// Requirement 7.1 + 7.2 + 7.5: Start generates a run_id, creates the run dir,
// spawns the headless runner as an argv vector, and wires stdout+stderr to
// runner.stderr.log.
func TestStartCreatesDirAndSpawnsArgvVector(t *testing.T) {
	var captured *exec.Cmd
	root := t.TempDir()
	m := New(root, func(cmd *exec.Cmd) error { captured = cmd; return nil },
		fixedID("abc12345"), WithPython("python", "scripts/run_headless.py"))

	// A topic carrying shell metacharacters must survive intact as a single
	// argv element — proof the spawn is an argument vector, not a shell string.
	topic := "Kiro hooks; rm -rf / && echo pwned"
	h, err := m.Start(StartRequest{Topic: topic, Outputs: []string{"all"}})
	if err != nil {
		t.Fatalf("Start returned error: %v", err)
	}

	if h.RunID != "abc12345" {
		t.Errorf("RunID = %q, want %q", h.RunID, "abc12345")
	}
	wantDir := filepath.Join(root, "abc12345")
	if h.OutputDir != wantDir {
		t.Errorf("OutputDir = %q, want %q", h.OutputDir, wantDir)
	}
	if fi, err := os.Stat(h.OutputDir); err != nil || !fi.IsDir() {
		t.Errorf("output dir not created: stat err=%v", err)
	}
	wantLog := filepath.Join(wantDir, "runner.stderr.log")
	if h.LogPath != wantLog {
		t.Errorf("LogPath = %q, want %q", h.LogPath, wantLog)
	}
	if h.StartedAt.IsZero() {
		t.Error("StartedAt is zero")
	}

	if captured == nil {
		t.Fatal("starter never received a command")
	}
	wantArgs := []string{
		"python", "scripts/run_headless.py",
		"--run-id", "abc12345",
		"--topic", topic,
		"--outputs", "all",
		"--output-dir", wantDir,
	}
	if !reflect.DeepEqual(captured.Args, wantArgs) {
		t.Errorf("argv = %#v, want %#v", captured.Args, wantArgs)
	}
}

// Requirement 7.2: multiple selected outputs are each passed as their own argv
// element after --outputs.
func TestStartPassesEachOutputAsArgvElement(t *testing.T) {
	var captured *exec.Cmd
	m := New(t.TempDir(), func(cmd *exec.Cmd) error { captured = cmd; return nil },
		fixedID("id1"))

	if _, err := m.Start(StartRequest{Topic: "t", Outputs: []string{"blog", "cfp", "digest"}}); err != nil {
		t.Fatalf("Start: %v", err)
	}
	wantTail := []string{"--outputs", "blog", "cfp", "digest", "--output-dir"}
	args := captured.Args
	// Find --outputs and compare the slice through --output-dir.
	idx := indexOf(args, "--outputs")
	if idx < 0 || idx+len(wantTail) > len(args) {
		t.Fatalf("argv missing --outputs run: %#v", args)
	}
	got := args[idx : idx+len(wantTail)]
	if !reflect.DeepEqual(got, wantTail) {
		t.Errorf("outputs argv = %#v, want prefix %#v", got, wantTail)
	}
}

// Requirement 7.5: the runner's stdout and stderr are both directed to
// runner.stderr.log, and the file is created.
func TestStartCapturesStdoutStderrToLog(t *testing.T) {
	var captured *exec.Cmd
	m := New(t.TempDir(), func(cmd *exec.Cmd) error { captured = cmd; return nil },
		fixedID("logid"))

	h, err := m.Start(validReq())
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if _, err := os.Stat(h.LogPath); err != nil {
		t.Fatalf("runner.stderr.log not created: %v", err)
	}
	if captured.Stdout == nil || captured.Stderr == nil {
		t.Fatal("stdout/stderr not wired")
	}
	if captured.Stdout != captured.Stderr {
		t.Error("stdout and stderr should share the same log sink")
	}
}

// Requirement 7.3: a second Start while a Run is active returns ErrRunInProgress
// and does not start a second runner.
func TestSecondStartReturnsErrRunInProgress(t *testing.T) {
	spawns := 0
	m := New(t.TempDir(), func(cmd *exec.Cmd) error { spawns++; return nil },
		countingID())

	if _, err := m.Start(validReq()); err != nil {
		t.Fatalf("first Start: %v", err)
	}
	_, err := m.Start(validReq())
	if !errors.Is(err, ErrRunInProgress) {
		t.Fatalf("second Start err = %v, want ErrRunInProgress", err)
	}
	if spawns != 1 {
		t.Errorf("spawns = %d, want 1 (no second runner)", spawns)
	}
}

// Requirement 7.3: under concurrent Starts exactly one succeeds.
func TestConcurrentStartsSingleActive(t *testing.T) {
	m := New(t.TempDir(), func(cmd *exec.Cmd) error { return nil }, countingID())

	const n = 16
	var wg sync.WaitGroup
	var ok, inProgress int64
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_, err := m.Start(validReq())
			switch {
			case err == nil:
				atomic.AddInt64(&ok, 1)
			case errors.Is(err, ErrRunInProgress):
				atomic.AddInt64(&inProgress, 1)
			default:
				t.Errorf("unexpected err: %v", err)
			}
		}()
	}
	close(start)
	wg.Wait()

	if ok != 1 {
		t.Errorf("successful Starts = %d, want 1", ok)
	}
	if inProgress != n-1 {
		t.Errorf("ErrRunInProgress count = %d, want %d", inProgress, n-1)
	}
}

// Requirement 7.4: an empty/blank topic is rejected before any spawn.
func TestStartEmptyTopicRejected(t *testing.T) {
	spawns := 0
	m := New(t.TempDir(), func(cmd *exec.Cmd) error { spawns++; return nil }, fixedID("x"))

	for _, topic := range []string{"", "   ", "\t\n"} {
		_, err := m.Start(StartRequest{Topic: topic, Outputs: []string{"all"}})
		if !errors.Is(err, ErrEmptyTopic) {
			t.Errorf("topic %q: err = %v, want ErrEmptyTopic", topic, err)
		}
	}
	if spawns != 0 {
		t.Errorf("spawns = %d, want 0", spawns)
	}
	if _, active := m.Active(); active {
		t.Error("a Run is active after validation failure")
	}
}

// Requirement 7.4: outputs must be ["all"] or a subset of the known set.
func TestStartInvalidOutputsRejected(t *testing.T) {
	m := New(t.TempDir(), func(cmd *exec.Cmd) error { return nil }, countingID())

	bad := [][]string{
		nil,
		{},
		{"bogus"},
		{"blog", "nope"},
		{"all", "blog"}, // "all" must stand alone
		{"All"},         // case-sensitive token
	}
	for _, outs := range bad {
		_, err := m.Start(StartRequest{Topic: "t", Outputs: outs})
		if !errors.Is(err, ErrInvalidOutputs) {
			t.Errorf("outputs %#v: err = %v, want ErrInvalidOutputs", outs, err)
		}
	}
}

func TestStartValidOutputSubsetsAccepted(t *testing.T) {
	good := [][]string{
		{"all"},
		{"blog"},
		{"youtube", "cfp"},
		{"blog", "youtube", "cfp", "usergroup", "digest"},
	}
	for _, outs := range good {
		m := New(t.TempDir(), func(cmd *exec.Cmd) error { return nil }, fixedID("g"))
		if _, err := m.Start(StartRequest{Topic: "t", Outputs: outs}); err != nil {
			t.Errorf("outputs %#v rejected: %v", outs, err)
		}
	}
}

// Requirement 7.6: a spawn failure surfaces distinctly, leaves no active Run,
// and removes the freshly created (empty) run directory.
func TestStartSpawnFailureNoActiveRun(t *testing.T) {
	root := t.TempDir()
	m := New(root, func(cmd *exec.Cmd) error { return errors.New("exec: \"python\": not found") },
		fixedID("failrun"))

	_, err := m.Start(validReq())
	var se *SpawnError
	if !errors.As(err, &se) {
		t.Fatalf("err = %v, want *SpawnError", err)
	}
	if se.Unwrap() == nil {
		t.Error("SpawnError should wrap the underlying cause")
	}
	if _, active := m.Active(); active {
		t.Error("a Run is active after spawn failure")
	}
	// A subsequent valid Start should succeed (the failed attempt freed state).
	m2 := New(root, func(cmd *exec.Cmd) error { return nil }, fixedID("okrun"))
	if _, err := m2.Start(validReq()); err != nil {
		t.Fatalf("Start after spawn failure: %v", err)
	}
}

func indexOf(s []string, v string) int {
	for i, e := range s {
		if e == v {
			return i
		}
	}
	return -1
}
