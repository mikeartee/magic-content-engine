package devto_test

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mikeartee/magic-content-engine/console/internal/devto"
)

const testAPIKey = "secret-devto-key-123"

// articleEnvelope mirrors the dev.to request body {"article": {...}} so tests
// can assert exactly what the publisher sends upstream.
type articleEnvelope struct {
	Article struct {
		Title        string   `json:"title"`
		BodyMarkdown string   `json:"body_markdown"`
		Tags         []string `json:"tags"`
		Published    bool     `json:"published"`
	} `json:"article"`
}

// capture records what a fake dev.to server received, so tests can assert the
// api-key header and the request body.
type capture struct {
	apiKey   string
	envelope articleEnvelope
	hits     int
}

// newFakeDevto returns an httptest.Server that records the request and replies
// with the given status and body. Tests NEVER touch the real dev.to.
func newFakeDevto(t *testing.T, status int, respBody string, cap *capture) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cap.hits++
		cap.apiKey = r.Header.Get("api-key")
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &cap.envelope)
		w.WriteHeader(status)
		_, _ = io.WriteString(w, respBody)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func seedPost(t *testing.T, outputDir, rel, content string) {
	t.Helper()
	path := filepath.Join(outputDir, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// Requirement 8.1 (flat) + 8.2 + 8.3: post.md at output/<run_id>/post.md is
// located, POSTed with the api-key header and the article envelope, and a 201
// maps to {success:true, url, id}.
func TestPublishLocatesFlatPostMD(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	seedPost(t, output, "run1/post.md", "# Flat Body\n\nHello from the flat path.")

	cap := &capture{}
	srv := newFakeDevto(t, http.StatusCreated, `{"url":"https://dev.to/mike/flat","id":4242}`, cap)

	pub := devto.New(output, devto.WithBaseURL(srv.URL))
	res, err := pub.Publish("run1", devto.DevtoRequest{
		Title:     "Flat Title",
		Tags:      []string{"aws", "kiro"},
		Published: true,
	})
	if err != nil {
		t.Fatalf("Publish error = %v, want nil", err)
	}
	if !res.Success || res.URL != "https://dev.to/mike/flat" || res.ID != 4242 {
		t.Errorf("result = %+v, want success url/id mapped", res)
	}
	if cap.envelope.Article.BodyMarkdown != "# Flat Body\n\nHello from the flat path." {
		t.Errorf("body_markdown = %q, want the flat post.md content", cap.envelope.Article.BodyMarkdown)
	}
	if cap.envelope.Article.Title != "Flat Title" {
		t.Errorf("title = %q, want Flat Title", cap.envelope.Article.Title)
	}
	if strings.Join(cap.envelope.Article.Tags, ",") != "aws,kiro" {
		t.Errorf("tags = %v, want [aws kiro]", cap.envelope.Article.Tags)
	}
	if !cap.envelope.Article.Published {
		t.Errorf("published = false, want true")
	}
}

// Requirement 8.1 (nested): when post.md is absent at the flat path it is
// located at output/<run_id>/<date-slug>/post.md.
func TestPublishLocatesNestedPostMD(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	seedPost(t, output, "run1/2026-01-09-aotearoa-agentcore/post.md", "# Nested Body\n\nFrom the date-slug path.")

	cap := &capture{}
	srv := newFakeDevto(t, http.StatusCreated, `{"url":"https://dev.to/mike/nested","id":99}`, cap)

	pub := devto.New(output, devto.WithBaseURL(srv.URL))
	res, err := pub.Publish("run1", devto.DevtoRequest{Title: "Nested", Tags: nil, Published: false})
	if err != nil {
		t.Fatalf("Publish error = %v, want nil", err)
	}
	if !res.Success || res.URL != "https://dev.to/mike/nested" || res.ID != 99 {
		t.Errorf("result = %+v, want success from nested path", res)
	}
	if cap.envelope.Article.BodyMarkdown != "# Nested Body\n\nFrom the date-slug path." {
		t.Errorf("body_markdown = %q, want the nested post.md content", cap.envelope.Article.BodyMarkdown)
	}
}

// Requirement 8.1: a missing post.md (neither flat nor nested) yields
// ErrPostNotFound and no upstream call is made.
func TestPublishPostMDNotFound(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	if err := os.MkdirAll(filepath.Join(output, "run1"), 0o755); err != nil {
		t.Fatal(err)
	}

	cap := &capture{}
	srv := newFakeDevto(t, http.StatusCreated, `{}`, cap)

	pub := devto.New(output, devto.WithBaseURL(srv.URL))
	_, err := pub.Publish("run1", devto.DevtoRequest{Title: "x"})
	if !errors.Is(err, devto.ErrPostNotFound) {
		t.Fatalf("err = %v, want ErrPostNotFound", err)
	}
	if cap.hits != 0 {
		t.Errorf("upstream hits = %d, want 0 (no call when post.md missing)", cap.hits)
	}
}

// Requirement 8.4: a non-201 status maps to {success:false, status_code, error}
// (the HTTP layer turns this into 502). Publish itself returns a nil error.
func TestPublishNon201Maps(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	seedPost(t, output, "run1/post.md", "body")

	cap := &capture{}
	srv := newFakeDevto(t, http.StatusUnprocessableEntity, `{"error":"title is taken"}`, cap)

	pub := devto.New(output, devto.WithBaseURL(srv.URL))
	res, err := pub.Publish("run1", devto.DevtoRequest{Title: "dup"})
	if err != nil {
		t.Fatalf("Publish error = %v, want nil (non-201 is a result, not a Go error)", err)
	}
	if res.Success {
		t.Errorf("result.Success = true, want false for non-201")
	}
	if res.StatusCode != http.StatusUnprocessableEntity {
		t.Errorf("result.StatusCode = %d, want 422", res.StatusCode)
	}
	if !strings.Contains(res.Error, "title is taken") {
		t.Errorf("result.Error = %q, want upstream body text", res.Error)
	}
}

// errTransport always fails, simulating a network-level failure without ever
// reaching a real server.
type errTransport struct{}

func (errTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("dial tcp: simulated network failure")
}

// Requirement 8.5: a network-level failure maps to {success:false, error} with
// no status code. Publish returns a nil error (the failure is in the result).
func TestPublishNetworkFailure(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	seedPost(t, output, "run1/post.md", "body")

	pub := devto.New(output,
		devto.WithBaseURL("https://dev.to/api/articles"),
		devto.WithHTTPClient(&http.Client{Transport: errTransport{}}),
	)
	res, err := pub.Publish("run1", devto.DevtoRequest{Title: "x"})
	if err != nil {
		t.Fatalf("Publish error = %v, want nil (network failure is a result)", err)
	}
	if res.Success {
		t.Errorf("result.Success = true, want false on network failure")
	}
	if res.Error == "" {
		t.Errorf("result.Error empty, want the network error message")
	}
	if res.StatusCode != 0 {
		t.Errorf("result.StatusCode = %d, want 0 on network failure", res.StatusCode)
	}
}

// Requirement 8.2: the api-key header carries DEVTO_API_KEY read from the
// environment at call time.
func TestPublishSendsAPIKeyHeader(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	seedPost(t, output, "run1/post.md", "body")

	cap := &capture{}
	srv := newFakeDevto(t, http.StatusCreated, `{"url":"u","id":1}`, cap)

	pub := devto.New(output, devto.WithBaseURL(srv.URL))
	if _, err := pub.Publish("run1", devto.DevtoRequest{Title: "x"}); err != nil {
		t.Fatalf("Publish error = %v", err)
	}
	if cap.apiKey != testAPIKey {
		t.Errorf("api-key header = %q, want %q", cap.apiKey, testAPIKey)
	}
}

// Requirement 8.6: DEVTO_API_KEY is never written to any log output the
// publisher produces.
func TestPublishDoesNotLogAPIKey(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	seedPost(t, output, "run1/post.md", "body")

	var logBuf strings.Builder
	oldOut := log.Writer()
	oldFlags := log.Flags()
	log.SetOutput(&logBuf)
	t.Cleanup(func() {
		log.SetOutput(oldOut)
		log.SetFlags(oldFlags)
	})

	cap := &capture{}
	srv := newFakeDevto(t, http.StatusCreated, `{"url":"u","id":1}`, cap)

	pub := devto.New(output, devto.WithBaseURL(srv.URL))
	if _, err := pub.Publish("run1", devto.DevtoRequest{Title: "x"}); err != nil {
		t.Fatalf("Publish error = %v", err)
	}
	if strings.Contains(logBuf.String(), testAPIKey) {
		t.Errorf("log output contains the API key, must never be logged: %q", logBuf.String())
	}
}

// Requirement 8.6: when DEVTO_API_KEY is unset the publisher reports
// ErrMissingAPIKey and makes no upstream call.
func TestPublishMissingAPIKey(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", "")
	output := t.TempDir()
	seedPost(t, output, "run1/post.md", "body")

	cap := &capture{}
	srv := newFakeDevto(t, http.StatusCreated, `{}`, cap)

	pub := devto.New(output, devto.WithBaseURL(srv.URL))
	_, err := pub.Publish("run1", devto.DevtoRequest{Title: "x"})
	if !errors.Is(err, devto.ErrMissingAPIKey) {
		t.Fatalf("err = %v, want ErrMissingAPIKey", err)
	}
	if cap.hits != 0 {
		t.Errorf("upstream hits = %d, want 0 when key missing", cap.hits)
	}
}

// Requirement 8.1: the flat path wins when both flat and nested post.md exist.
func TestPublishPrefersFlatOverNested(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	seedPost(t, output, "run1/post.md", "FLAT")
	seedPost(t, output, "run1/2026-01-09-slug/post.md", "NESTED")

	cap := &capture{}
	srv := newFakeDevto(t, http.StatusCreated, `{"url":"u","id":1}`, cap)

	pub := devto.New(output, devto.WithBaseURL(srv.URL))
	if _, err := pub.Publish("run1", devto.DevtoRequest{Title: "x"}); err != nil {
		t.Fatalf("Publish error = %v", err)
	}
	if cap.envelope.Article.BodyMarkdown != "FLAT" {
		t.Errorf("body_markdown = %q, want FLAT (flat path preferred)", cap.envelope.Article.BodyMarkdown)
	}
}

// Requirement 8.1 + path safety: a traversal run_id cannot escape the output
// root; it resolves to not-found rather than reading an outside file.
func TestPublishRejectsTraversalRunID(t *testing.T) {
	t.Setenv("DEVTO_API_KEY", testAPIKey)
	output := t.TempDir()
	// Place a post.md OUTSIDE the output root that a traversal might reach.
	outside := filepath.Join(output, "..", "outside")
	seedPost(t, outside, "post.md", "SECRET OUTSIDE")

	pub := devto.New(output, devto.WithBaseURL("http://127.0.0.1:0"))
	_, err := pub.Publish("../outside", devto.DevtoRequest{Title: "x"})
	if !errors.Is(err, devto.ErrPostNotFound) {
		t.Fatalf("err = %v, want ErrPostNotFound for a traversal run_id", err)
	}
}
