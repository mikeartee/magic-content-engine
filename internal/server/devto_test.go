package server_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"testing/fstest"

	"github.com/mikeartee/magic-content-engine/console/internal/devto"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// fakePublisher is a hand-rolled DevtoPublisher so the handler tests stay
// decoupled from the filesystem + HTTP-backed devto.Publisher. It records the
// arguments it was called with and returns a canned result/error.
type fakePublisher struct {
	result   devto.DevtoResult
	err      error
	gotRunID string
	gotReq   devto.DevtoRequest
	hit      bool
}

func (f *fakePublisher) Publish(runID string, req devto.DevtoRequest) (devto.DevtoResult, error) {
	f.hit = true
	f.gotRunID = runID
	f.gotReq = req
	return f.result, f.err
}

func newDevtoServer(t *testing.T, fake server.DevtoPublisher) http.Handler {
	t.Helper()
	ui := fstest.MapFS{
		"index.html": &fstest.MapFile{Data: []byte("<!doctype html><title>Bullpen Console</title>")},
	}
	s := server.New(ui)
	if fake != nil {
		s.SetDevtoPublisher(fake)
	}
	return s.Routes()
}

func postPublish(t *testing.T, h http.Handler, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/publish/devto", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// Requirement 8.3: HTTP 201 success maps to 201 with {success:true, url, id}
// and the handler forwards run_id, title, tags, published to the publisher.
func TestPublishDevtoSuccess(t *testing.T) {
	fake := &fakePublisher{result: devto.DevtoResult{Success: true, URL: "https://dev.to/mike/x", ID: 7}}
	h := newDevtoServer(t, fake)

	rec := postPublish(t, h, `{"run_id":"run1","title":"My Title","tags":["aws","kiro"],"published":true}`)

	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201 (%q)", rec.Code, rec.Body.String())
	}
	var body devto.DevtoResult
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	if !body.Success || body.URL != "https://dev.to/mike/x" || body.ID != 7 {
		t.Errorf("body = %+v, want success url/id", body)
	}
	if fake.gotRunID != "run1" {
		t.Errorf("run_id = %q, want run1", fake.gotRunID)
	}
	if fake.gotReq.Title != "My Title" || !fake.gotReq.Published {
		t.Errorf("forwarded req = %+v, want title/published forwarded", fake.gotReq)
	}
	if len(fake.gotReq.Tags) != 2 || fake.gotReq.Tags[0] != "aws" {
		t.Errorf("forwarded tags = %v, want [aws kiro]", fake.gotReq.Tags)
	}
}

// Requirement 8.4: a non-201 publisher result maps to HTTP 502 with
// {success:false, status_code, error}.
func TestPublishDevtoNon201Maps502(t *testing.T) {
	fake := &fakePublisher{result: devto.DevtoResult{Success: false, StatusCode: 422, Error: "title is taken"}}
	h := newDevtoServer(t, fake)

	rec := postPublish(t, h, `{"run_id":"run1","title":"dup"}`)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 (%q)", rec.Code, rec.Body.String())
	}
	var body devto.DevtoResult
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	if body.Success || body.StatusCode != 422 || body.Error != "title is taken" {
		t.Errorf("body = %+v, want failure with status_code+error", body)
	}
}

// Requirement 8.5: a network-level failure (success:false, no status code) maps
// to HTTP 502 with {success:false, error}.
func TestPublishDevtoNetworkFailureMaps502(t *testing.T) {
	fake := &fakePublisher{result: devto.DevtoResult{Success: false, Error: "dial tcp: network failure"}}
	h := newDevtoServer(t, fake)

	rec := postPublish(t, h, `{"run_id":"run1","title":"x"}`)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 (%q)", rec.Code, rec.Body.String())
	}
	var body devto.DevtoResult
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	if body.Success || body.Error == "" {
		t.Errorf("body = %+v, want failure with error", body)
	}
}

// Requirement 8.1: a missing post.md maps to HTTP 404 with the standard
// {"error","detail"} shape.
func TestPublishDevtoPostNotFound404(t *testing.T) {
	fake := &fakePublisher{err: devto.ErrPostNotFound}
	h := newDevtoServer(t, fake)

	rec := postPublish(t, h, `{"run_id":"run1","title":"x"}`)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 (%q)", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v", err)
	}
	if body["error"] != "not_found" {
		t.Errorf("error = %v, want not_found", body["error"])
	}
}

// Requirement 8.6: a missing DEVTO_API_KEY maps to HTTP 400 missing_api_key.
func TestPublishDevtoMissingKey400(t *testing.T) {
	fake := &fakePublisher{err: devto.ErrMissingAPIKey}
	h := newDevtoServer(t, fake)

	rec := postPublish(t, h, `{"run_id":"run1","title":"x"}`)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (%q)", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v", err)
	}
	if body["error"] != "missing_api_key" {
		t.Errorf("error = %v, want missing_api_key", body["error"])
	}
}

// A malformed JSON body is rejected with 422 before the publisher is called.
func TestPublishDevtoMalformedBody422(t *testing.T) {
	fake := &fakePublisher{}
	h := newDevtoServer(t, fake)

	rec := postPublish(t, h, `{not json`)

	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422 (%q)", rec.Code, rec.Body.String())
	}
	if fake.hit {
		t.Error("publisher was called on a malformed body, want short-circuit")
	}
}

// When no publisher is wired the endpoint returns the standard internal_error
// shape, matching the file/suggestion APIs' fail-safe behaviour.
func TestPublishDevtoNotConfigured500(t *testing.T) {
	h := newDevtoServer(t, nil)

	rec := postPublish(t, h, `{"run_id":"run1","title":"x"}`)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500 (%q)", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v", err)
	}
	if body["error"] != "internal_error" {
		t.Errorf("error = %v, want internal_error", body["error"])
	}
}
