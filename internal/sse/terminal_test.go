package sse_test

import (
	"context"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/sse"
)

// streamWithResolver runs a stream to completion (isActive false) with an
// optional terminal-status resolver and returns the raw body.
func streamWithResolver(t *testing.T, h *sse.Hub, logPath string, resolver func() string) string {
	t.Helper()
	rec := httptest.NewRecorder()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := h.Stream(ctx, rec, logPath, func() bool { return false }, resolver); err != nil {
		t.Fatalf("Stream error: %v", err)
	}
	return rec.Body.String()
}

// Requirement 3.5/3.6: when the reconciler reports an errored outcome, the
// single synthetic terminal frame carries status "error" rather than
// "complete", so the client renders the Errored terminal state. The frame is
// still emitted exactly once (Requirement 1.7, 3.2).
func TestTerminalFrameCarriesErroredStatus(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent-log.jsonl")
	writeLines(t, path, []string{
		`{"event_type":"agent_completed","timestamp":"2026-06-02T10:00:00Z","agent_type":"researcher","run_id":"r1","details":{}}`,
	})

	body := streamWithResolver(t, fastHub(), path, func() string { return "error" })

	if n := strings.Count(body, terminalMarker); n != 1 {
		t.Errorf("terminal frame count = %d, want exactly 1; body=%q", n, body)
	}
	if !strings.Contains(body, `data: {"status":"error"}`) {
		t.Errorf("terminal frame did not carry status error; body=%q", body)
	}
	if strings.Contains(body, `data: {"status":"complete"}`) {
		t.Errorf("terminal frame carried complete despite errored resolver; body=%q", body)
	}
}

// Requirement 3.4: an escalated outcome is carried as status "escalated".
func TestTerminalFrameCarriesEscalatedStatus(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent-log.jsonl")
	writeLines(t, path, nil)

	body := streamWithResolver(t, fastHub(), path, func() string { return "escalated" })

	if !strings.Contains(body, `data: {"status":"escalated"}`) {
		t.Errorf("terminal frame did not carry status escalated; body=%q", body)
	}
}

// A nil resolver (and an empty-string resolver result) keep the default
// "complete" terminal frame, preserving the existing behaviour (Requirement 1.7).
func TestTerminalFrameDefaultsToComplete(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent-log.jsonl")
	writeLines(t, path, nil)

	// nil resolver
	body := streamWithResolver(t, fastHub(), path, nil)
	if !strings.Contains(body, terminalPayload) {
		t.Errorf("nil resolver: terminal frame not complete; body=%q", body)
	}

	// empty-string resolver result falls back to complete
	body2 := streamWithResolver(t, fastHub(), path, func() string { return "" })
	if !strings.Contains(body2, terminalPayload) {
		t.Errorf("empty resolver: terminal frame not complete; body=%q", body2)
	}
}
