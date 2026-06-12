// Package devto publishes a Run's post.md to dev.to over plain HTTPS. It carries
// no AWS dependency: the article is sent directly to the dev.to REST API at
// https://dev.to/api/articles with an api-key header.
//
// This implements Requirement 8 of the bullpen-console-go spec. It reproduces
// the Flask Console's devto_client.publish_article and _find_post_md behaviour
// in Go: post.md is located at output/<run_id>/post.md (flat) or, failing that,
// at output/<run_id>/<date-slug>/post.md (nested); an HTTP 201 is treated as
// authoritative success without inspecting the response body fields (decided in
// issue #36); a non-201 maps to a structured failure the HTTP layer turns into
// 502; and a network-level failure maps to a structured failure too.
//
// DEVTO_API_KEY is read from the environment at call time and sent only in the
// api-key request header. It is never written to any log this package produces.
package devto

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// articlesURL is the dev.to REST endpoint for creating articles. It is the
// default base URL; tests override it via WithBaseURL to point at a fake server
// so the real dev.to is never contacted.
const articlesURL = "https://dev.to/api/articles"

// apiKeyEnv is the environment variable holding the dev.to API key. It is read
// at call time and never logged (Requirement 8.6).
const apiKeyEnv = "DEVTO_API_KEY"

// postFilename is the reviewable article file produced by the writer.
const postFilename = "post.md"

// Sentinel errors. The HTTP layer maps these to status codes: ErrPostNotFound
// -> 404 and ErrMissingAPIKey -> 400. HTTP-level outcomes (non-201, network
// failure) are reported in DevtoResult, not as Go errors.
var (
	// ErrPostNotFound indicates no post.md exists at the flat or nested path.
	ErrPostNotFound = errors.New("devto: post.md not found")
	// ErrMissingAPIKey indicates DEVTO_API_KEY is unset or empty.
	ErrMissingAPIKey = errors.New("devto: DEVTO_API_KEY is not set")
)

// DevtoRequest is the publish input supplied by the client. The body_markdown
// is read from the located post.md, so it is not part of the request.
type DevtoRequest struct {
	Title     string   `json:"title"`
	Tags      []string `json:"tags"`
	Published bool     `json:"published"`
}

// DevtoResult is the structured outcome of a publish attempt. It mirrors the
// Python devto_client return shape: success carries url+id; an upstream non-201
// carries status_code+error; a network failure carries error only.
type DevtoResult struct {
	Success    bool   `json:"success"`
	URL        string `json:"url,omitempty"`
	ID         int    `json:"id,omitempty"`
	StatusCode int    `json:"status_code,omitempty"`
	Error      string `json:"error,omitempty"`
}

// Publisher locates a Run's post.md and POSTs it to dev.to. It is safe for
// concurrent use: it holds no mutable state. The HTTP client and base URL are
// injectable so tests use a fake dev.to server and never hit the network.
type Publisher struct {
	outputDir string
	baseURL   string
	client    *http.Client
}

// Option configures a Publisher at construction time.
type Option func(*Publisher)

// WithBaseURL overrides the dev.to articles endpoint. Tests point this at an
// httptest.Server so no request ever reaches the real dev.to.
func WithBaseURL(url string) Option {
	return func(p *Publisher) {
		if url != "" {
			p.baseURL = url
		}
	}
}

// WithHTTPClient overrides the HTTP client, letting tests inject a fake
// transport (for example one that always fails, to exercise the network-failure
// mapping) without opening a socket.
func WithHTTPClient(c *http.Client) Option {
	return func(p *Publisher) {
		if c != nil {
			p.client = c
		}
	}
}

// New constructs a Publisher rooted at outputDir (the parent of every
// output/<run_id>/ directory). It defaults to the real dev.to endpoint and a
// client with a sane timeout; both are overridable for tests via the options.
func New(outputDir string, opts ...Option) *Publisher {
	p := &Publisher{
		outputDir: outputDir,
		baseURL:   articlesURL,
		client:    &http.Client{Timeout: 30 * time.Second},
	}
	for _, opt := range opts {
		opt(p)
	}
	return p
}

// articleEnvelope is the dev.to request body: {"article": {...}}.
type articleEnvelope struct {
	Article articleBody `json:"article"`
}

type articleBody struct {
	Title        string   `json:"title"`
	BodyMarkdown string   `json:"body_markdown"`
	Tags         []string `json:"tags"`
	Published    bool     `json:"published"`
}

// Publish locates post.md for runID, reads it as the article body, and POSTs it
// to dev.to with the api-key header (Requirement 8.1, 8.2).
//
// Return contract:
//   - post.md missing            -> (zero, ErrPostNotFound)
//   - DEVTO_API_KEY unset         -> (zero, ErrMissingAPIKey)
//   - HTTP 201                    -> ({success:true, url, id}, nil)   [201 alone is authoritative, #36]
//   - non-201                     -> ({success:false, status_code, error}, nil)
//   - network-level failure       -> ({success:false, error}, nil)
func (p *Publisher) Publish(runID string, req DevtoRequest) (DevtoResult, error) {
	postPath, ok := p.findPostMD(runID)
	if !ok {
		return DevtoResult{}, ErrPostNotFound
	}

	// Read the API key at call time (Requirement 8.6). It is used only as a
	// request header below and is never logged.
	apiKey := os.Getenv(apiKeyEnv)
	if apiKey == "" {
		return DevtoResult{}, ErrMissingAPIKey
	}

	bodyMarkdown, err := os.ReadFile(postPath)
	if err != nil {
		return DevtoResult{}, ErrPostNotFound
	}

	payload, err := json.Marshal(articleEnvelope{Article: articleBody{
		Title:        req.Title,
		BodyMarkdown: string(bodyMarkdown),
		Tags:         req.Tags,
		Published:    req.Published,
	}})
	if err != nil {
		return DevtoResult{Success: false, Error: err.Error()}, nil
	}

	httpReq, err := http.NewRequest(http.MethodPost, p.baseURL, bytes.NewReader(payload))
	if err != nil {
		return DevtoResult{Success: false, Error: err.Error()}, nil
	}
	httpReq.Header.Set("api-key", apiKey)
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/vnd.forem.api-v1+json")

	resp, err := p.client.Do(httpReq)
	if err != nil {
		// Network-level failure: no status code (Requirement 8.5). The error
		// message is the transport error and never contains the API key.
		return DevtoResult{Success: false, Error: err.Error()}, nil
	}
	defer func() { _ = resp.Body.Close() }()

	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == http.StatusCreated {
		// HTTP 201 alone is authoritative — we do not additionally verify the
		// response body fields before treating the publish as successful
		// (Requirement 8.3, decided in issue #36). url/id are best-effort.
		var parsed struct {
			URL string `json:"url"`
			ID  int    `json:"id"`
		}
		_ = json.Unmarshal(respBody, &parsed)
		return DevtoResult{Success: true, URL: parsed.URL, ID: parsed.ID}, nil
	}

	// Non-201: surface the upstream status and body for diagnosis
	// (Requirement 8.4). The HTTP layer maps this to 502.
	return DevtoResult{
		Success:    false,
		StatusCode: resp.StatusCode,
		Error:      strings.TrimSpace(string(respBody)),
	}, nil
}

// findPostMD locates post.md for runID, confined to output/<run_id>/. It returns
// the flat output/<run_id>/post.md when present, else the first nested
// output/<run_id>/<subdir>/post.md, reproducing the Flask _find_post_md. A
// traversal run_id that resolves outside the output root is treated as
// not-found (false) so no file outside the run directory can be read.
func (p *Publisher) findPostMD(runID string) (string, bool) {
	runDir, ok := p.safeRunDir(runID)
	if !ok {
		return "", false
	}
	info, err := os.Stat(runDir)
	if err != nil || !info.IsDir() {
		return "", false
	}

	// Flat path wins when both exist.
	flat := filepath.Join(runDir, postFilename)
	if fi, err := os.Stat(flat); err == nil && !fi.IsDir() {
		return flat, true
	}

	// Nested: first <subdir>/post.md found.
	entries, err := os.ReadDir(runDir)
	if err != nil {
		return "", false
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		candidate := filepath.Join(runDir, entry.Name(), postFilename)
		if fi, err := os.Stat(candidate); err == nil && !fi.IsDir() {
			return candidate, true
		}
	}
	return "", false
}

// safeRunDir resolves output/<run_id>/ and confirms it stays inside the output
// root, mirroring the files package guard. Any run_id that would escape — via
// "..", an absolute path, or mixed separators — is rejected (false).
func (p *Publisher) safeRunDir(runID string) (string, bool) {
	base, err := filepath.Abs(p.outputDir)
	if err != nil {
		return "", false
	}
	base = filepath.Clean(base)

	candidate, err := filepath.Abs(filepath.Join(base, runID))
	if err != nil {
		return "", false
	}
	candidate = filepath.Clean(candidate)

	rel, err := filepath.Rel(base, candidate)
	if err != nil {
		return "", false
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return candidate, true
}
