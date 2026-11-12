package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/vivekvx/AgentProof/db"
)

func TestBenchmarkContractsAndInput(t *testing.T) {
	_, ds, err := builtinDataset()
	if err != nil {
		t.Fatal(err)
	}
	if len(ds.Scenarios) != 32 {
		t.Fatal("expected reviewed dataset")
	}
	for _, body := range []string{
		`{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-safe","agent_version":1}`,
		`{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-blind","agent_version":1,"trials":2,"split":"held-out"}`,
	} {
		if _, err := decodeBenchmark(httptest.NewRecorder(), httptest.NewRequest("POST", "/", strings.NewReader(body))); err != nil {
			t.Fatal(err)
		}
	}
	for _, body := range []string{`{}`, `{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-safe","agent_version":1,"trials":null}`, `{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-safe","agent_version":1,"trials":true}`, `{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-safe","agent_version":1,"scenario_ids":null}`, `{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-safe","agent_version":1,"trials":6}`, `{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-safe","agent_version":1,"seed":42}`, `{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-safe","agent_version":1,"callback_url":"http://169.254.169.254/latest/meta-data"}`} {
		if _, err := decodeBenchmark(httptest.NewRecorder(), httptest.NewRequest("POST", "/", strings.NewReader(body))); err == nil {
			t.Fatal("accepted invalid benchmark configuration", body)
		}
	}
}
func TestPairedComparisonCoverageAndCompatibility(t *testing.T) {
	manifest := benchmarkManifest{SchemaVersion: "2.0", ScenarioIDs: []string{"cs-001", "cs-002", "cs-003", "cs-004"}, ToolSchemaVersion: "support-tools-v1"}
	manifest.Dataset.ID = "customer-support"
	manifest.Dataset.Version = 1
	manifest.Dataset.SHA256 = "same"
	manifest.Evaluator.SHA256 = "eval"
	raw, _ := json.Marshal(manifest)
	left := benchmarkView{ID: "left", Manifest: raw, Status: "completed", Trials: []trialView{{ScenarioID: "cs-001", Index: 1, Result: json.RawMessage(`{"verdict":"PASS"}`)}, {ScenarioID: "cs-002", Index: 1, Result: json.RawMessage(`{"verdict":"FAIL"}`)}, {ScenarioID: "cs-003", Index: 1, Result: json.RawMessage(`{"verdict":"INDETERMINATE"}`)}, {ScenarioID: "cs-004", Index: 1, Result: json.RawMessage(`{"verdict":"PASS"}`)}}}
	right := benchmarkView{ID: "right", Manifest: raw, Status: "completed", Trials: []trialView{{ScenarioID: "cs-001", Index: 1, Result: json.RawMessage(`{"verdict":"FAIL"}`)}, {ScenarioID: "cs-002", Index: 1, Result: json.RawMessage(`{"verdict":"PASS"}`)}, {ScenarioID: "cs-003", Index: 1, Result: json.RawMessage(`{"verdict":"PASS"}`)}}}
	result, err := compareViews(left, right)
	if err != nil {
		t.Fatal(err)
	}
	counts := result["counts"].(map[string]int)
	if counts["improvement"] != 1 || counts["regression"] != 1 || counts["indeterminate"] != 1 || counts["missing"] != 1 {
		t.Fatal(counts)
	}
	manifest.Dataset.SHA256 = "changed"
	right.Manifest, _ = json.Marshal(manifest)
	if _, err = compareViews(left, right); err == nil {
		t.Fatal("accepted changed dataset")
	}
	manifest.Dataset.SHA256 = "same"
	manifest.Evaluator.SHA256 = "changed"
	right.Manifest, _ = json.Marshal(manifest)
	if _, err = compareViews(left, right); err == nil {
		t.Fatal("accepted changed evaluator")
	}
}
func TestPairedComparisonLabelsRealModelsAndExclusions(t *testing.T) {
	manifest := benchmarkManifest{SchemaVersion: "2.0", ScenarioIDs: []string{"cs-001", "cs-002"}, ToolSchemaVersion: "support-tools-v1"}
	manifest.Dataset.ID, manifest.Dataset.Version, manifest.Dataset.SHA256 = "customer-support", 5, "dataset"
	manifest.Evaluator.SHA256, manifest.ToolSchema.SHA256 = "evaluator", "tools"
	manifest.Agent.Content = json.RawMessage(`{"policy":"llm","model_configuration":{"model":"openai/gpt-oss-20b"}}`)
	leftRaw, _ := json.Marshal(manifest)
	manifest.Agent.Content = json.RawMessage(`{"policy":"llm","model_configuration":{"model":"openai/gpt-oss-120b"}}`)
	rightRaw, _ := json.Marshal(manifest)
	left := benchmarkView{ID: "left", Manifest: leftRaw, Status: "completed", Trials: []trialView{{ScenarioID: "cs-001", Index: 1, Result: json.RawMessage(`{"verdict":"PASS"}`)}, {ScenarioID: "cs-002", Index: 1, Result: json.RawMessage(`{"verdict":"INDETERMINATE"}`)}}}
	right := benchmarkView{ID: "right", Manifest: rightRaw, Status: "completed", Trials: []trialView{{ScenarioID: "cs-001", Index: 1, Result: json.RawMessage(`{"verdict":"FAIL"}`)}, {ScenarioID: "cs-002", Index: 1}}}
	result, err := compareViews(left, right)
	if err != nil {
		t.Fatal(err)
	}
	if result["comparison_kind"] != "real_model" || result["baseline_model"] != "openai/gpt-oss-20b" || result["candidate_model"] != "openai/gpt-oss-120b" {
		t.Fatal("model comparison was mislabeled")
	}
	counts := result["counts"].(map[string]int)
	if counts["regression"] != 1 || counts["missing"] != 1 || result["comparable_count"] != 1 {
		t.Fatal("comparison exclusions differ from raw pairs", counts)
	}
	for _, note := range result["notes"].([]string) {
		if strings.Contains(note, "offline reference outcomes") {
			t.Fatal("real-model comparison has an offline label")
		}
	}
}
func TestMatchedExperimentBudgetGate(t *testing.T) {
	profile := func(model string, requests int) json.RawMessage {
		return json.RawMessage(fmt.Sprintf(`{"policy":"llm","prompt":"same task prompt","prompt_version":"prompt-v1","max_retries":0,"model_configuration":{"provider":"openai-compatible","model":%q,"base_url":"https://api.groq.com/openai/v1","tool_calling":true,"timeout_seconds":10,"temperature":0,"top_p":1,"max_output_tokens":1024,"max_requests":%d,"max_tool_calls":10,"max_tokens":64000,"max_estimated_cost_usd":0.1}}`, model, requests))
	}
	m := benchmarkManifest{SchemaVersion: "2.0", ScenarioIDs: []string{"cs-001"}, ToolSchemaVersion: "support-tools-v1", Trials: 2, Split: "development"}
	m.Dataset.ID, m.Dataset.Version, m.Dataset.SHA256 = "customer-support", 5, "dataset"
	m.Evaluator.SHA256, m.ToolSchema.SHA256 = "evaluator", "tools"
	m.Agent.Content = profile("openai/gpt-oss-20b", 6)
	lraw, _ := json.Marshal(m)
	m.Agent.Content = profile("openai/gpt-oss-120b", 6)
	rraw, _ := json.Marshal(m)
	trials := []trialView{{ScenarioID: "cs-001", Index: 1, Result: json.RawMessage(`{"verdict":"PASS"}`)}, {ScenarioID: "cs-001", Index: 2, Result: json.RawMessage(`{"verdict":"INDETERMINATE"}`)}}
	left := benchmarkView{Manifest: lraw, Status: "completed", Trials: trials}
	right := benchmarkView{Manifest: rraw, Status: "completed", Trials: trials}
	result, err := compareViews(left, right)
	if err != nil || result["experiment_eligible"] != true {
		t.Fatalf("matched profiles rejected: %v %v", result, err)
	}
	right.Trials = trials[:1]
	result, err = compareViews(left, right)
	if err != nil || result["experiment_eligible"] != false || result["counts"].(map[string]int)["missing"] != 1 {
		t.Fatalf("missing repeat accepted: %v %v", result, err)
	}
	right.Trials = trials
	m.Agent.Content = profile("openai/gpt-oss-120b", 5)
	right.Manifest, _ = json.Marshal(m)
	result, err = compareViews(left, right)
	if err != nil || result["experiment_eligible"] != false || len(result["experiment_mismatch_reasons"].([]string)) == 0 {
		t.Fatalf("unequal request budget accepted: %v %v", result, err)
	}
	m.Agent.Content = profile("openai/gpt-oss-120b", 6)
	m.Trials = 1
	right.Manifest, _ = json.Marshal(m)
	result, err = compareViews(left, right)
	if err != nil || result["experiment_eligible"] != false {
		t.Fatalf("unequal repeats accepted: %v %v", result, err)
	}
}
func TestBenchmarkAPIPersistenceAndPublication(t *testing.T) {
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
	catalog := httptest.NewRecorder()
	api.ServeHTTP(catalog, httptest.NewRequest("GET", "/api/v2/datasets", nil))
	if catalog.Code != 200 {
		t.Fatal(catalog.Body.String())
	}
	if strings.Contains(catalog.Body.String(), "assertions") || strings.Contains(catalog.Body.String(), "refunded_cents") {
		t.Fatal("catalog exposed private fixture data")
	}
	body := `{"dataset_id":"customer-support","dataset_version":2,"agent_id":"support-safe","agent_version":1,"split":"development","trials":2,"scenario_ids":["cs-015","cs-016"]}`
	key := uuid.NewString()
	create := func(body string) *httptest.ResponseRecorder {
		response := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/api/v2/evaluations", strings.NewReader(body))
		req.Header.Set("Idempotency-Key", key)
		api.ServeHTTP(response, req)
		return response
	}
	response := create(body)
	if response.Code != 202 {
		t.Fatal(response.Body.String())
	}
	var accepted map[string]string
	_ = json.Unmarshal(response.Body.Bytes(), &accepted)
	id := accepted["id"]
	defer func() {
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_requests WHERE evaluation_id=$1", id)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_evidence_heads WHERE trial_id IN (SELECT id FROM benchmark_trials WHERE evaluation_id=$1)", id)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_trials WHERE evaluation_id=$1", id)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_evaluations WHERE id=$1", id)
	}()
	repeated := create(body)
	if repeated.Code != 202 || repeated.Body.String() != response.Body.String() {
		t.Fatal("request was not idempotent")
	}
	if create(strings.Replace(body, "support-safe", "support-blind", 1)).Code != 409 {
		t.Fatal("changed request accepted with same key")
	}
	var n int
	if err = pool.QueryRow(ctx, "SELECT count(*) FROM benchmark_trials WHERE evaluation_id=$1", id).Scan(&n); err != nil || n != 4 {
		t.Fatalf("trial plan: %d %v", n, err)
	}
	inspected := httptest.NewRecorder()
	api.ServeHTTP(inspected, httptest.NewRequest("GET", "/api/v2/evaluations/"+id, nil))
	if inspected.Code != 200 {
		t.Fatal(inspected.Body.String())
	}
	if _, err = pool.Exec(ctx, "UPDATE benchmark_datasets SET content='{}' WHERE id='customer-support' AND version=2"); err == nil {
		t.Fatal("publication was mutable")
	}
	if create(strings.Replace(body, "cs-016", "cs-025", 1)).Code != 400 {
		t.Fatal("accepted held-out scenario through development split")
	}
}

func TestRealModelConfigurationDiscovery(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("AGENTPROOF_MODEL_CONFIG_DIR", directory)
	raw := `{"schema_version":"2.0","id":"test-real","version":1,"title":"Test real model","policy":"llm","model_configuration":{"provider":"openai-compatible","model":"fixture-model","base_url":"https://mock.invalid/v1","api_key_env":"EVALUATION_KEY","tool_calling":true,"input_usd_per_million":1.0,"output_usd_per_million":2.0,"pricing_source":"https://example.org/pricing","pricing_checked_at":"2026-10-31"},"prompt":"Use only approved simulated order tools.","prompt_version":"support-prompt-v1","tool_schema_version":"support-tools-v1","max_retries":0}`
	if err := os.WriteFile(filepath.Join(directory, "test-real.v1.json"), []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	ref, err := loadAgent("test-real")
	if err != nil || ref.ID != "test-real" || ref.SHA256 == "" {
		t.Fatalf("load real config: %v", err)
	}
	if _, err = loadAgent("../test-real"); err == nil {
		t.Fatal("accepted path traversal")
	}
	bad := strings.Replace(raw, `"tool_calling":true`, `"tool_calling":false`, 1)
	if err := os.WriteFile(filepath.Join(directory, "test-real.v1.json"), []byte(bad), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = loadAgent("test-real"); err == nil {
		t.Fatal("accepted unsupported capabilities")
	}
	bad = strings.Replace(raw, `"tool_calling":true`, `"tool_calling":true,"api_key":"fixture-key"`, 1)
	if err := os.WriteFile(filepath.Join(directory, "test-real.v1.json"), []byte(bad), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = loadAgent("test-real"); err == nil {
		t.Fatal("accepted embedded credential")
	}
}

func TestAuditedAndLegacyBenchmarkReferences(t *testing.T) {
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
	for _, version := range []int{2, 3, 4, 5} {
		raw := fmt.Sprintf(`{"dataset_id":"customer-support","dataset_version":%d,"agent_id":"support-safe","agent_version":1,"split":"development","trials":1,"scenario_ids":["cs-024"]}`, version)
		response := httptest.NewRecorder()
		api.ServeHTTP(response, httptest.NewRequest("POST", "/api/v2/evaluations", strings.NewReader(raw)))
		if response.Code != 202 {
			t.Fatal(response.Body.String())
		}
		var accepted map[string]string
		_ = json.Unmarshal(response.Body.Bytes(), &accepted)
		id := accepted["id"]
		var manifest benchmarkManifest
		var stored []byte
		if err = pool.QueryRow(ctx, "SELECT manifest FROM benchmark_evaluations WHERE id=$1", id).Scan(&stored); err != nil {
			t.Fatal(err)
		}
		if err = json.Unmarshal(stored, &manifest); err != nil {
			t.Fatal(err)
		}
		expected := version - 1
		if manifest.Dataset.Version != version || manifest.Evaluator.Version != expected {
			t.Fatalf("wrong version pairing: %+v", manifest)
		}
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_evidence_heads WHERE trial_id IN (SELECT id FROM benchmark_trials WHERE evaluation_id=$1)", id)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_trials WHERE evaluation_id=$1", id)
		_, _ = pool.Exec(ctx, "DELETE FROM benchmark_evaluations WHERE id=$1", id)
	}
}
