package server_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/run"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// TestMain lets this test binary double as the headless-runner *stub* the
// run.Manager spawns. When the manager re-executes this binary it appends the
// runner argv (notably --output-dir); detecting that here — before the testing
// framework parses its own flags — lets the process act as the stub and exit,
// rather than running the suite recursively. A normal `go test` invocation
// carries no --output-dir, so it falls through to m.Run().
func TestMain(m *testing.M) {
	if dir, ok := stubRunnerOutputDir(os.Args); ok {
		runStubRunner(os.Args, dir) // writes events then os.Exit(0)
		return
	}
	os.Exit(m.Run())
}

// stubRunnerOutputDir reports the --output-dir value if the argv looks like a
// runner invocation (i.e. the manager spawned us as the stub runner).
func stubRunnerOutputDir(argv []string) (string, bool) {
	v := argValue(argv, "--output-dir")
	return v, v != ""
}

// argValue returns the value following flag in argv, or "" if absent.
func argValue(argv []string, flag string) string {
	for i := 0; i < len(argv)-1; i++ {
		if argv[i] == flag {
			return argv[i+1]
		}
	}
	return ""
}

// runStubRunner is the deterministic stand-in for scripts/run_headless.py: it
// writes a handful of agent-log.jsonl events (including a deliberate duplicate
// to exercise dedup) into the run directory and exits 0. It performs no AWS
// call and reads no credentials. The manager's completion watch sees this
// process exit and marks the Run inactive, which lets the SSE hub emit its
// single synthetic terminal frame.
//
// dir is the per-run directory passed by the manager as --output-dir, i.e.
// output/<run_id>/, which is exactly where the SSE hub tails agent-log.jsonl.
func runStubRunner(argv []string, dir string) {
	runID := argValue(argv, "--run-id")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		os.Exit(1)
	}
	f, err := os.OpenFile(filepath.Join(dir, "agent-log.jsonl"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		os.Exit(1)
	}
	write := func(eventType, agent, ts string) {
		line := fmt.Sprintf(
			`{"event_type":%q,"timestamp":%q,"agent_type":%q,"run_id":%q,"details":{}}`,
			eventType, ts, agent, runID)
		_, _ = io.WriteString(f, line+"\n")
		_ = f.Sync()
		// Small pause so a concurrently-attached SSE client streams these
		// events live rather than only via replay.
		time.Sleep(10 * time.Millisecond)
	}
	write("agent_started", "researcher", "2026-06-02T10:00:00Z")
	write("agent_completed", "researcher", "2026-06-02T10:00:01Z")
	// Deliberate duplicate of the first event (same timestamp|type|agent): the
	// hub must dedup it so it renders exactly once.
	write("agent_started", "researcher", "2026-06-02T10:00:00Z")
	write("agent_completed", "writer", "2026-06-02T10:00:02Z")
	_ = f.Close()
	os.Exit(0)
}

// streamRunStatus opens an SSE connection to GET /api/run/status?run_id= and
// reads the whole stream to completion. The Run completes (the stub exits, the
// manager marks it inactive) so the hub emits its terminal frame and closes the
// response, which surfaces here as a clean EOF. The context timeout guards
// against a hang if no actor ever terminates the stream.
func streamRunStatus(t *testing.T, baseURL, runID string, timeout time.Duration) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		baseURL+"/api/run/status?run_id="+runID, nil)
	if err != nil {
		t.Fatalf("build status request: %v", err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("open SSE stream: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status stream code = %d, want 200", resp.StatusCode)
	}
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read SSE stream: %v", err)
	}
	return string(data)
}

// postStartRun clicks Run: POST /api/run, asserts 202, returns the run_id.
func postStartRun(t *testing.T, baseURL, body string) string {
	t.Helper()
	resp, err := http.Post(baseURL+"/api/run", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST /api/run: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		raw, _ := io.ReadAll(resp.Body)
		t.Fatalf("POST /api/run code = %d, want 202 (%s)", resp.StatusCode, raw)
	}
	var out struct {
		RunID string `json:"run_id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode run_id: %v", err)
	}
	if out.RunID == "" {
		t.Fatal("POST /api/run returned an empty run_id")
	}
	return out.RunID
}

// assertSingleRenderTimeline asserts the streamed timeline rendered each event
// exactly once (the duplicated agent_started collapses to one), included the
// later writer event, and ended in exactly one terminal frame.
func assertSingleRenderTimeline(t *testing.T, body string) {
	t.Helper()
	if got := strings.Count(body, `"event_type":"agent_started"`); got != 1 {
		t.Errorf("agent_started rendered %d times, want exactly 1 (dedup failed)\n%s", got, body)
	}
	if got := strings.Count(body, `"agent_type":"writer"`); got != 1 {
		t.Errorf("writer event rendered %d times, want exactly 1\n%s", got, body)
	}
	if got := strings.Count(body, "event: pipeline_complete"); got != 1 {
		t.Errorf("terminal frame count = %d, want exactly 1\n%s", got, body)
	}
}

func e2eTestUI() fstest.MapFS {
	return fstest.MapFS{
		"index.html": &fstest.MapFile{Data: []byte("<!doctype html><title>Bullpen Console</title>")},
	}
}

// TestEndToEndRunSpawnsRunnerAndStreamsEvents is the tracer bullet: POST
// /api/run spawns the (stub) headless runner, whose agent-log.jsonl events
// stream live through GET /api/run/status — each rendered once — ending in a
// single terminal frame. A second connection (a browser refresh mid/after run)
// replays the timeline without duplication and still shows one terminal frame.
//
// Requirements 1, 7, 10 end-to-end. No AWS, no network beyond loopback.
func TestEndToEndRunSpawnsRunnerAndStreamsEvents(t *testing.T) {
	root := t.TempDir()

	rm := run.New(root, run.DefaultStarter,
		// Re-execute this very test binary as the stub runner; the manager's
		// fixed --run-id/--topic/--outputs/--output-dir argv is read by
		// runStubRunner via TestMain.
		run.WithPython(os.Args[0], "stub-runner"),
		// The completion watch flips the Run inactive when the stub exits so
		// the SSE hub knows to emit its single terminal frame.
		run.WithCompletionWatch(func(c *exec.Cmd) error { return c.Wait() }),
	)

	srv := server.New(e2eTestUI(),
		server.WithOutputDir(root),
		server.WithActiveProbe(func(runID string) bool {
			h, ok := rm.Active()
			return ok && h.RunID == runID
		}),
		// Tight SSE timing so the test runs fast and deterministically.
		server.WithSSETiming(15*time.Millisecond, 2),
	)
	srv.SetRunManager(rm)

	ts := httptest.NewServer(srv.Routes())
	defer ts.Close()

	// 1. Clicking Run starts a real (stub) pipeline Run.
	runID := postStartRun(t, ts.URL, `{"topic":"AgentCore in Sydney","outputs":["all"]}`)

	// 2. Events stream live, each rendered once, ending in one terminal frame.
	body := streamRunStatus(t, ts.URL, runID, 30*time.Second)
	assertSingleRenderTimeline(t, body)

	// 3. Browser refresh: a second connection replays without duplication and
	//    still shows a single terminal frame.
	refresh := streamRunStatus(t, ts.URL, runID, 30*time.Second)
	assertSingleRenderTimeline(t, refresh)

	// 4. The runner actually wrote the log at the SSE-resolved path
	//    (output/<run_id>/agent-log.jsonl) — proving the spawn glue lines up.
	logPath := filepath.Join(root, runID, "agent-log.jsonl")
	if _, err := os.Stat(logPath); err != nil {
		t.Fatalf("runner did not write agent-log.jsonl at %s: %v", logPath, err)
	}
}
