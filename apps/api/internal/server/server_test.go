package server

import (
	"encoding/json"
	"github.com/vivekvx/AgentProof/packages/contracts"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

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
