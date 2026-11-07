package server

import (
	"context"
	"encoding/json"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/vivekvx/AgentProof/db"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
)

func TestRunLifecycleAPI(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("Set TEST_DATABASE_URL for API database integration")
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
	api := (&Server{Pool: pool}).Routes()
	created := httptest.NewRecorder()
	api.ServeHTTP(created, httptest.NewRequest("POST", "/api/v1/runs", strings.NewReader(`{"agent":"duplicate"}`)))
	if created.Code != 202 {
		t.Fatalf("create: %d %s", created.Code, created.Body.String())
	}
	var accepted struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(created.Body.Bytes(), &accepted); err != nil {
		t.Fatal(err)
	}
	defer pool.Exec(ctx, "DELETE FROM runs WHERE id=$1", accepted.ID)
	// A persisted queued run survives without a running dispatcher or worker.
	var status string
	if err := pool.QueryRow(ctx, "SELECT status FROM runs WHERE id=$1", accepted.ID).Scan(&status); err != nil || status != "queued" {
		t.Fatalf("queued record missing: %s %v", status, err)
	}
	inspected := httptest.NewRecorder()
	api.ServeHTTP(inspected, httptest.NewRequest("GET", "/api/v1/runs/"+accepted.ID, nil))
	if inspected.Code != 200 {
		t.Fatal(inspected.Body.String())
	}
	var run Run
	if err := json.Unmarshal(inspected.Body.Bytes(), &run); err != nil {
		t.Fatal(err)
	}
	if run.ID != accepted.ID || run.Status != "queued" || len(run.Events) != 0 || string(run.Result) != "null" {
		t.Fatalf("invalid run: %+v", run)
	}
	var manifest map[string]any
	if err := json.Unmarshal(run.Manifest, &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest["agent"] != "duplicate" || manifest["schema_version"] != "1.0" {
		t.Fatal(manifest)
	}
	for path, want := range map[string]int{"/api/v1/runs/not-valid": 400, "/api/v1/runs/00000000-0000-0000-0000-000000000000": 404, "/api/v1/runs": 200, "/healthz": 200} {
		response := httptest.NewRecorder()
		api.ServeHTTP(response, httptest.NewRequest("GET", path, nil))
		if response.Code != want {
			t.Fatalf("%s: got %d want %d", path, response.Code, want)
		}
	}
}

func TestRunCreationIdempotency(t *testing.T) {
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
	api := (&Server{Pool: pool}).Routes()
	key := "request-" + uuid.NewString()
	defer func() {
		var id string
		_ = pool.QueryRow(ctx, "DELETE FROM run_requests WHERE idempotency_key=$1 RETURNING run_id", key).Scan(&id)
		if id != "" {
			_, _ = pool.Exec(ctx, "DELETE FROM runs WHERE id=$1", id)
		}
	}()
	var group sync.WaitGroup
	responses := make(chan *httptest.ResponseRecorder, 8)
	for i := 0; i < 8; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			request := httptest.NewRequest("POST", "/api/v1/runs", strings.NewReader(`{"agent":"safe"}`))
			request.Header.Set("Idempotency-Key", key)
			response := httptest.NewRecorder()
			api.ServeHTTP(response, request)
			responses <- response
		}()
	}
	group.Wait()
	close(responses)
	id := ""
	for response := range responses {
		if response.Code != 202 {
			t.Fatal(response.Body.String())
		}
		var data map[string]string
		if err := json.Unmarshal(response.Body.Bytes(), &data); err != nil {
			t.Fatal(err)
		}
		if id == "" {
			id = data["id"]
		}
		if data["id"] != id {
			t.Fatal("repeated request created another run")
		}
	}
	request := httptest.NewRequest("POST", "/api/v1/runs", strings.NewReader(`{"agent":"duplicate"}`))
	request.Header.Set("Idempotency-Key", key)
	response := httptest.NewRecorder()
	api.ServeHTTP(response, request)
	if response.Code != 409 {
		t.Fatalf("changed request: %d", response.Code)
	}
}
