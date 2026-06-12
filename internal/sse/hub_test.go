package sse_test

import (
	"context"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/sse"
)

// fastHub returns a Hub tuned for tests: tiny poll interval so a stream that is
// not active terminates in milliseconds rather than seconds.
func fastHub() *sse.Hub {
	return &sse.Hub{
		PollInterval:   2 * time.Millisecond,
		IdleTicksLimit: 2,
	}
}

// dataFrame is "data: " + raw line; terminalFrame is the synthetic completion.
const (
	dataPrefix      = "data: "
	terminalMarker  = "event: pipeline_complete"
	terminalPayload = `data: {"status":"complete"}`
)

// collectStream runs a stream to completion (isActive always false, so it ends
// after replay + the synthetic terminal frame) and returns the raw JSON
// payloads of every data frame, the number of terminal frames, and the headers.
func collectStream(t *testing.T, h *sse.Hub, logPath string) (payloads []string, terminals int, hdr http.Header) {
	t.Helper()
	rec := httptest.NewRecorder()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := h.Stream(ctx, rec, logPath, func() bool { return false }); err != nil {
		t.Fatalf("Stream returned error: %v", err)
	}
	payloads, terminals = parseFrames(rec.Body.String())
	return payloads, terminals, rec.Header()
}

// parseFrames splits an SSE body into data payloads (raw JSON strings) and a
// count of synthetic terminal frames.
func parseFrames(body string) (payloads []string, terminals int) {
	for _, frame := range strings.Split(body, "\n\n") {
		frame = strings.TrimRight(frame, "\n")
		if frame == "" {
			continue
		}
		if strings.Contains(frame, terminalMarker) {
			terminals++
			continue
		}
		if strings.HasPrefix(frame, dataPrefix) {
			payloads = append(payloads, strings.TrimPrefix(frame, dataPrefix))
		}
	}
	return payloads, terminals
}

// keyOf parses a raw JSON line into a LogEvent and returns its DedupKey, the
// same identity the hub uses to suppress duplicates.
func keyOf(line string) string {
	var e sse.LogEvent
	if err := json.Unmarshal([]byte(line), &e); err != nil {
		return line // fallback identity, matches hub behaviour for invalid JSON
	}
	return e.DedupKey()
}

// distinctKeys returns the set of DedupKeys present in lines.
func distinctKeys(lines []string) map[string]struct{} {
	set := make(map[string]struct{})
	for _, l := range lines {
		if strings.TrimSpace(l) == "" {
			continue
		}
		set[keyOf(l)] = struct{}{}
	}
	return set
}

func keySet(payloads []string) map[string]struct{} {
	set := make(map[string]struct{})
	for _, p := range payloads {
		set[keyOf(p)] = struct{}{}
	}
	return set
}

func writeLines(t *testing.T, path string, lines []string) {
	t.Helper()
	var b strings.Builder
	for _, l := range lines {
		b.WriteString(l)
		b.WriteByte('\n')
	}
	if err := os.WriteFile(path, []byte(b.String()), 0o644); err != nil {
		t.Fatalf("write lines: %v", err)
	}
}

func appendLines(t *testing.T, path string, lines []string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0o644)
	if err != nil {
		t.Fatalf("open append: %v", err)
	}
	defer f.Close()
	for _, l := range lines {
		if _, err := f.WriteString(l + "\n"); err != nil {
			t.Fatalf("append: %v", err)
		}
	}
}

// logSeq is a generated sequence of agent-log.jsonl lines drawn from a small
// pool of timestamps/types/agents so that duplicate DedupKeys actually occur.
type logSeq struct {
	lines []string
}

func (logSeq) Generate(r *rand.Rand, size int) reflect.Value {
	timestamps := []string{"2026-06-02T10:00:00Z", "2026-06-02T10:00:01Z", "2026-06-02T10:00:02Z"}
	types := []string{"agent_invoked", "agent_completed", "verdict", "weird_unknown_type"}
	agents := []string{"researcher", "writer", "subeditor", ""}

	n := r.Intn(size + 1)
	lines := make([]string, 0, n)
	for i := 0; i < n; i++ {
		e := sse.LogEvent{
			EventType: types[r.Intn(len(types))],
			Timestamp: timestamps[r.Intn(len(timestamps))],
			AgentType: agents[r.Intn(len(agents))],
			RunID:     "run-x",
			Details:   map[string]any{"i": i},
		}
		raw, _ := json.Marshal(e)
		lines = append(lines, string(raw))
	}
	return reflect.ValueOf(logSeq{lines: lines})
}

// Requirement 1.1 + 1.2: for any sequence of log lines and any split into two
// connect sessions (a reconnect that replays from offset 0), every distinct
// event by DedupKey is rendered exactly once per stream, none dropped, no
// duplicates.
func TestProperty_DedupAcrossReplaySessions(t *testing.T) {
	prop := func(seq logSeq, split uint16) bool {
		dir := t.TempDir()
		path := filepath.Join(dir, "agent-log.jsonl")

		lines := seq.lines
		k := 0
		if len(lines) > 0 {
			k = int(split) % (len(lines) + 1)
		}

		h := fastHub()

		// Session 1: client connects, log has the first k lines.
		writeLines(t, path, lines[:k])
		p1, term1, _ := collectStream(t, h, path)

		// Session 2: reconnect after more lines have been appended; the hub
		// replays the FULL file from offset 0.
		appendLines(t, path, lines[k:])
		p2, term2, _ := collectStream(t, h, path)

		// No duplicate renders within either stream.
		if len(p1) != len(keySet(p1)) {
			t.Logf("session1 emitted duplicates: %v", p1)
			return false
		}
		if len(p2) != len(keySet(p2)) {
			t.Logf("session2 emitted duplicates: %v", p2)
			return false
		}
		// Each stream renders exactly the distinct set, none dropped.
		if !reflect.DeepEqual(keySet(p1), distinctKeys(lines[:k])) {
			t.Logf("session1 set mismatch")
			return false
		}
		if !reflect.DeepEqual(keySet(p2), distinctKeys(lines)) {
			t.Logf("session2 (full replay) set mismatch")
			return false
		}
		// Exactly one synthetic terminal frame per completed stream.
		return term1 == 1 && term2 == 1
	}

	if err := quick.Check(prop, &quick.Config{MaxCount: 150}); err != nil {
		t.Fatalf("dedup/replay property failed: %v", err)
	}
}

// Requirement 1.3: the response carries the SSE headers.
func TestStreamSetsHeaders(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent-log.jsonl")
	writeLines(t, path, nil)

	_, _, hdr := collectStream(t, fastHub(), path)

	if got := hdr.Get("Content-Type"); got != "text/event-stream" {
		t.Errorf("Content-Type = %q, want text/event-stream", got)
	}
	if got := hdr.Get("Cache-Control"); got != "no-cache" {
		t.Errorf("Cache-Control = %q, want no-cache", got)
	}
	if got := hdr.Get("X-Accel-Buffering"); got != "no" {
		t.Errorf("X-Accel-Buffering = %q, want no", got)
	}
}

// Requirement 1.4: each log line is emitted as a "data: <raw json>" frame.
func TestStreamEmitsRawDataFrame(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent-log.jsonl")
	line := `{"event_type":"agent_completed","timestamp":"2026-06-02T10:00:00Z","agent_type":"writer","run_id":"r1","details":{}}`
	writeLines(t, path, []string{line})

	payloads, _, _ := collectStream(t, fastHub(), path)

	if len(payloads) != 1 {
		t.Fatalf("got %d data frames, want 1: %v", len(payloads), payloads)
	}
	var got, want map[string]any
	_ = json.Unmarshal([]byte(payloads[0]), &got)
	_ = json.Unmarshal([]byte(line), &want)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("emitted payload = %s, want %s", payloads[0], line)
	}
}

// Requirement 1.6: an event whose event_type is outside the known set is
// emitted raw rather than dropped.
func TestUnknownEventTypeEmittedRaw(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent-log.jsonl")
	line := `{"event_type":"totally_unknown_xyz","timestamp":"2026-06-02T10:00:00Z","agent_type":"mystery","run_id":"r1","details":{"k":"v"}}`
	writeLines(t, path, []string{line})

	payloads, _, _ := collectStream(t, fastHub(), path)

	found := false
	for _, p := range payloads {
		if strings.Contains(p, "totally_unknown_xyz") {
			found = true
		}
	}
	if !found {
		t.Errorf("unknown event_type was dropped; payloads = %v", payloads)
	}
}

// Requirement 1.7: exactly one synthetic terminal frame is emitted when the run
// is inactive and the file is idle, and then the stream ends.
func TestSyntheticTerminalFrameOnce(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent-log.jsonl")
	writeLines(t, path, []string{
		`{"event_type":"agent_invoked","timestamp":"2026-06-02T10:00:00Z","agent_type":"writer","run_id":"r1","details":{}}`,
	})

	rec := httptest.NewRecorder()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := fastHub().Stream(ctx, rec, path, func() bool { return false }); err != nil {
		t.Fatalf("Stream error: %v", err)
	}

	body := rec.Body.String()
	if n := strings.Count(body, terminalMarker); n != 1 {
		t.Errorf("terminal frame count = %d, want 1; body=%q", n, body)
	}
	if !strings.Contains(body, terminalPayload) {
		t.Errorf("terminal frame payload missing; body=%q", body)
	}
}

// Requirement 1.8: when the client disconnects (ctx cancelled), the stream
// closes cleanly and does not emit a terminal frame.
func TestCleanCloseOnDisconnect(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent-log.jsonl")
	writeLines(t, path, nil)

	rec := httptest.NewRecorder()
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan error, 1)
	go func() {
		// isActive always true: the stream only ends because of disconnect.
		done <- fastHub().Stream(ctx, rec, path, func() bool { return true })
	}()

	time.Sleep(30 * time.Millisecond)
	cancel()

	select {
	case <-done:
		// returned promptly after cancel — clean close
	case <-time.After(2 * time.Second):
		t.Fatal("Stream did not return after client disconnect")
	}

	if strings.Contains(rec.Body.String(), terminalMarker) {
		t.Errorf("terminal frame emitted on disconnect; want none")
	}
}

// Requirement 1.9: when the log file does not yet exist, the hub creates an
// empty file before tailing so the stream does not error.
func TestMissingLogFileCreated(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nested", "agent-log.jsonl")

	rec := httptest.NewRecorder()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := fastHub().Stream(ctx, rec, path, func() bool { return false }); err != nil {
		t.Fatalf("Stream errored on missing file: %v", err)
	}

	if _, err := os.Stat(path); err != nil {
		t.Errorf("log file was not created: %v", err)
	}
}

// DedupKey is timestamp|event_type|agent_type.
func TestDedupKeyFormat(t *testing.T) {
	e := sse.LogEvent{EventType: "verdict", Timestamp: "2026-06-02T10:00:00Z", AgentType: "subeditor"}
	want := "2026-06-02T10:00:00Z|verdict|subeditor"
	if got := e.DedupKey(); got != want {
		t.Errorf("DedupKey() = %q, want %q", got, want)
	}
	// sanity: fmt import used
	_ = fmt.Sprintf
}
