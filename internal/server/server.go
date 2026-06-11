// Package server provides the Bullpen Console HTTP surface: a net/http mux with
// handlers for the API and the embedded UI. It binds loopback-only and is the
// only network entry point of the Console.
//
// Error shape: API endpoints (the /api/... surface) return a JSON body of the
// form {"error": <code>, "detail": <message>}. UI endpoints (GET / and
// /static/...) return HTML errors rather than JSON. This split implements
// Requirement 12.2 of the bullpen-console-go spec.
//
// This package carries no AWS SDK dependency and needs no AWS credentials to
// build the handler or serve /api/health (Requirement 5).
package server

import (
	"encoding/json"
	"errors"
	"io/fs"
	"net"
	"net/http"
	"strconv"

	"github.com/mikeartee/magic-content-engine/console/internal/run"
)

// LoopbackHost is the only interface the Console ever binds to. The listener is
// loopback-only and never network-exposed (Requirement 12.1), so no
// authentication layer is added — a conscious decision, not an oversight.
const LoopbackHost = "127.0.0.1"

// ListenAddr returns the loopback bind address for the given port. It always
// targets 127.0.0.1 and never 0.0.0.0 (Requirement 12.1).
func ListenAddr(port int) string {
	return net.JoinHostPort(LoopbackHost, strconv.Itoa(port))
}

// RunStarter is the slice of the Run_Manager the HTTP server depends on to
// start a Run. It is declared here (consumer-side) so the server stays
// decoupled from the concrete *run.Manager and so handlers can be tested with a
// fake. Later slices may widen this as approval and status wiring land.
type RunStarter interface {
	Start(req run.StartRequest) (run.RunHandle, error)
}

// Server holds the dependencies needed to serve the Console. For this slice the
// dependencies are the embedded UI file system and the run manager; later
// slices add the SSE hub, file service, vault and dev.to publisher.
type Server struct {
	ui   fs.FS      // embedded UI assets (rooted at the static directory)
	runs RunStarter // owns the single active Run; nil until wired
}

// New constructs a Server backed by the given UI file system. The ui argument
// is the embedded static asset tree (index.html and friends). The run manager
// is attached separately via SetRunManager so this constructor keeps the
// signature the skeleton slice (#35) introduced.
func New(ui fs.FS) *Server {
	return &Server{ui: ui}
}

// SetRunManager attaches the Run_Manager dependency used by POST /api/run.
func (s *Server) SetRunManager(rm RunStarter) {
	s.runs = rm
}

// Routes builds the http.ServeMux with every Console route registered. It is
// the single place the URL surface is wired.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()

	// API surface.
	mux.HandleFunc("GET /api/health", s.handleHealth)
	mux.HandleFunc("POST /api/run", s.handleStartRun)
	// Catch-all for any other /api/... path: JSON error shape, never HTML.
	mux.HandleFunc("/api/", s.handleAPINotFound)

	// UI surface. Static assets are served under /static/; everything else
	// falls through to the root handler which serves index.html or an HTML 404.
	mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServerFS(s.ui)))
	mux.HandleFunc("/", s.handleUI)

	return mux
}

// handleHealth implements Requirement 5.3: GET /api/health returns
// {"status":"ok"} as JSON.
func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// startRunRequest is the POST /api/run request body.
type startRunRequest struct {
	Topic   string   `json:"topic"`
	Outputs []string `json:"outputs"`
}

// handleStartRun implements Requirement 7: start a Run.
//
//   - 202 {"run_id": ...} on success
//   - 409 when a Run is already active
//   - 422 on an empty topic, invalid outputs, or a malformed body
//   - 500 {"error":"spawn_failed","detail":...} when the runner fails to spawn
//     (no run_id, no active Run)
func (s *Server) handleStartRun(w http.ResponseWriter, r *http.Request) {
	if s.runs == nil {
		writeJSONError(w, http.StatusInternalServerError, "internal_error",
			"Run manager is not configured.")
		return
	}

	var body startRunRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		// A malformed body cannot describe a valid Run (Requirement 7.4).
		writeJSONError(w, http.StatusUnprocessableEntity, "validation",
			"Request body must be valid JSON.")
		return
	}

	req := run.StartRequest{Topic: body.Topic, Outputs: body.Outputs}
	// Validate at the HTTP boundary so invalid input returns 422 before the Run
	// manager is involved (Requirement 7.4).
	if err := run.ValidateStartRequest(req); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "validation", err.Error())
		return
	}

	handle, err := s.runs.Start(req)
	if err != nil {
		s.writeStartRunError(w, err)
		return
	}

	writeJSON(w, http.StatusAccepted, map[string]string{"run_id": handle.RunID})
}

// writeStartRunError maps a Start error to the correct status and JSON shape.
func (s *Server) writeStartRunError(w http.ResponseWriter, err error) {
	var spawnErr *run.SpawnError
	switch {
	case errors.Is(err, run.ErrRunInProgress):
		writeJSONError(w, http.StatusConflict, "conflict",
			"A run is already in progress.")
	case errors.Is(err, run.ErrEmptyTopic), errors.Is(err, run.ErrInvalidOutputs):
		// Defensive: validation also runs at the boundary above.
		writeJSONError(w, http.StatusUnprocessableEntity, "validation", err.Error())
	case errors.As(err, &spawnErr):
		writeJSONError(w, http.StatusInternalServerError, "spawn_failed", spawnErr.Err.Error())
	default:
		writeJSONError(w, http.StatusInternalServerError, "internal_error", err.Error())
	}
}

// handleAPINotFound returns the JSON error shape for any unmatched API path.
func (s *Server) handleAPINotFound(w http.ResponseWriter, r *http.Request) {
	writeJSONError(w, http.StatusNotFound, "not_found", "No such API endpoint: "+r.URL.Path)
}

// handleUI serves the embedded UI. The root path serves index.html; any other
// unmatched path returns an HTML 404 (not the JSON error shape), per
// Requirement 12.2.
func (s *Server) handleUI(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeHTMLError(w, http.StatusNotFound, "Page not found")
		return
	}
	data, err := fs.ReadFile(s.ui, "index.html")
	if err != nil {
		writeHTMLError(w, http.StatusInternalServerError, "UI unavailable")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

// writeJSON writes v as a JSON response with the given status code.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// apiError is the canonical JSON error shape for the API surface:
// {"error": <code>, "detail": <message>} (Requirement 12.2).
type apiError struct {
	Error  string `json:"error"`
	Detail string `json:"detail"`
}

// writeJSONError writes the canonical API JSON error shape.
func writeJSONError(w http.ResponseWriter, status int, code, detail string) {
	writeJSON(w, status, apiError{Error: code, Detail: detail})
}

// writeHTMLError writes a minimal HTML error response for the UI surface.
func writeHTMLError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte("<!doctype html><html><head><title>" +
		strconv.Itoa(status) + "</title></head><body><h1>" +
		strconv.Itoa(status) + "</h1><p>" + message + "</p></body></html>"))
}
