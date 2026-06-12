package server_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"testing/fstest"

	"github.com/mikeartee/magic-content-engine/console/internal/server"
	"github.com/mikeartee/magic-content-engine/console/internal/vault"
)

// fakeSuggestions is a hand-rolled SuggestionService so the handler tests stay
// decoupled from the filesystem-backed vault service. It records the arguments
// it is called with so the handler's parameter parsing can be asserted.
type fakeSuggestions struct {
	recency    vault.Suggestions
	search     vault.Suggestions
	err        error
	gotLimit   int
	gotQuery   string
	gotSLimit  int
	recencyHit bool
	searchHit  bool
}

func (f *fakeSuggestions) Recency(limit int) (vault.Suggestions, error) {
	f.recencyHit = true
	f.gotLimit = limit
	return f.recency, f.err
}

func (f *fakeSuggestions) Search(query string, limit int) (vault.Suggestions, error) {
	f.searchHit = true
	f.gotQuery = query
	f.gotSLimit = limit
	return f.search, f.err
}

func newSuggestionServer(t *testing.T, fake *fakeSuggestions) http.Handler {
	t.Helper()
	ui := fstest.MapFS{
		"index.html": &fstest.MapFile{Data: []byte("<!doctype html><title>Bullpen Console</title>")},
	}
	s := server.New(ui)
	s.SetSuggestionService(fake)
	return s.Routes()
}

// Requirement 6.2: GET /api/suggestions returns the recency list as
// {"suggestions": [...]} and forwards a limit query parameter.
func TestGetSuggestionsReturnsRecencyList(t *testing.T) {
	fake := &fakeSuggestions{recency: vault.Suggestions{Items: []vault.Suggestion{
		{Topic: "Strands Agents SDK", LastCovered: "2026-01-09", DaysSince: 1, Source: "06-permanent/Strands Agents SDK.md"},
		{Topic: "AgentCore in Sydney", LastCovered: "2026-01-07", DaysSince: 3, Source: "06-permanent/AgentCore in Sydney.md"},
	}}}
	h := newSuggestionServer(t, fake)

	req := httptest.NewRequest(http.MethodGet, "/api/suggestions?limit=5", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (%q)", rec.Code, rec.Body.String())
	}
	var body struct {
		Suggestions []vault.Suggestion `json:"suggestions"`
		Warning     string             `json:"warning"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	if len(body.Suggestions) != 2 || body.Suggestions[0].Topic != "Strands Agents SDK" {
		t.Errorf("suggestions = %+v, want the recency list", body.Suggestions)
	}
	if !fake.recencyHit {
		t.Error("handler did not call Recency")
	}
	if fake.gotLimit != 5 {
		t.Errorf("forwarded limit = %d, want 5", fake.gotLimit)
	}
}

// Requirement 6.5: when the service reports a warning (e.g. missing vault),
// the handler returns 200 with an empty list and the warning field, not an error.
func TestGetSuggestionsSurfacesWarning(t *testing.T) {
	fake := &fakeSuggestions{recency: vault.Suggestions{Items: []vault.Suggestion{}, Warning: "VAULT_PATH not found"}}
	h := newSuggestionServer(t, fake)

	req := httptest.NewRequest(http.MethodGet, "/api/suggestions", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (%q)", rec.Code, rec.Body.String())
	}
	var body struct {
		Suggestions []vault.Suggestion `json:"suggestions"`
		Warning     string             `json:"warning"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	if len(body.Suggestions) != 0 {
		t.Errorf("suggestions = %+v, want empty", body.Suggestions)
	}
	if body.Warning == "" {
		t.Error("warning field missing, want the service warning surfaced")
	}
}

// Requirement 6.4: GET /api/suggestions/search?q= forwards the query and
// returns the matches as {"suggestions": [...]}.
func TestSearchSuggestionsReturnsMatches(t *testing.T) {
	fake := &fakeSuggestions{search: vault.Suggestions{Items: []vault.Suggestion{
		{Topic: "Bedrock AgentCore", Source: "06-permanent/Bedrock AgentCore.md"},
	}}}
	h := newSuggestionServer(t, fake)

	req := httptest.NewRequest(http.MethodGet, "/api/suggestions/search?q=agent&limit=3", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (%q)", rec.Code, rec.Body.String())
	}
	var body struct {
		Suggestions []vault.Suggestion `json:"suggestions"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body not JSON: %v (%q)", err, rec.Body.String())
	}
	if len(body.Suggestions) != 1 || body.Suggestions[0].Topic != "Bedrock AgentCore" {
		t.Errorf("suggestions = %+v, want the search match", body.Suggestions)
	}
	if !fake.searchHit {
		t.Error("handler did not call Search")
	}
	if fake.gotQuery != "agent" {
		t.Errorf("forwarded query = %q, want agent", fake.gotQuery)
	}
	if fake.gotSLimit != 3 {
		t.Errorf("forwarded search limit = %d, want 3", fake.gotSLimit)
	}
}

// The suggestion endpoints return the standard internal_error JSON shape when
// no service is wired, matching the file API's fail-safe behaviour.
func TestSuggestionsNotConfigured(t *testing.T) {
	ui := fstest.MapFS{"index.html": &fstest.MapFile{Data: []byte("<title>x</title>")}}
	h := server.New(ui).Routes() // no SetSuggestionService

	for _, path := range []string{"/api/suggestions", "/api/suggestions/search?q=x"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusInternalServerError {
			t.Errorf("%s: status = %d, want 500", path, rec.Code)
		}
		var body map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Errorf("%s: body not JSON: %v", path, err)
			continue
		}
		if body["error"] != "internal_error" {
			t.Errorf("%s: error = %v, want internal_error", path, body["error"])
		}
	}
}
