package server

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/vivekvx/AgentProof/db"
)

func TestInteractionPublicationAndSelection(t *testing.T) {
	ref, data, err := builtinDataset(6)
	if err != nil {
		t.Fatal(err)
	}
	if ref.Version != 6 || len(data.Scenarios) != 5 {
		t.Fatalf("unexpected publication: version=%d cases=%d", ref.Version, len(data.Scenarios))
	}
	for _, test := range []struct {
		name, body string
		valid      bool
	}{
		{"development", `{"schema_version":"3.0","dataset_id":"customer-support","dataset_version":6,"agent_id":"support-interaction-safe","agent_version":1,"split":"development"}`, true},
		{"unsupported version", `{"schema_version":"3.0","dataset_id":"customer-support","dataset_version":7,"agent_id":"support-interaction-safe","agent_version":1}`, false},
		{"wrong schema", `{"schema_version":"2.0","dataset_id":"customer-support","dataset_version":6,"agent_id":"support-interaction-safe","agent_version":1}`, false},
		{"unsupported split", `{"schema_version":"3.0","dataset_id":"customer-support","dataset_version":6,"agent_id":"support-interaction-safe","agent_version":1,"split":"secret"}`, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			r := httptest.NewRequest("POST", "/api/v2/evaluations", strings.NewReader(test.body))
			_, err := decodeBenchmark(w, r)
			if (err == nil) != test.valid {
				t.Fatalf("valid=%v error=%v", test.valid, err)
			}
		})
	}
}

func TestInteractionAPICreatesImmutableTrialPlan(t *testing.T) {
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
	if err = db.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	api := (&Server{Pool: pool}).Routes()
	request := `{"schema_version":"3.0","dataset_id":"customer-support","dataset_version":6,"agent_id":"support-interaction-safe","agent_version":1,"split":"development","trials":1,"scenario_ids":["t07-001"]}`
	created := httptest.NewRecorder()
	api.ServeHTTP(created, httptest.NewRequest("POST", "/api/v2/evaluations", strings.NewReader(request)))
	if created.Code != 202 {
		t.Fatal(created.Body.String())
	}
	var accepted map[string]string
	if err := json.Unmarshal(created.Body.Bytes(), &accepted); err != nil {
		t.Fatal(err)
	}
	id := accepted["id"]
	defer func() {
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_evidence_heads WHERE trial_id IN (SELECT id FROM benchmark_trials WHERE evaluation_id=$1)", id)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_trials WHERE evaluation_id=$1", id)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_evaluations WHERE id=$1", id)
	}()
	var version int
	var tool string
	var count int
	err = pool.QueryRow(ctx, "SELECT (manifest->'evaluator'->>'version')::int,manifest->>'tool_schema_version' FROM benchmark_evaluations WHERE id=$1", id).Scan(&version, &tool)
	if err != nil || version != 5 || tool != "support-tools-v2" {
		t.Fatalf("wrong manifest: %d %s %v", version, tool, err)
	}
	err = pool.QueryRow(ctx, "SELECT count(*) FROM benchmark_trials WHERE evaluation_id=$1 AND scenario_id='t07-001'", id).Scan(&count)
	if err != nil || count != 1 {
		t.Fatalf("wrong trial plan: %d %v", count, err)
	}
	inspected := httptest.NewRecorder()
	api.ServeHTTP(inspected, httptest.NewRequest("GET", "/api/v2/evaluations/"+id, nil))
	if inspected.Code != 200 {
		t.Fatal(inspected.Body.String())
	}
	if _, err = pool.Exec(ctx, "UPDATE benchmark_datasets SET content='{}' WHERE id='customer-support' AND version=6"); err == nil {
		t.Fatal("published dataset bytes changed")
	}
	wrongSplit := strings.Replace(request, `"development"`, `"held-out"`, 1)
	rejected := httptest.NewRecorder()
	api.ServeHTTP(rejected, httptest.NewRequest("POST", "/api/v2/evaluations", strings.NewReader(wrongSplit)))
	if rejected.Code != 400 {
		t.Fatalf("held-out selection accepted: %d %s", rejected.Code, rejected.Body.String())
	}
}
