package server

import (
	"encoding/json"
	"github.com/vivekvx/AgentProof/packages/contracts"
	"net/http/httptest"
	"strings"
	"testing"
)

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
