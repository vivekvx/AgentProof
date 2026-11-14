package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/vivekvx/AgentProof/db"
)

const studyCreateBody = `{"schema_version":"study-create-v1","plan_id":"agentproof-mocked-t16"}`
const ordinaryEvaluationBody = `{"dataset_id":"customer-support","dataset_version":5,"agent_id":"support-safe","agent_version":1,"split":"development","trials":1,"scenario_ids":["cs-001"]}`

// Each case owns a fresh database, including all of its synthetic study rows.
func auditStudyDatabase(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("Set TEST_DATABASE_URL")
	}
	adminConfig, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	adminConfig.ConnConfig.Database = "postgres"
	admin, err := pgxpool.NewWithConfig(context.Background(), adminConfig)
	if err != nil {
		t.Fatal(err)
	}
	name := "agentproof_audit_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err := admin.Exec(context.Background(), "CREATE DATABASE "+name); err != nil {
		admin.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, err := admin.Exec(context.Background(), "DROP DATABASE "+name+" WITH (FORCE)"); err != nil {
			t.Errorf("drop isolated database: %v", err)
		}
		admin.Close()
	})
	testConfig, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	testConfig.ConnConfig.Database = name
	pool, err := pgxpool.NewWithConfig(context.Background(), testConfig)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := db.Migrate(context.Background(), pool); err != nil {
		t.Fatal(err)
	}
	fixtures, err := filepath.Abs(filepath.Join("testdata", "study"))
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("AGENTPROOF_MODEL_CONFIG_DIR", fixtures)
	t.Setenv("AGENTPROOF_STUDY_PLAN_PATH", filepath.Join(fixtures, "mocked-plan.v1.json"))
	t.Setenv("AGENTPROOF_ENABLE_STUDY_DISPATCH", "1")
	return pool
}

func studyRequest(handler http.Handler, method, path, key, body string) (int, map[string]any) {
	request := httptest.NewRequest(method, path, strings.NewReader(body))
	if key != "" {
		request.Header.Set("Idempotency-Key", key)
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	var data map[string]any
	_ = json.Unmarshal(response.Body.Bytes(), &data)
	return response.Code, data
}

func TestStudySingleActiveAdmissionAcrossInstances(t *testing.T) {
	pool := auditStudyDatabase(t)
	first := (&Server{Pool: pool}).Routes()
	second := (&Server{Pool: pool}).Routes()
	type reply struct {
		code int
		body map[string]any
	}
	results := make(chan reply, 2)
	start := make(chan struct{})
	for _, handler := range []http.Handler{first, second} {
		handler := handler
		go func() {
			<-start
			code, body := studyRequest(handler, "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
			results <- reply{code, body}
		}()
	}
	close(start)
	accepted, rejected := 0, 0
	var id string
	for range 2 {
		r := <-results
		switch r.code {
		case 202:
			accepted++
			id = r.body["id"].(string)
		case 409:
			rejected++
		default:
			t.Fatalf("unexpected admission: %d %v", r.code, r.body)
		}
	}
	if accepted != 1 || rejected != 1 {
		t.Fatalf("simultaneous admissions: accepted=%d rejected=%d", accepted, rejected)
	}
	// A restarted API must still observe durable admission, including cancellation
	// while the original child evaluations have not finished.
	code, _ := studyRequest(second, "POST", "/api/v2/studies/"+id+"/cancel", "", "")
	if code != 202 {
		t.Fatalf("cancel: %d", code)
	}
	restarted := (&Server{Pool: pool}).Routes()
	code, _ = studyRequest(restarted, "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
	if code != 409 {
		t.Fatalf("cancelled but unfinished study admitted another: %d", code)
	}
	result := `{"schema_version":"2.0","verdict":"INDETERMINATE","failure_category":"infrastructure"}`
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_trials SET status='completed',result=$2 WHERE id IN (SELECT trial_id FROM benchmark_study_trials WHERE study_id=$1)", id, result); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_evaluations SET status='completed',result='{"+`"schema_version":"2.0"`+"}' WHERE id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", id); err != nil {
		t.Fatal(err)
	}
	code, next := studyRequest(restarted, "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
	if code != 202 || next["id"] == id {
		t.Fatalf("terminal study did not release admission: %d %v", code, next)
	}
}

func TestStudyAndOrdinaryEvaluationShareOneLocalEnvelope(t *testing.T) {
	pool := auditStudyDatabase(t)
	first := (&Server{Pool: pool}).Routes()
	code, study := studyRequest(first, "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
	if code != 202 {
		t.Fatalf("study create: %d %v", code, study)
	}
	code, ordinary := studyRequest((&Server{Pool: pool}).Routes(), "POST", "/api/v2/evaluations", uuid.NewString(), ordinaryEvaluationBody)
	if code != 409 || ordinary["error"] != "an active study owns the local evaluation envelope" {
		t.Fatalf("ordinary evaluation bypassed study envelope: %d %v", code, ordinary)
	}
	studyID := study["id"].(string)
	result := `{"schema_version":"2.0","verdict":"INDETERMINATE","failure_category":"infrastructure"}`
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_trials SET status='completed',result=$2 WHERE id IN (SELECT trial_id FROM benchmark_study_trials WHERE study_id=$1)", studyID, result); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_evaluations SET status='completed',result='{\"schema_version\":\"2.0\"}' WHERE id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", studyID); err != nil {
		t.Fatal(err)
	}
	code, ordinary = studyRequest((&Server{Pool: pool}).Routes(), "POST", "/api/v2/evaluations", uuid.NewString(), ordinaryEvaluationBody)
	if code != 202 {
		t.Fatalf("ordinary evaluation after terminal study: %d %v", code, ordinary)
	}
	// The durable queued evaluation remains an admission holder after an API restart.
	code, blocked := studyRequest((&Server{Pool: pool}).Routes(), "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
	if code != 409 || blocked["error"] != "an ordinary evaluation owns the local study envelope" {
		t.Fatalf("study bypassed stale ordinary admission: %d %v", code, blocked)
	}
	evaluationID := ordinary["id"].(string)
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_trials SET status='completed',result=$2 WHERE evaluation_id=$1", evaluationID, result); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_evaluations SET status='completed',result='{\"schema_version\":\"2.0\"}' WHERE id=$1", evaluationID); err != nil {
		t.Fatal(err)
	}
	code, next := studyRequest((&Server{Pool: pool}).Routes(), "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
	if code != 202 || next["id"] == studyID {
		t.Fatalf("terminal ordinary evaluation did not release study admission: %d %v", code, next)
	}
}

type blockedStudyStarter struct {
	mu      sync.Mutex
	calls   []string
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

type deadlineStudyStarter struct {
	mu    sync.Mutex
	calls []string
	fail  bool
}

func (s *deadlineStudyStarter) Start(context.Context, string) error { return nil }
func (s *deadlineStudyStarter) CancelBenchmark(_ context.Context, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = append(s.calls, id)
	if s.fail {
		return errors.New("synthetic cancellation delivery failure")
	}
	return nil
}

func TestStudyDeadlineCancelsAndRetriesAfterRestart(t *testing.T) {
	pool := auditStudyDatabase(t)
	code, body := studyRequest((&Server{Pool: pool}).Routes(), "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
	if code != 202 {
		t.Fatalf("create: %d %v", code, body)
	}
	id := body["id"].(string)
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_studies SET created_at=clock_timestamp()-interval '181 seconds' WHERE id=$1", id); err != nil {
		t.Fatal(err)
	}
	starter := &deadlineStudyStarter{}
	api := &Server{Pool: pool, Starter: starter}
	if err := api.expireStudiesOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	var status string
	if err := pool.QueryRow(context.Background(), "SELECT status FROM benchmark_studies WHERE id=$1", id).Scan(&status); err != nil || status != "cancelled" {
		t.Fatalf("deadline state: %s %v", status, err)
	}
	starter.mu.Lock()
	firstCalls := len(starter.calls)
	starter.mu.Unlock()
	if firstCalls != 2 {
		t.Fatalf("deadline signaled %d evaluations", firstCalls)
	}
	// A new API instance repeats the cancellation signal until child work ends.
	restarted := &Server{Pool: pool, Starter: starter}
	if err := restarted.expireStudiesOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	starter.mu.Lock()
	allCalls := len(starter.calls)
	starter.mu.Unlock()
	if allCalls != 4 {
		t.Fatalf("restart signaled %d total cancellations", allCalls)
	}
	code, _ = studyRequest(restarted.Routes(), "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
	if code != 409 {
		t.Fatalf("expired active study allowed admission: %d", code)
	}
}

func TestStudyDeadlineExposesDeliveryAndChildTerminalState(t *testing.T) {
	pool := auditStudyDatabase(t)
	code, body := studyRequest((&Server{Pool: pool}).Routes(), "POST", "/api/v2/studies", uuid.NewString(), studyCreateBody)
	if code != 202 {
		t.Fatalf("create: %d %v", code, body)
	}
	id := body["id"].(string)
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_studies SET created_at=clock_timestamp()-interval '181 seconds' WHERE id=$1", id); err != nil {
		t.Fatal(err)
	}
	failed := &deadlineStudyStarter{fail: true}
	if err := (&Server{Pool: pool, Starter: failed}).expireStudiesOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	code, observed := studyRequest((&Server{Pool: pool}).Routes(), "GET", "/api/v2/studies/"+id, "", "")
	if code != 200 {
		t.Fatalf("inspect failed deadline: %d %v", code, observed)
	}
	cancellation := observed["cancellation"].(map[string]any)
	if cancellation["deadline_reached_at"] == nil || cancellation["cancellation_requested_at"] == nil || cancellation["temporal_cancellation_acknowledged"] != false || cancellation["child_workflows_terminal"] != false || cancellation["study_terminal"] != false {
		t.Fatalf("deadline state is misleading: %v", cancellation)
	}
	accepted := &deadlineStudyStarter{}
	if err := (&Server{Pool: pool, Starter: accepted}).expireStudiesOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	_, observed = studyRequest((&Server{Pool: pool}).Routes(), "GET", "/api/v2/studies/"+id, "", "")
	cancellation = observed["cancellation"].(map[string]any)
	if cancellation["temporal_requests"] != float64(2) || cancellation["temporal_requests_accepted"] != float64(2) || cancellation["temporal_cancellation_acknowledged"] != true || cancellation["child_workflows_terminal"] != false {
		t.Fatalf("delivery acknowledgement differs: %v", cancellation)
	}
	result := `{"schema_version":"2.0","verdict":"INDETERMINATE","failure_category":"infrastructure"}`
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_trials SET status='completed',result=$2 WHERE id IN (SELECT trial_id FROM benchmark_study_trials WHERE study_id=$1)", id, result); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(context.Background(), "UPDATE benchmark_evaluations SET status='completed',result='{\"schema_version\":\"2.0\"}' WHERE id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", id); err != nil {
		t.Fatal(err)
	}
	_, observed = studyRequest((&Server{Pool: pool}).Routes(), "GET", "/api/v2/studies/"+id, "", "")
	cancellation = observed["cancellation"].(map[string]any)
	if cancellation["child_workflows_terminal"] != true || cancellation["study_terminal"] != true {
		t.Fatalf("terminal children not exposed: %v", cancellation)
	}
}

func (s *blockedStudyStarter) Start(context.Context, string) error { return nil }

func (s *blockedStudyStarter) StartBenchmark(ctx context.Context, id string) error {
	s.mu.Lock()
	s.calls = append(s.calls, id)
	s.mu.Unlock()
	s.once.Do(func() {
		close(s.entered)
		select {
		case <-s.release:
		case <-ctx.Done():
		}
	})
	return errors.New("injected dispatch failure")
}

func TestStudySameKeyConcurrentAdmissionAndDispatchRetry(t *testing.T) {
	pool := auditStudyDatabase(t)
	starter := &blockedStudyStarter{entered: make(chan struct{}), release: make(chan struct{})}
	handler := (&Server{Pool: pool, Starter: starter}).Routes()
	key := "study-audit-" + uuid.NewString()
	type reply struct {
		code int
		body map[string]any
	}
	first := make(chan reply, 1)
	go func() {
		code, body := studyRequest(handler, "POST", "/api/v2/studies", key, studyCreateBody)
		first <- reply{code, body}
	}()
	select {
	case <-starter.entered: // Admission committed; dispatch remains blocked.
	case <-time.After(10 * time.Second):
		t.Fatal("first dispatch was not reached")
	}
	const contenders = 8
	results := make(chan reply, contenders)
	start := make(chan struct{})
	for i := 0; i < contenders; i++ {
		go func() {
			<-start
			code, body := studyRequest(handler, "POST", "/api/v2/studies", key, studyCreateBody)
			results <- reply{code, body}
		}()
	}
	close(start)
	originalID := ""
	var originalEvaluations []any
	for i := 0; i < contenders; i++ {
		select {
		case result := <-results:
			if result.code != 202 {
				t.Fatalf("concurrent retry: %d %v", result.code, result.body)
			}
			if originalID == "" {
				originalID, _ = result.body["id"].(string)
				originalEvaluations, _ = result.body["evaluation_ids"].([]any)
			}
			if result.body["id"] != originalID || fmt.Sprint(result.body["evaluation_ids"]) != fmt.Sprint(originalEvaluations) || result.body["status"] != "running" {
				t.Fatalf("retry changed committed identity or status: %v", result.body)
			}
		case <-time.After(10 * time.Second):
			t.Fatal("retry waited for workflow dispatch")
		}
	}
	code, _ := studyRequest(handler, "POST", "/api/v2/studies", key, `{"schema_version":"study-create-v1","plan_id":"another-plan"}`)
	if code != 409 {
		t.Fatalf("altered payload returned %d, want 409", code)
	}
	close(starter.release)
	select {
	case result := <-first:
		if result.code != 202 || result.body["id"] != originalID || fmt.Sprint(result.body["evaluation_ids"]) != fmt.Sprint(originalEvaluations) {
			t.Fatalf("original response changed: %+v", result)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("original request did not return")
	}
	for table, want := range map[string]int{"benchmark_studies": 1, "benchmark_study_create_keys": 1, "benchmark_study_evaluations": 2, "benchmark_evaluations": 2, "benchmark_trials": 8, "benchmark_study_trials": 8, "benchmark_study_reservations": 8} {
		var count int
		if err := pool.QueryRow(context.Background(), "SELECT count(*) FROM "+table).Scan(&count); err != nil || count != want {
			t.Fatalf("%s: count %d, want %d: %v", table, count, want, err)
		}
	}
	starter.mu.Lock()
	calls := append([]string(nil), starter.calls...)
	starter.mu.Unlock()
	if len(calls) != 2 {
		t.Fatalf("same-key retries scheduled %d starts, want two original evaluations", len(calls))
	}
	// A new API instance sees the durable key after the first dispatch failed.
	restarted := (&Server{Pool: pool}).Routes()
	code, body := studyRequest(restarted, "POST", "/api/v2/studies", key, studyCreateBody)
	if code != 202 || body["id"] != originalID || fmt.Sprint(body["evaluation_ids"]) != fmt.Sprint(originalEvaluations) {
		t.Fatalf("API restart changed study identity: %d %v", code, body)
	}
}

func TestStudyCommittedSnapshotsAndTerminalRetries(t *testing.T) {
	for _, terminal := range []string{"completed", "cancelled"} {
		t.Run(terminal, func(t *testing.T) {
			pool := auditStudyDatabase(t)
			handler := (&Server{Pool: pool}).Routes()
			key := "study-terminal-" + uuid.NewString()
			code, created := studyRequest(handler, "POST", "/api/v2/studies", key, studyCreateBody)
			if code != 202 {
				t.Fatalf("create: %d %v", code, created)
			}
			id := created["id"].(string)
			path := "/api/v2/studies/" + id
			if terminal == "cancelled" {
				code, body := studyRequest(handler, "POST", path+"/cancel", "", "")
				if code != 202 || body["status"] != "cancelled" {
					t.Fatalf("cancel: %d %v", code, body)
				}
				if _, err := pool.Exec(context.Background(), "UPDATE benchmark_studies SET status='running' WHERE id=$1", id); err == nil {
					t.Fatal("cancelled study returned to running")
				}
			} else {
				tx, err := pool.Begin(context.Background())
				if err != nil {
					t.Fatal(err)
				}
				defer tx.Rollback(context.Background())
				result := `{"schema_version":"2.0","verdict":"PASS","failure_category":null}`
				if _, err := tx.Exec(context.Background(), "UPDATE benchmark_trials SET status='completed',result=$2 WHERE evaluation_id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", id, result); err != nil {
					t.Fatal(err)
				}
				if _, err := tx.Exec(context.Background(), "UPDATE benchmark_evaluations SET status='completed',result='{\"schema_version\":\"2.0\"}' WHERE id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", id); err != nil {
					t.Fatal(err)
				}
				// Both readers must use the pre-commit snapshot while child writes are pending.
				for _, method := range []string{"GET", "POST"} {
					target, requestKey, payload := path, "", ""
					if method == "POST" {
						target, requestKey, payload = "/api/v2/studies", key, studyCreateBody
					}
					code, body := studyRequest(handler, method, target, requestKey, payload)
					if (method == "GET" && code != 200) || (method == "POST" && code != 202) || body["status"] != "running" {
						t.Fatalf("uncommitted children leaked to %s: %d %v", method, code, body)
					}
				}
				if err := tx.Commit(context.Background()); err != nil {
					t.Fatal(err)
				}
				code, _ = studyRequest(handler, "POST", path+"/cancel", "", "")
				if code != 409 {
					t.Fatalf("completed study accepted cancellation: %d", code)
				}
			}
			for _, method := range []string{"GET", "POST"} {
				target, requestKey, payload := path, "", ""
				wantCode := 200
				if method == "POST" {
					target, requestKey, payload, wantCode = "/api/v2/studies", key, studyCreateBody, 202
				}
				code, body := studyRequest(handler, method, target, requestKey, payload)
				if code != wantCode || body["id"] != id || body["status"] != terminal || fmt.Sprint(body["evaluation_ids"]) != fmt.Sprint(created["evaluation_ids"]) {
					t.Fatalf("%s terminal snapshot: %d %v", method, code, body)
				}
			}
		})
	}
}

func TestStudyCancellationRacingWithFinalization(t *testing.T) {
	pool := auditStudyDatabase(t)
	handler := (&Server{Pool: pool}).Routes()
	key := "study-race-" + uuid.NewString()
	code, created := studyRequest(handler, "POST", "/api/v2/studies", key, studyCreateBody)
	if code != 202 {
		t.Fatalf("create: %d %v", code, created)
	}
	id := created["id"].(string)
	ctx := context.Background()
	gate, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Rollback(ctx)
	if _, err := gate.Exec(ctx, "SELECT id FROM benchmark_studies WHERE id=$1 FOR UPDATE", id); err != nil {
		t.Fatal(err)
	}
	cancelled := make(chan int, 1)
	go func() {
		code, _ := studyRequest(handler, "POST", "/api/v2/studies/"+id+"/cancel", "", "")
		cancelled <- code
	}()
	// Wait for the cancellation statement to reach the row lock, not for a timer.
	deadline, stop := context.WithTimeout(ctx, 10*time.Second)
	defer stop()
	for {
		var waiting int
		err = pool.QueryRow(deadline, `SELECT count(*) FROM pg_stat_activity
			WHERE wait_event_type='Lock' AND query LIKE 'SELECT status FROM benchmark_studies WHERE id=%FOR UPDATE'`).Scan(&waiting)
		if err != nil {
			t.Fatal(err)
		}
		if waiting > 0 {
			break
		}
		select {
		case <-deadline.Done():
			t.Fatal("cancellation did not reach study row lock")
		default:
		}
	}
	result := `{"schema_version":"2.0","verdict":"PASS","failure_category":null}`
	if _, err := pool.Exec(ctx, "UPDATE benchmark_trials SET status='completed',result=$2 WHERE evaluation_id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", id, result); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, "UPDATE benchmark_evaluations SET status='completed',result='{\"schema_version\":\"2.0\"}' WHERE id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", id); err != nil {
		t.Fatal(err)
	}
	if err := gate.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case code := <-cancelled:
		if code != 409 {
			t.Fatalf("cancellation committed after all children finalized: %d", code)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("cancellation did not finish")
	}
	for _, method := range []string{"GET", "POST"} {
		target, requestKey, body := "/api/v2/studies/"+id, "", ""
		if method == "POST" {
			target, requestKey, body = "/api/v2/studies", key, studyCreateBody
		}
		_, response := studyRequest(handler, method, target, requestKey, body)
		if response["status"] != "completed" {
			t.Fatalf("%s reversed terminal finalization: %v", method, response)
		}
	}
}

func TestStudyCancellationWinsPendingFinalization(t *testing.T) {
	pool := auditStudyDatabase(t)
	handler := (&Server{Pool: pool}).Routes()
	key := "study-cancel-first-" + uuid.NewString()
	code, created := studyRequest(handler, "POST", "/api/v2/studies", key, studyCreateBody)
	if code != 202 {
		t.Fatalf("create: %d %v", code, created)
	}
	id := created["id"].(string)
	ctx := context.Background()
	finalization, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer finalization.Rollback(ctx)
	result := `{"schema_version":"2.0","verdict":"PASS","failure_category":null}`
	if _, err := finalization.Exec(ctx, "UPDATE benchmark_trials SET status='completed',result=$2 WHERE evaluation_id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", id, result); err != nil {
		t.Fatal(err)
	}
	if _, err := finalization.Exec(ctx, "UPDATE benchmark_evaluations SET status='completed',result='{\"schema_version\":\"2.0\"}' WHERE id IN (SELECT evaluation_id FROM benchmark_study_evaluations WHERE study_id=$1)", id); err != nil {
		t.Fatal(err)
	}
	// Finalization has not committed; cancellation must see the pending snapshot.
	code, body := studyRequest(handler, "POST", "/api/v2/studies/"+id+"/cancel", "", "")
	if code != 202 || body["status"] != "cancelled" {
		t.Fatalf("pending finalization blocked cancellation: %d %v", code, body)
	}
	if err := finalization.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	for _, method := range []string{"GET", "POST"} {
		target, requestKey, payload := "/api/v2/studies/"+id, "", ""
		if method == "POST" {
			target, requestKey, payload = "/api/v2/studies", key, studyCreateBody
		}
		_, body := studyRequest(handler, method, target, requestKey, payload)
		if body["status"] != "cancelled" || body["id"] != id {
			t.Fatalf("%s reversed cancellation: %v", method, body)
		}
	}
	code, _ = studyRequest(handler, "POST", "/api/v2/studies/"+id+"/cancel", "", "")
	if code != 409 {
		t.Fatalf("duplicate cancellation returned %d", code)
	}
}
