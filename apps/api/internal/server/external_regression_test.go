package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

type rejectingExternalWorkflow struct {
	mu      sync.Mutex
	updates int
}

func (*rejectingExternalWorkflow) Start(context.Context, string) error { return nil }
func (*rejectingExternalWorkflow) ExternalSession(context.Context, string, string) (externalTask, error) {
	return externalTask{}, nil
}
func (w *rejectingExternalWorkflow) ExternalUpdate(context.Context, string, string, string, any) (map[string]any, error) {
	w.mu.Lock()
	w.updates++
	w.mu.Unlock()
	return nil, fmt.Errorf("terminal session reached workflow")
}

func TestExternalTerminalSessionsFenceGoRequestsAfterRestart(t *testing.T) {
	for _, state := range []string{"completed", "expired", "cancelled", "waiting-expired", "waiting-stale"} {
		t.Run(state, func(t *testing.T) {
			pool := auditStudyDatabase(t)
			ctx := context.Background()
			evaluationID, trialID := uuid.NewString(), uuid.NewString()
			token := strings.Repeat("a", 64)
			digest := sha256.Sum256([]byte(token))
			hash := hex.EncodeToString(digest[:])
			if _, err := pool.Exec(ctx, "INSERT INTO benchmark_evaluations(id,manifest) VALUES($1,'{}')", evaluationID); err != nil {
				t.Fatal(err)
			}
			if _, err := pool.Exec(ctx, "INSERT INTO benchmark_trials(id,evaluation_id,scenario_id,trial_index,scenario,state) VALUES($1,$2,'cs-001',1,'{}','{}')", trialID, evaluationID); err != nil {
				t.Fatal(err)
			}
			storedState := state
			attempt := 1
			expires := time.Now().Add(time.Hour)
			if state == "waiting-expired" {
				storedState = "waiting"
				expires = time.Now().Add(-time.Second)
			}
			if state == "waiting-stale" {
				storedState = "waiting"
				attempt = 2
				newAttempt := sha256.Sum256([]byte(strings.Repeat("b", 64)))
				hash = hex.EncodeToString(newAttempt[:])
			}
			if _, err := pool.Exec(ctx, "INSERT INTO benchmark_external_sessions(trial_id,evaluation_id,scenario_id,attempt,state,credential_hash,expires_at) VALUES($1,$2,'cs-001',$3,$4,$5,$6)", trialID, evaluationID, attempt, storedState, hash, expires); err != nil {
				t.Fatal(err)
			}
			workflow := &rejectingExternalWorkflow{}
			// A fresh server instance represents an API restart against the committed session.
			handler := (&Server{Pool: pool, Starter: workflow, BearerToken: strings.Repeat("c", 32)}).Routes()
			path := "/agent/v1/sessions/" + trialID
			tool := fmt.Sprintf(`{"schema_version":"1.0","run_id":"%s","trial_id":"%s","sequence":1,"operation_id":"%s","tool":"get_order","arguments":{}}`, evaluationID, trialID, uuid.NewString())
			completion := fmt.Sprintf(`{"schema_version":"1.0","run_id":"%s","trial_id":"%s","last_sequence":0,"resolution":"completed"}`, evaluationID, trialID)
			for _, request := range []struct{ method, path, body string }{
				{"POST", path + "/tools", tool},
				{"POST", path + "/complete", completion},
				{"POST", path + "/complete", completion}, // duplicate completion
				{"DELETE", path, ""},
			} {
				httpRequest := httptest.NewRequest(request.method, request.path, strings.NewReader(request.body))
				httpRequest.Header.Set("Authorization", "Bearer "+token)
				response := httptest.NewRecorder()
				handler.ServeHTTP(response, httpRequest)
				want := 410
				if state == "waiting-stale" {
					want = 401
				}
				if response.Code != want {
					t.Fatalf("%s %s reached closed session: %d %s", request.method, request.path, response.Code, response.Body.String())
				}
			}
			var actualState string
			if err := pool.QueryRow(ctx, "SELECT state FROM benchmark_external_sessions WHERE trial_id=$1", trialID).Scan(&actualState); err != nil || actualState != storedState {
				t.Fatalf("session changed: %s %v", actualState, err)
			}
			var effects int
			if err := pool.QueryRow(ctx, "SELECT count(*) FROM benchmark_effects WHERE trial_id=$1", trialID).Scan(&effects); err != nil || effects != 0 {
				t.Fatalf("closed session added effects: %d %v", effects, err)
			}
			workflow.mu.Lock()
			updates := workflow.updates
			workflow.mu.Unlock()
			if updates != 0 {
				t.Fatalf("closed session reached workflow %d times", updates)
			}
		})
	}
}
