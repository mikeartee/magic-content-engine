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
	"path/filepath"
	"strconv"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/devto"
	"github.com/mikeartee/magic-content-engine/console/internal/files"
	"github.com/mikeartee/magic-content-engine/console/internal/run"
	"github.com/mikeartee/magic-content-engine/console/internal/sse"
	"github.com/mikeartee/magic-content-engine/console/internal/vault"
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

// RunApprover is the slice of the Run_Manager the HTTP server depends on to
// resolve an approval gate. Decide writes the decision file for the active Run
// and returns run.ErrNoGate when no gate is awaiting a decision, which the
// server maps to HTTP 409 (Requirement 2, 12.4). It is declared consumer-side
// so handlers can be tested with a fake; nil until wired via SetRunManager when
// the attached manager also satisfies this interface.
type RunApprover interface {
	Decide(approved bool) error
}

// FileService is the slice of the File_Service the HTTP server depends on to
// back the run-bundle file API (Requirement 4). It is declared consumer-side so
// the server stays decoupled from the concrete *files.Service and so handlers
// can be tested with a fake. nil until wired via SetFileService.
type FileService interface {
	ListRuns() ([]files.RunListing, error)
	ReadFile(runID, name string) ([]byte, error)
	SaveFile(runID, name string, content []byte) error
	ResolveDownload(runID, name string) (string, error)
}

// SuggestionService is the slice of the Suggestion_Service the HTTP server
// depends on to back the vault-only topic suggestions (Requirement 6). It is
// declared consumer-side so the server stays decoupled from the concrete
// *vault.Service and so handlers can be tested with a fake. nil until wired via
// SetSuggestionService.
type SuggestionService interface {
	Recency(limit int) (vault.Suggestions, error)
	Search(query string, limit int) (vault.Suggestions, error)
}

// DevtoPublisher is the slice of the Devto_Publisher the HTTP server depends on
// to back POST /api/publish/devto (Requirement 8). It is declared consumer-side
// so the server stays decoupled from the concrete *devto.Publisher and so
// handlers can be tested with a fake. nil until wired via SetDevtoPublisher.
type DevtoPublisher interface {
	Publish(runID string, req devto.DevtoRequest) (devto.DevtoResult, error)
}

// Server holds the dependencies needed to serve the Console: the embedded UI
// file system, the run manager (POST /api/run), and the SSE hub plus run-output
// root (GET /api/run/status). Later slices add the file service, vault, and
// dev.to publisher.
type Server struct {
	ui        fs.FS             // embedded UI assets (rooted at the static directory)
	runs      RunStarter        // owns the single active Run; nil until wired
	approver  RunApprover       // resolves the approval gate; nil until wired
	hub       *sse.Hub          // SSE hub: tails agent-log.jsonl with replay + dedup
	outputDir string            // root holding output/<run_id>/ run directories
	files     FileService       // run-bundle file API; nil until wired
	vault     SuggestionService // vault-only topic suggestions; nil until wired
	devto     DevtoPublisher    // dev.to publisher; nil until wired
	isActive  func(runID string) bool
}

// New constructs a Server backed by the given UI file system. The ui argument
// is the embedded static asset tree (index.html and friends). The SSE hub, the
// run-output root, and the active-run probe take sensible defaults that later
// slices (the run manager) can override via the With* options. The run manager
// is attached separately via SetRunManager so the skeleton signature (#35)
// stays stable.
func New(ui fs.FS, opts ...Option) *Server {
	s := &Server{
		ui:        ui,
		hub:       sse.New(),
		outputDir: "output",
		// No run manager is wired yet, so no Run is ever considered active; the
		// SSE stream replays the log then settles into its terminal frame.
		isActive: func(string) bool { return false },
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// Option configures a Server at construction time without changing the single
// required argument of New (keeping callers and tests stable).
type Option func(*Server)

// WithOutputDir sets the root directory that holds output/<run_id>/ run
// directories used to resolve the SSE log path.
func WithOutputDir(dir string) Option {
	return func(s *Server) { s.outputDir = dir }
}

// WithActiveProbe lets the run manager report whether a given run_id is still
// active, so the SSE hub knows when to emit its terminal frame.
func WithActiveProbe(fn func(runID string) bool) Option {
	return func(s *Server) {
		if fn != nil {
			s.isActive = fn
		}
	}
}

// WithSSETiming overrides the SSE hub's poll interval and idle-tick limit. It
// exists for tests that need the terminal frame to settle quickly; production
// keeps the hub's ~1s/2-tick defaults. Non-positive values leave the
// corresponding default in place.
func WithSSETiming(poll time.Duration, idleTicks int) Option {
	return func(s *Server) {
		if poll > 0 {
			s.hub.PollInterval = poll
		}
		if idleTicks > 0 {
			s.hub.IdleTicksLimit = idleTicks
		}
	}
}

// SetRunManager attaches the Run_Manager dependency used by POST /api/run. When
// the attached manager also satisfies RunApprover (the concrete *run.Manager
// does), it is wired for POST /api/run/approve and /api/run/reject as well.
func (s *Server) SetRunManager(rm RunStarter) {
	s.runs = rm
	if a, ok := rm.(RunApprover); ok {
		s.approver = a
	}
}

// SetFileService attaches the File_Service dependency used by the run-bundle
// file API (GET /api/runs, GET/POST /api/runs/{id}/file, and
// GET /api/runs/{id}/download/{file}).
func (s *Server) SetFileService(fs FileService) {
	s.files = fs
}

// SetSuggestionService attaches the Suggestion_Service dependency used by the
// vault-only topic suggestions API (GET /api/suggestions and
// GET /api/suggestions/search).
func (s *Server) SetSuggestionService(vs SuggestionService) {
	s.vault = vs
}

// SetDevtoPublisher attaches the Devto_Publisher dependency used by
// POST /api/publish/devto (Requirement 8).
func (s *Server) SetDevtoPublisher(dp DevtoPublisher) {
	s.devto = dp
}

// Routes builds the http.ServeMux with every Console route registered. It is
// the single place the URL surface is wired.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()

	// API surface.
	mux.HandleFunc("GET /api/health", s.handleHealth)
	mux.HandleFunc("POST /api/run", s.handleStartRun)
	mux.HandleFunc("GET /api/run/status", s.handleRunStatus)
	mux.HandleFunc("POST /api/run/approve", s.handleApprove)
	mux.HandleFunc("POST /api/run/reject", s.handleReject)
	// Run-bundle file API (Requirement 4).
	mux.HandleFunc("GET /api/runs", s.handleListRuns)
	mux.HandleFunc("GET /api/runs/{id}/file", s.handleReadFile)
	mux.HandleFunc("POST /api/runs/{id}/file", s.handleSaveFile)
	mux.HandleFunc("GET /api/runs/{id}/download/{file...}", s.handleDownloadFile)
	// Vault-only topic suggestions (Requirement 6). The more specific search
	// route is registered alongside the recency list; net/http's pattern matcher
	// routes /api/suggestions/search to the search handler.
	mux.HandleFunc("GET /api/suggestions", s.handleSuggestions)
	mux.HandleFunc("GET /api/suggestions/search", s.handleSearchSuggestions)
	// dev.to publishing (Requirement 8): locate post.md and POST to dev.to.
	mux.HandleFunc("POST /api/publish/devto", s.handlePublishDevto)
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

// handleApprove implements POST /api/run/approve (Requirement 2, 12.3): it
// records an approved decision for the active Run. With no gate awaiting a
// decision it returns 409 (Requirement 12.4).
func (s *Server) handleApprove(w http.ResponseWriter, _ *http.Request) {
	s.decide(w, true)
}

// handleReject implements POST /api/run/reject (Requirement 2, 12.3): it records
// a rejected decision for the active Run, retaining the files without
// publishing. With no gate awaiting a decision it returns 409 (Requirement 12.4).
func (s *Server) handleReject(w http.ResponseWriter, _ *http.Request) {
	s.decide(w, false)
}

// decide writes the approval decision via the Run_Manager and maps the outcome
// to the HTTP response: 200 on success, 409 when no gate is awaiting
// (run.ErrNoGate), 500 otherwise.
func (s *Server) decide(w http.ResponseWriter, approved bool) {
	if s.approver == nil {
		writeJSONError(w, http.StatusInternalServerError, "internal_error",
			"Run manager is not configured.")
		return
	}
	if err := s.approver.Decide(approved); err != nil {
		if errors.Is(err, run.ErrNoGate) {
			writeJSONError(w, http.StatusConflict, "conflict",
				"No approval gate is currently waiting.")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "internal_error", err.Error())
		return
	}
	decision := "rejected"
	if approved {
		decision = "approved"
	}
	writeJSON(w, http.StatusOK, map[string]string{"decision": decision})
}

// handleRunStatus implements Requirement 1: it streams a Run's agent log as
// Server-Sent Events. The run_id query parameter selects the run directory
// output/<run_id>/, whose agent-log.jsonl is the tail source (Requirement 1.5).
// Replay, deduplication, headers, the synthetic terminal frame, missing-file
// creation, and clean close on disconnect are all owned by the SSE hub.
func (s *Server) handleRunStatus(w http.ResponseWriter, r *http.Request) {
	runID := r.URL.Query().Get("run_id")
	logPath := filepath.Join(s.outputDir, runID, "agent-log.jsonl")
	isActive := func() bool { return s.isActive(runID) }
	// Errors here (e.g. client disconnect) are expected; the hub already wrote
	// the SSE headers, so there is no separate error response to send.
	_ = s.hub.Stream(r.Context(), w, logPath, isActive)
}

// fileServiceReady reports whether the file service is wired and, when not,
// writes the standard internal_error response.
func (s *Server) fileServiceReady(w http.ResponseWriter) bool {
	if s.files == nil {
		writeJSONError(w, http.StatusInternalServerError, "internal_error",
			"File service is not configured.")
		return false
	}
	return true
}

// handleListRuns implements Requirement 4.2: GET /api/runs lists run
// directories one level deep, with agent-log.jsonl and checkpoints.json
// excluded. A missing output directory yields an empty list, not an error.
func (s *Server) handleListRuns(w http.ResponseWriter, _ *http.Request) {
	if !s.fileServiceReady(w) {
		return
	}
	runs, err := s.files.ListRuns()
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "internal_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": runs})
}

// handleReadFile implements Requirement 4.3: GET /api/runs/{id}/file?name=
// returns the file content. The name may include a single subdirectory segment.
// A traversal name is rejected with 403 forbidden (Requirement 4.1).
func (s *Server) handleReadFile(w http.ResponseWriter, r *http.Request) {
	if !s.fileServiceReady(w) {
		return
	}
	runID := r.PathValue("id")
	name := r.URL.Query().Get("name")
	if name == "" {
		writeJSONError(w, http.StatusBadRequest, "missing_parameter", "name is required")
		return
	}
	content, err := s.files.ReadFile(runID, name)
	if err != nil {
		s.writeFileError(w, err)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(content)
}

// saveFileRequest is the POST /api/runs/{id}/file request body.
type saveFileRequest struct {
	Name    string `json:"name"`
	Content string `json:"content"`
}

// handleSaveFile implements Requirement 4.4: POST /api/runs/{id}/file saves the
// file atomically (temp write + rename). An empty name or content is a 422; a
// traversal name is rejected with 403 forbidden (Requirement 4.1).
func (s *Server) handleSaveFile(w http.ResponseWriter, r *http.Request) {
	if !s.fileServiceReady(w) {
		return
	}
	runID := r.PathValue("id")
	var body saveFileRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "validation",
			"Request body must be valid JSON.")
		return
	}
	if body.Name == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "validation", "name is required")
		return
	}
	if body.Content == "" {
		writeJSONError(w, http.StatusUnprocessableEntity, "validation", "content must be non-empty")
		return
	}
	if err := s.files.SaveFile(runID, body.Name, []byte(body.Content)); err != nil {
		s.writeFileError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"saved": true})
}

// handleDownloadFile implements Requirement 4.5: GET
// /api/runs/{id}/download/{file} serves the file content with a
// Content-Disposition: attachment header. A traversal name is rejected with 403
// forbidden (Requirement 4.1).
func (s *Server) handleDownloadFile(w http.ResponseWriter, r *http.Request) {
	if !s.fileServiceReady(w) {
		return
	}
	runID := r.PathValue("id")
	name := r.PathValue("file")
	path, err := s.files.ResolveDownload(runID, name)
	if err != nil {
		s.writeFileError(w, err)
		return
	}
	w.Header().Set("Content-Disposition", "attachment; filename=\""+filepath.Base(path)+"\"")
	http.ServeFile(w, r, path)
}

// writeFileError maps a File_Service error to the correct status and JSON shape:
// ErrForbidden -> 403 forbidden (Requirement 4.1), ErrNotFound -> 404, anything
// else -> 500.
func (s *Server) writeFileError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, files.ErrForbidden):
		writeJSONError(w, http.StatusForbidden, "forbidden", "path traversal detected")
	case errors.Is(err, files.ErrNotFound):
		writeJSONError(w, http.StatusNotFound, "not_found", "file not found")
	default:
		writeJSONError(w, http.StatusInternalServerError, "internal_error", err.Error())
	}
}

// suggestionServiceReady reports whether the suggestion service is wired and,
// when not, writes the standard internal_error response (matching the file
// API's fail-safe behaviour).
func (s *Server) suggestionServiceReady(w http.ResponseWriter) bool {
	if s.vault == nil {
		writeJSONError(w, http.StatusInternalServerError, "internal_error",
			"Suggestion service is not configured.")
		return false
	}
	return true
}

// parseLimit reads the optional `limit` query parameter, returning 0 when it is
// absent or malformed so the service applies its own default cap.
func parseLimit(r *http.Request) int {
	raw := r.URL.Query().Get("limit")
	if raw == "" {
		return 0
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 {
		return 0
	}
	return n
}

// handleSuggestions implements Requirement 6.2: GET /api/suggestions returns the
// vault recency list as {"suggestions": [...]}. A missing vault yields an empty
// list and a warning field, not an error (Requirement 6.5). No AWS call is made
// (Requirement 6.1).
func (s *Server) handleSuggestions(w http.ResponseWriter, r *http.Request) {
	if !s.suggestionServiceReady(w) {
		return
	}
	result, err := s.vault.Recency(parseLimit(r))
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "internal_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// handleSearchSuggestions implements Requirement 6.4: GET
// /api/suggestions/search?q= matches the query case-insensitively against the
// derived title of every vault note and returns {"suggestions": [...]}. A
// missing vault yields an empty list and a warning, not an error
// (Requirement 6.5). No AWS call is made (Requirement 6.1).
func (s *Server) handleSearchSuggestions(w http.ResponseWriter, r *http.Request) {
	if !s.suggestionServiceReady(w) {
		return
	}
	query := r.URL.Query().Get("q")
	result, err := s.vault.Search(query, parseLimit(r))
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "internal_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// publishDevtoRequest is the POST /api/publish/devto request body. body_markdown
// is not part of it: the publisher reads it from the located post.md.
type publishDevtoRequest struct {
	RunID     string   `json:"run_id"`
	Title     string   `json:"title"`
	Tags      []string `json:"tags"`
	Published bool     `json:"published"`
}

// handlePublishDevto implements Requirement 8: publish a Run's post.md to
// dev.to. It maps the publisher outcome to HTTP:
//
//   - success            -> 201 {success:true, url, id}
//   - non-201 upstream   -> 502 {success:false, status_code, error} (Req 8.4)
//   - network failure    -> 502 {success:false, error}              (Req 8.5)
//   - post.md not found  -> 404 {"error":"not_found", ...}          (Req 8.1)
//   - DEVTO_API_KEY unset -> 400 {"error":"missing_api_key", ...}    (Req 8.6)
//
// The api-key never appears in any response or log this handler produces.
func (s *Server) handlePublishDevto(w http.ResponseWriter, r *http.Request) {
	if s.devto == nil {
		writeJSONError(w, http.StatusInternalServerError, "internal_error",
			"dev.to publisher is not configured.")
		return
	}

	var body publishDevtoRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSONError(w, http.StatusUnprocessableEntity, "validation",
			"Request body must be valid JSON.")
		return
	}

	result, err := s.devto.Publish(body.RunID, devto.DevtoRequest{
		Title:     body.Title,
		Tags:      body.Tags,
		Published: body.Published,
	})
	if err != nil {
		switch {
		case errors.Is(err, devto.ErrPostNotFound):
			writeJSONError(w, http.StatusNotFound, "not_found",
				"post.md not found for run "+body.RunID)
		case errors.Is(err, devto.ErrMissingAPIKey):
			writeJSONError(w, http.StatusBadRequest, "missing_api_key",
				"DEVTO_API_KEY is not set.")
		default:
			writeJSONError(w, http.StatusInternalServerError, "internal_error", err.Error())
		}
		return
	}

	if result.Success {
		writeJSON(w, http.StatusCreated, result)
		return
	}
	// Both a non-201 upstream status and a network failure surface as 502
	// (Requirement 8.4, 8.5; design Scenario 7).
	writeJSON(w, http.StatusBadGateway, result)
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
