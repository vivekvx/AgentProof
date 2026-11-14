package server

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/vivekvx/AgentProof/db"
	"github.com/vivekvx/AgentProof/packages/contracts"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

type testOIDCVerifier struct {
	principal Principal
	err       error
}
type tokenVerifier map[string]Principal

func (v tokenVerifier) Verify(_ context.Context, token string) (Principal, error) {
	p, ok := v[token]
	if !ok {
		return Principal{}, ErrInvalidToken
	}
	return p, nil
}

func TestTwoDeveloperProjectEvaluationIsolation(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("Set TEST_DATABASE_URL")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if err = db.Migrate(context.Background(), pool); err != nil {
		t.Fatal(err)
	}
	h := (&Server{Pool: pool, HostedAuth: tokenVerifier{"a": {Issuer: "https://local-issuer", Subject: "a"}, "b": {Issuer: "https://local-issuer", Subject: "b"}}}).Routes()
	call := func(token, method, path, project, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		if project != "" {
			r.Header.Set("X-AgentProof-Project", project)
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	project := func(token, name string) string {
		w := call(token, "POST", "/api/v2/projects", "", `{"name":"`+name+`"}`)
		if w.Code != 201 {
			t.Fatal(w.Code, w.Body.String())
		}
		var out map[string]string
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		return out["id"]
	}
	a, b := project("a", "A"), project("b", "B")
	w := call("a", "POST", "/api/v2/evaluations", a, `{"dataset_id":"customer-support","dataset_version":5,"agent_id":"support-safe","agent_version":1,"split":"development","trials":1,"scenario_ids":["cs-001"]}`)
	if w.Code != 202 {
		t.Fatal(w.Code, w.Body.String())
	}
	var accepted map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &accepted)
	id := accepted["id"]
	if got := call("b", "GET", "/api/v2/evaluations/"+id, b, "").Code; got != 404 {
		t.Fatalf("cross-project evaluation read=%d", got)
	}
	var trial string
	if err = pool.QueryRow(context.Background(), "SELECT id FROM benchmark_trials WHERE evaluation_id=$1", id).Scan(&trial); err != nil {
		t.Fatal(err)
	}
	if got := call("b", "GET", "/api/v2/trials/"+trial, b, "").Code; got != 404 {
		t.Fatalf("cross-project trial read=%d", got)
	}
	if got := call("b", "POST", "/api/v2/external/sessions/"+trial, b, "").Code; got != 404 {
		t.Fatalf("cross-project session=%d", got)
	}
}

func (v testOIDCVerifier) Verify(context.Context, string) (Principal, error) {
	return v.principal, v.err
}

func TestHostedModeRejectsMissingAndInvalidDeveloperTokens(t *testing.T) {
	handler := (&Server{HostedAuth: testOIDCVerifier{err: errors.New("bad token")}}).Routes()
	for _, header := range []string{"", "Bearer forged"} {
		request := httptest.NewRequest(http.MethodGet, "/api/v2/projects", nil)
		if header != "" {
			request.Header.Set("Authorization", header)
		}
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusUnauthorized {
			t.Fatalf("%q: %d", header, response.Code)
		}
	}
}

func TestHostedModeDisablesUnscopedLegacyRoutes(t *testing.T) {
	handler := (&Server{HostedAuth: testOIDCVerifier{principal: Principal{Issuer: "https://issuer.test", Subject: "developer"}}}).Routes()
	for _, path := range []string{"/api/v1/runs", "/api/v2/studies", "/api/v2/comparisons"} {
		request := httptest.NewRequest(http.MethodGet, path, nil)
		request.Header.Set("Authorization", "Bearer signed-token")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusNotFound {
			t.Fatalf("%s: %d", path, response.Code)
		}
	}
}

func TestValidateListenSecurity(t *testing.T) {
	for _, tc := range []struct {
		address string
		token   string
		valid   bool
	}{
		{"127.0.0.1:8080", "", false},
		{"[::1]:8080", "", false},
		{"localhost:8080", "", false},
		{"127.0.0.1:8080", strings.Repeat("x", 32), true},
		{"0.0.0.0:8080", "", false},
		{":8080", "", false},
		{"0.0.0.0:8080", strings.Repeat("x", 31), false},
		{"0.0.0.0:8080", strings.Repeat("x", 32), true},
		{"bad-address", strings.Repeat("x", 32), false},
	} {
		err := ValidateListenSecurity(tc.address, tc.token)
		if (err == nil) != tc.valid {
			t.Errorf("address %q, valid=%t, err=%v", tc.address, tc.valid, err)
		}
	}
}

func TestBearerAuthentication(t *testing.T) {
	const token = "local-test-token-with-at-least-32-characters"
	called := false
	handler := bearerAuthentication(token)(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusNoContent)
	}))
	for _, tc := range []struct {
		name   string
		header string
		status int
	}{{"missing", "", http.StatusUnauthorized}, {"wrong", "Bearer wrong", http.StatusUnauthorized}, {"malformed", token, http.StatusUnauthorized}, {"valid", "Bearer " + token, http.StatusNoContent}} {
		t.Run(tc.name, func(t *testing.T) {
			called = false
			request := httptest.NewRequest(http.MethodGet, "/", nil)
			if tc.header != "" {
				request.Header.Set("Authorization", tc.header)
			}
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			if response.Code != tc.status {
				t.Fatalf("status %d, want %d", response.Code, tc.status)
			}
			if called != (tc.status == http.StatusNoContent) {
				t.Fatalf("handler reached=%t", called)
			}
			if tc.status == http.StatusUnauthorized && response.Header().Get("Cache-Control") != "no-store" {
				t.Fatal("unauthorized response can be cached")
			}
		})
	}
}

func TestControlTokenRotationRejectsPreviousToken(t *testing.T) {
	oldToken := strings.Repeat("a", 32)
	newToken := strings.Repeat("b", 32)
	handler := bearerAuthentication(newToken)(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	for _, tc := range []struct {
		token string
		want  int
	}{{oldToken, http.StatusUnauthorized}, {newToken, http.StatusNoContent}} {
		request := httptest.NewRequest(http.MethodGet, "/", nil)
		request.Header.Set("Authorization", "Bearer "+tc.token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != tc.want {
			t.Fatalf("rotation returned %d, want %d", response.Code, tc.want)
		}
	}
}

func TestRoutesRequireBearerOnAllRouteFamilies(t *testing.T) {
	handler := (&Server{BearerToken: "a-token-long-enough-for-tests-0123456789", RequireAuth: true}).Routes()
	for _, path := range []string{"/healthz", "/api/v1/runs", "/api/v2/datasets"} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		if response.Code != http.StatusUnauthorized {
			t.Errorf("%s returned %d without credentials", path, response.Code)
		}
	}
}

func TestProtectedRoutesFailClosedWithoutConfiguredToken(t *testing.T) {
	handler := (&Server{RequireAuth: true}).Routes()
	for _, route := range []struct{ method, path string }{
		{http.MethodGet, "/healthz"},
		{http.MethodGet, "/api/v1/runs"},
		{http.MethodGet, "/api/v2/datasets"},
		{http.MethodPost, "/api/v2/external/sessions/00000000-0000-0000-0000-000000000001"},
	} {
		request := httptest.NewRequest(route.method, route.path, nil)
		request.Header.Set("Authorization", "Bearer ")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusServiceUnavailable {
			t.Errorf("%s returned %d without configured control token", route.path, response.Code)
		}
	}
}

func TestExternalResultReadGraceIsBounded(t *testing.T) {
	expires := time.Unix(1000, 0)
	if !externalResultReadable(expires, expires) {
		t.Fatal("result cannot be read immediately after action expiry")
	}
	if !externalResultReadable(expires, expires.Add(externalResultReadGrace-time.Nanosecond)) {
		t.Fatal("result closed before grace deadline")
	}
	if externalResultReadable(expires, expires.Add(externalResultReadGrace)) {
		t.Fatal("expired result credential remains valid")
	}
}

func TestParseAgent(t *testing.T) {
	for _, tc := range []struct {
		body  string
		valid bool
	}{{`{"agent":"safe"}`, true}, {`{"agent":"duplicate"}`, true}, {`{"agent":"unknown"}`, false}, {`{"agent":"safe","upload":"x"}`, false}, {`{"agent":"safe"}{}`, false}, {``, false}, {strings.Repeat("x", 2048), false}} {
		t.Run(tc.body[:min(len(tc.body), 40)], func(t *testing.T) {
			_, err := ParseAgent(httptest.NewRecorder(), httptest.NewRequest("POST", "/", strings.NewReader(tc.body)))
			if (err == nil) != tc.valid {
				t.Fatalf("unexpected result: %v", err)
			}
		})
	}
}
func TestReferences(t *testing.T) {
	for _, path := range []string{"scenarios/cancel-order.v1.json", "evaluators/cancellation.v1.json"} {
		ref := contracts.Load(path)
		if len(ref.SHA256) != 64 || ref.Version != 1 || !json.Valid(ref.Content) {
			t.Fatalf("invalid reference %s", path)
		}
	}
}
