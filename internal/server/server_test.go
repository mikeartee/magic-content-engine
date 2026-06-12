package server_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// newTestServer builds the server handler with a minimal in-memory UI so the
// server package tests do not depend on the embedded web assets.
func newTestServer(t *testing.T) http.Handler {
	t.Helper()
	ui := fstest.MapFS{
		"index.html": &fstest.MapFile{
			Data: []byte("<!doctype html><title>Bullpen Console</title>"),
		},
	}
	return server.New(ui).Routes()
}

// Requirement 5.3: GET /api/health returns {"status":"ok"} with 200.
func TestHealthReturnsOK(t *testing.T) {
	h := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/api/health", nil)
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not valid JSON: %v (%q)", err, rec.Body.String())
	}
	if body["status"] != "ok" {
		t.Errorf("status field = %q, want \"ok\"", body["status"])
	}
}

// Requirement 12.2: a failing API request returns the JSON error shape
// {"error": <code>, "detail": <message>}.
func TestUnknownAPIReturnsJSONErrorShape(t *testing.T) {
	h := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/api/does-not-exist", nil)
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not valid JSON: %v (%q)", err, rec.Body.String())
	}
	code, ok := body["error"].(string)
	if !ok || code == "" {
		t.Errorf("missing/empty \"error\" code in %v", body)
	}
	if _, ok := body["detail"].(string); !ok {
		t.Errorf("missing \"detail\" message in %v", body)
	}
}

// Requirement 12.5 / Requirement 5.1: GET / serves the embedded UI.
func TestRootServesEmbeddedUI(t *testing.T) {
	h := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.Contains(ct, "html") {
		t.Errorf("Content-Type = %q, want HTML", ct)
	}
	if !strings.Contains(rec.Body.String(), "Bullpen Console") {
		t.Errorf("body did not contain the embedded UI marker: %q", rec.Body.String())
	}
}

// Requirement 12.2: UI endpoints return HTML errors, not the JSON error shape.
func TestUIErrorIsNotJSON(t *testing.T) {
	h := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/no-such-page", nil)
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); strings.HasPrefix(ct, "application/json") {
		t.Errorf("UI 404 Content-Type = %q, want a non-JSON (HTML) response", ct)
	}
}

// Requirement 12.1: the server binds exclusively to the 127.0.0.1 loopback
// interface and never 0.0.0.0.
func TestLoopbackBindingConfigured(t *testing.T) {
	addr := server.ListenAddr(8765)
	if !strings.HasPrefix(addr, "127.0.0.1:") {
		t.Errorf("ListenAddr(8765) = %q, want a 127.0.0.1 loopback address", addr)
	}
	if strings.Contains(addr, "0.0.0.0") {
		t.Errorf("ListenAddr(8765) = %q, must never bind 0.0.0.0", addr)
	}
}

// The server handler builds and is usable as an http.Handler.
func TestRoutesBuilds(t *testing.T) {
	if h := newTestServer(t); h == nil {
		t.Fatal("Routes() returned nil handler")
	}
}
