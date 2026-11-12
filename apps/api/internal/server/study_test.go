package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/vivekvx/AgentProof/db"
)

func TestCompletedStudyCannotBeCancelled(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("Set TEST_DATABASE_URL")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if err := db.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	studyID, evaluationID, trialID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	planID := "completed-study-test-" + strings.ReplaceAll(studyID, "-", "")
	plan, _ := json.Marshal(map[string]string{"id": planID})
	_, err = pool.Exec(ctx, "INSERT INTO benchmark_study_budgets(study_id,limits) VALUES($1,'{}')", studyID)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_trials WHERE id=$1", trialID)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_study_evaluations WHERE study_id=$1", studyID)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_evaluations WHERE id=$1", evaluationID)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_studies WHERE id=$1", studyID)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_study_budgets WHERE study_id=$1", studyID)
	}()
	_, err = pool.Exec(ctx, "INSERT INTO benchmark_studies(id,plan_id,plan_sha256,plan) VALUES($1,$2,$3,$4)", studyID, planID, strings.Repeat("0", 64), plan)
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, "INSERT INTO benchmark_evaluations(id,status,manifest,result) VALUES($1,'completed','{}','{\"schema_version\":\"2.0\"}')", evaluationID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, "INSERT INTO benchmark_study_evaluations(study_id,evaluation_id,profile_id) VALUES($1,$2,'test-profile')", studyID, evaluationID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, "INSERT INTO benchmark_trials(id,evaluation_id,scenario_id,trial_index,status,scenario,state,result) VALUES($1,$2,'cs-001',1,'completed','{}','{}','{\"schema_version\":\"2.0\",\"verdict\":\"PASS\",\"failure_category\":null}')", trialID, evaluationID)
	if err != nil {
		t.Fatal(err)
	}
	handler := (&Server{Pool: pool}).Routes()
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/api/v2/studies/"+studyID+"/cancel", nil))
	if response.Code != http.StatusConflict {
		t.Fatalf("completed study cancellation returned %d: %s", response.Code, response.Body.String())
	}
	var status string
	if err = pool.QueryRow(ctx, "SELECT status FROM benchmark_studies WHERE id=$1", studyID).Scan(&status); err != nil || status != "running" {
		t.Fatalf("completed study state changed: %s %v", status, err)
	}
}

func TestStudyWaitsForParentEvaluationFinalization(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("Set TEST_DATABASE_URL")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if err := db.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	studyID := uuid.NewString()
	planID := "pending-parent-test-" + strings.ReplaceAll(studyID, "-", "")
	evaluations := []string{uuid.NewString(), uuid.NewString()}
	trials := make([]string, 8)
	_, err = pool.Exec(ctx, "INSERT INTO benchmark_study_budgets(study_id,limits) VALUES($1,'{}')", studyID)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		for _, id := range trials {
			if id != "" {
				_, _ = pool.Exec(ctx, "DELETE FROM benchmark_trials WHERE id=$1", id)
			}
		}
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_study_evaluations WHERE study_id=$1", studyID)
		for _, id := range evaluations {
			_, _ = pool.Exec(ctx, "DELETE FROM benchmark_evaluations WHERE id=$1", id)
		}
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_studies WHERE id=$1", studyID)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_study_budgets WHERE study_id=$1", studyID)
	}()
	plan, _ := json.Marshal(map[string]string{"id": planID})
	_, err = pool.Exec(ctx, "INSERT INTO benchmark_studies(id,plan_id,plan_sha256,plan) VALUES($1,$2,$3,$4)", studyID, planID, strings.Repeat("0", 64), plan)
	if err != nil {
		t.Fatal(err)
	}
	for profile, evaluationID := range evaluations {
		_, err = pool.Exec(ctx, "INSERT INTO benchmark_evaluations(id,status,manifest) VALUES($1,'running','{}')", evaluationID)
		if err != nil {
			t.Fatal(err)
		}
		_, err = pool.Exec(ctx, "INSERT INTO benchmark_study_evaluations(study_id,evaluation_id,profile_id) VALUES($1,$2,$3)", studyID, evaluationID, fmt.Sprintf("profile-%d", profile))
		if err != nil {
			t.Fatal(err)
		}
		for trial := 0; trial < 4; trial++ {
			id := uuid.NewString()
			trials[profile*4+trial] = id
			_, err = pool.Exec(ctx, "INSERT INTO benchmark_trials(id,evaluation_id,scenario_id,trial_index,status,scenario,state,result) VALUES($1,$2,$3,$4,'completed','{}','{}','{\"schema_version\":\"2.0\",\"verdict\":\"PASS\",\"failure_category\":null}')", id, evaluationID, []string{"cs-001", "cs-016"}[trial/2], trial%2+1)
			if err != nil {
				t.Fatal(err)
			}
		}
	}
	handler := (&Server{Pool: pool}).Routes()
	read := func() map[string]any {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/v2/studies/"+studyID, nil))
		if response.Code != http.StatusOK {
			t.Fatalf("study read returned %d: %s", response.Code, response.Body.String())
		}
		var body map[string]any
		if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		return body
	}
	if body := read(); body["status"] != "running" || body["pending_evaluations"] != float64(2) {
		t.Fatalf("parent evaluations were omitted from completion: %v", body)
	}
	for _, evaluationID := range evaluations {
		_, err = pool.Exec(ctx, "UPDATE benchmark_evaluations SET status='completed',result='{\"schema_version\":\"2.0\"}' WHERE id=$1", evaluationID)
		if err != nil {
			t.Fatal(err)
		}
	}
	if body := read(); body["status"] != "completed" || body["pending_evaluations"] != float64(0) {
		t.Fatalf("completed parent evaluations were not recognized: %v", body)
	}
	if _, err = pool.Exec(ctx, "DELETE FROM benchmark_trials WHERE id=$1", trials[0]); err != nil {
		t.Fatal(err)
	}
	if body := read(); body["status"] != "running" || body["completed_trials"] != float64(7) {
		t.Fatalf("missing child trial produced a terminal study: %v", body)
	}
	// A missing child binding must not be interpreted as a complete study.
	if _, err = pool.Exec(ctx, "DELETE FROM benchmark_study_evaluations WHERE study_id=$1 AND evaluation_id=$2", studyID, evaluations[0]); err != nil {
		t.Fatal(err)
	}
	if body := read(); body["status"] != "running" || body["completed_trials"] != float64(4) {
		t.Fatalf("missing child evaluation produced a terminal study: %v", body)
	}
}
