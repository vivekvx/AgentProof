package server

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/santhosh-tekuri/jsonschema/v5"
	"github.com/vivekvx/AgentProof/packages/contracts"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/client"
)

type benchmarkRequest struct {
	DatasetID      string   `json:"dataset_id"`
	DatasetVersion int      `json:"dataset_version"`
	AgentID        string   `json:"agent_id"`
	AgentVersion   int      `json:"agent_version"`
	Split          string   `json:"split"`
	Trials         int      `json:"trials"`
	ScenarioIDs    []string `json:"scenario_ids"`
}
type scenarioAsset struct {
	ID       string `json:"id"`
	Title    string `json:"title"`
	Split    string `json:"split"`
	Category string `json:"category"`
	Fixture  struct {
		Order json.RawMessage `json:"order"`
	} `json:"fixture"`
}
type datasetAsset struct {
	ID        string            `json:"id"`
	Version   int               `json:"version"`
	Title     string            `json:"title"`
	Scenarios []json.RawMessage `json:"scenarios"`
}
type benchmarkManifest struct {
	SchemaVersion     string              `json:"schema_version"`
	ID                string              `json:"id"`
	Dataset           contracts.Reference `json:"dataset"`
	Agent             contracts.Reference `json:"agent"`
	Evaluator         contracts.Reference `json:"evaluator"`
	ToolSchema        contracts.Reference `json:"tool_schema"`
	ToolSchemaVersion string              `json:"tool_schema_version"`
	Split             string              `json:"split"`
	Trials            int                 `json:"trials"`
	ScenarioIDs       []string            `json:"scenario_ids"`
	Traceparent       string              `json:"traceparent"`
}

func validateAsset(schemaName string, content json.RawMessage) error {
	raw, err := contracts.Assets.ReadFile("v2/" + schemaName + ".schema.json")
	if err != nil {
		return err
	}
	schema, err := jsonschema.CompileString("https://agentproof.local/"+schemaName, string(raw))
	if err != nil {
		return err
	}
	var value any
	if err = json.Unmarshal(content, &value); err != nil {
		return err
	}
	return schema.Validate(value)
}
func validateInteractionAsset(schemaName string, content json.RawMessage) error {
	raw, err := contracts.Assets.ReadFile("v3/" + schemaName + ".schema.json")
	if err != nil {
		return err
	}
	schema, err := jsonschema.CompileString("https://agentproof.local/v3/"+schemaName, string(raw))
	if err != nil {
		return err
	}
	var value any
	if err = json.Unmarshal(content, &value); err != nil {
		return err
	}
	return schema.Validate(value)
}
func builtinDataset(versions ...int) (contracts.Reference, datasetAsset, error) {
	version := 5
	if len(versions) > 0 {
		version = versions[0]
	}
	ref := contracts.Load(fmt.Sprintf("benchmarks/support.v%d.json", version))
	var value datasetAsset
	var validation error
	if version == 6 {
		validation = validateInteractionAsset("InteractionDataset", ref.Content)
	} else {
		validation = validateAsset("Dataset", ref.Content)
	}
	if validation != nil {
		return ref, value, validation
	}
	if err := json.Unmarshal(ref.Content, &value); err != nil {
		return ref, value, err
	}
	seen := map[string]bool{}
	for _, raw := range value.Scenarios {
		var sc scenarioAsset
		if err := json.Unmarshal(raw, &sc); err != nil {
			return ref, value, err
		}
		if seen[sc.ID] {
			return ref, value, errors.New("duplicate scenario identity")
		}
		seen[sc.ID] = true
	}
	return ref, value, nil
}

var errDatasetConflict = errors.New("published dataset version has changed; publish a new version")

func (s *Server) publishDataset(ctx context.Context, ref contracts.Reference) error {
	_, err := s.Pool.Exec(ctx, "INSERT INTO benchmark_datasets(id,version,sha256,content) VALUES($1,$2,$3,$4) ON CONFLICT(id,version) DO NOTHING", ref.ID, ref.Version, ref.SHA256, ref.Content)
	if err != nil {
		return err
	}
	var hash string
	if err = s.Pool.QueryRow(ctx, "SELECT sha256 FROM benchmark_datasets WHERE id=$1 AND version=$2", ref.ID, ref.Version).Scan(&hash); err != nil {
		return err
	}
	if hash != ref.SHA256 {
		return errDatasetConflict
	}
	return nil
}
func (s *Server) benchmarkRoutes(r chi.Router) {
	r.Get("/api/v2/datasets", s.datasets)
	r.Get("/api/v2/agents", s.agents)
	r.Post("/api/v2/evaluations", s.createBenchmark)
	r.Get("/api/v2/evaluations", s.listBenchmarks)
	r.Get("/api/v2/evaluations/{id}", s.inspectBenchmark)
	r.Get("/api/v2/trials/{id}", s.inspectTrial)
	r.Post("/api/v2/comparisons", s.compareBenchmarks)
}
func (s *Server) datasets(w http.ResponseWriter, r *http.Request) {
	result := []any{}
	for _, version := range []int{5, 6} {
		ref, value, err := builtinDataset(version)
		if err != nil {
			problem(w, 500, "invalid dataset asset")
			return
		}
		if err = s.publishDataset(r.Context(), ref); err != nil {
			if errors.Is(err, errDatasetConflict) {
				problem(w, 409, "dataset publication conflicts with installed version")
			} else {
				problem(w, 503, "cannot read dataset publication")
			}
			return
		}
		scenarios := []map[string]string{}
		for _, raw := range value.Scenarios {
			var sc scenarioAsset
			_ = json.Unmarshal(raw, &sc)
			scenarios = append(scenarios, map[string]string{"id": sc.ID, "title": sc.Title, "split": sc.Split, "category": sc.Category})
		}
		result = append(result, map[string]any{"schema_version": "2.0", "id": ref.ID, "version": ref.Version, "sha256": ref.SHA256, "title": value.Title, "scenarios": scenarios, "review": "Engineering review and executable reference checks; source-visible development cases are not sealed"})
	}
	respond(w, 200, result)
}
func (s *Server) agents(w http.ResponseWriter, r *http.Request) {
	refs := []contracts.Reference{contracts.Load("agents/support-safe.v1.json"), contracts.Load("agents/support-blind.v1.json"), contracts.Load("agents/support-interaction-safe.v1.json")}
	if directory := os.Getenv("AGENTPROOF_MODEL_CONFIG_DIR"); directory != "" {
		paths, _ := filepath.Glob(filepath.Join(directory, "*.v1.json"))
		for _, path := range paths {
			id := strings.TrimSuffix(filepath.Base(path), ".v1.json")
			if ref, err := loadAgent(id); err == nil {
				refs = append(refs, ref)
			}
		}
	}
	respond(w, 200, refs)
}
func loadAgent(id string) (contracts.Reference, error) {
	if id == "support-safe" || id == "support-blind" || id == "support-interaction-safe" {
		return contracts.Load("agents/" + id + ".v1.json"), nil
	}
	if !regexp.MustCompile(`^[a-z][a-z0-9-]{1,63}$`).MatchString(id) {
		return contracts.Reference{}, errors.New("invalid agent id")
	}
	directory := os.Getenv("AGENTPROOF_MODEL_CONFIG_DIR")
	if directory == "" {
		return contracts.Reference{}, errors.New("model configuration directory is not set")
	}
	raw, err := os.ReadFile(filepath.Join(directory, id+".v1.json"))
	if err != nil {
		return contracts.Reference{}, err
	}
	if err = validateAsset("AgentConfiguration", raw); err != nil {
		return contracts.Reference{}, err
	}
	var value struct {
		ID      string `json:"id"`
		Version int    `json:"version"`
		Policy  string `json:"policy"`
	}
	if err = json.Unmarshal(raw, &value); err != nil {
		return contracts.Reference{}, err
	}
	var model struct {
		Configuration json.RawMessage `json:"model_configuration"`
	}
	_ = json.Unmarshal(raw, &model)
	if err = validateAsset("ModelConfiguration", model.Configuration); err != nil {
		return contracts.Reference{}, err
	}
	if value.ID != id || value.Version != 1 || value.Policy != "llm" {
		return contracts.Reference{}, errors.New("invalid model identity")
	}
	return contracts.Reference{ID: id, Version: 1, SHA256: fmt.Sprintf("%x", sha256.Sum256(raw)), Content: raw}, nil
}
func decodeBenchmark(w http.ResponseWriter, r *http.Request) (benchmarkRequest, error) {
	input := benchmarkRequest{Trials: 1, Split: "development", ScenarioIDs: []string{}}
	r.Body = http.MaxBytesReader(w, r.Body, 8192)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	var raw json.RawMessage
	if err := decoder.Decode(&raw); err != nil {
		return input, err
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return input, errors.New("one JSON object required")
	}
	var identity struct {
		DatasetVersion int `json:"dataset_version"`
	}
	if err := json.Unmarshal(raw, &identity); err != nil {
		return input, err
	}
	var validation error
	if identity.DatasetVersion == 6 {
		validation = validateInteractionAsset("InteractionConfiguration", raw)
	} else {
		validation = validateAsset("EvaluationConfiguration", raw)
	}
	if validation != nil {
		return input, validation
	}
	if err := json.Unmarshal(raw, &input); err != nil {
		return input, err
	}
	if input.DatasetID != "customer-support" || (input.DatasetVersion != 2 && input.DatasetVersion != 3 && input.DatasetVersion != 4 && input.DatasetVersion != 5 && input.DatasetVersion != 6) || input.AgentVersion != 1 || !regexp.MustCompile(`^[a-z][a-z0-9-]{1,63}$`).MatchString(input.AgentID) || input.Trials < 1 || input.Trials > 5 || (input.Split != "development" && input.Split != "held-out" && input.Split != "all") || len(input.ScenarioIDs) > 50 {
		return input, errors.New("invalid benchmark configuration")
	}
	return input, nil
}
func (s *Server) createBenchmark(w http.ResponseWriter, r *http.Request) {
	input, err := decodeBenchmark(w, r)
	if err != nil {
		problem(w, 400, "invalid benchmark configuration")
		return
	}
	dataset, value, err := builtinDataset(input.DatasetVersion)
	if err != nil {
		problem(w, 500, "invalid installed dataset")
		return
	}
	agent, err := loadAgent(input.AgentID)
	if err != nil {
		problem(w, 400, "unknown or invalid model configuration")
		return
	}
	if input.DatasetVersion == 6 && input.AgentID != "support-interaction-safe" || input.DatasetVersion != 6 && input.AgentID == "support-interaction-safe" {
		problem(w, 400, "agent and dataset versions are incompatible")
		return
	}
	if input.DatasetVersion == 6 {
		err = validateInteractionAsset("InteractionAgentConfiguration", agent.Content)
	} else {
		err = validateAsset("AgentConfiguration", agent.Content)
	}
	if err != nil {
		problem(w, 500, "invalid agent asset")
		return
	}
	requested := map[string]bool{}
	for _, id := range input.ScenarioIDs {
		if requested[id] {
			problem(w, 400, "duplicate selected scenario")
			return
		}
		requested[id] = true
	}
	selected := []json.RawMessage{}
	ids := []string{}
	for _, raw := range value.Scenarios {
		var sc scenarioAsset
		_ = json.Unmarshal(raw, &sc)
		if input.Split != "all" && sc.Split != input.Split {
			continue
		}
		if len(requested) > 0 && !requested[sc.ID] {
			continue
		}
		selected = append(selected, raw)
		ids = append(ids, sc.ID)
	}
	if len(selected) == 0 || len(requested) > 0 && len(selected) != len(requested) {
		problem(w, 400, "scenario selection is empty, unknown, or outside selected split")
		return
	}
	input.ScenarioIDs = ids
	if err = s.publishDataset(r.Context(), dataset); err != nil {
		if errors.Is(err, errDatasetConflict) {
			problem(w, 409, "dataset version is immutable")
		} else {
			problem(w, 503, "cannot publish dataset")
		}
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if key != "" && !regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`).MatchString(key) {
		problem(w, 400, "invalid Idempotency-Key")
		return
	}
	tx, err := s.Pool.Begin(r.Context())
	if err != nil {
		problem(w, 503, "database unavailable")
		return
	}
	defer tx.Rollback(r.Context())
	encoded, _ := json.Marshal(input)
	if key != "" {
		if _, err = tx.Exec(r.Context(), "SELECT pg_advisory_xact_lock(hashtextextended($1,3))", key); err != nil {
			problem(w, 503, "cannot lock benchmark request")
			return
		}
		var oldID string
		var same bool
		err = tx.QueryRow(r.Context(), "SELECT evaluation_id,request=$2::jsonb FROM benchmark_requests WHERE key=$1", key, encoded).Scan(&oldID, &same)
		if err == nil {
			if !same {
				problem(w, 409, "Idempotency-Key already used with another configuration")
				return
			}
			respond(w, 202, map[string]string{"id": oldID, "status": "queued"})
			return
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			problem(w, 503, "cannot inspect request key")
			return
		}
	}
	id := uuid.New()
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(r.Context(), carrier)
	evaluator := contracts.Load("evaluators/support.v1.json")
	if input.DatasetVersion == 3 {
		evaluator = contracts.Load("evaluators/support.v2.json")
	} else if input.DatasetVersion == 4 {
		evaluator = contracts.Load("evaluators/support.v3.json")
	} else if input.DatasetVersion == 5 {
		evaluator = contracts.Load("evaluators/support.v4.json")
	} else if input.DatasetVersion == 6 {
		evaluator = contracts.Load("evaluators/support.v5.json")
	}
	toolSchema := contracts.Load("tools/support-tools.v1.json")
	toolVersion := "support-tools-v1"
	manifestVersion := "2.0"
	if input.DatasetVersion == 6 {
		toolSchema = contracts.Load("tools/support-tools.v2.json")
		toolVersion = "support-tools-v2"
		manifestVersion = "3.0"
	}
	manifest := benchmarkManifest{manifestVersion, id.String(), dataset, agent, evaluator, toolSchema, toolVersion, input.Split, input.Trials, ids, carrier.Get("traceparent")}
	body, _ := json.Marshal(manifest)
	if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_evaluations(id,manifest) VALUES($1,$2)", id.String(), body); err != nil {
		problem(w, 503, "cannot persist evaluation")
		return
	}
	for _, raw := range selected {
		var sc scenarioAsset
		_ = json.Unmarshal(raw, &sc)
		for n := 1; n <= input.Trials; n++ {
			trialID := uuid.NewSHA1(id, []byte(fmt.Sprintf("%s:%d", sc.ID, n)))
			if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_trials(id,evaluation_id,scenario_id,trial_index,scenario,state) VALUES($1,$2,$3,$4,$5,$6)", trialID.String(), id.String(), sc.ID, n, raw, sc.Fixture.Order); err != nil {
				problem(w, 503, "cannot persist trial plan")
				return
			}
			if input.DatasetVersion >= 5 {
				if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_evidence_heads(trial_id) VALUES($1)", trialID.String()); err != nil {
					problem(w, 503, "cannot persist evidence witness")
					return
				}
			}
		}
	}
	if key != "" {
		if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_requests VALUES($1,$2,$3)", key, encoded, id.String()); err != nil {
			problem(w, 503, "cannot persist request key")
			return
		}
	}
	if err = tx.Commit(r.Context()); err != nil {
		problem(w, 503, "cannot commit evaluation")
		return
	}
	trace.SpanFromContext(r.Context()).SetAttributes(attribute.String("agentproof.run_id", id.String()), attribute.String("agentproof.dataset.id", dataset.ID), attribute.Int("agentproof.dataset.version", dataset.Version))
	w.Header().Set("Location", "/api/v2/evaluations/"+id.String())
	respond(w, 202, map[string]string{"id": id.String(), "status": "queued"})
}
func validUUID(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := chi.URLParam(r, "id")
	if _, err := uuid.Parse(id); err != nil {
		problem(w, 400, "invalid identifier")
		return "", false
	}
	return id, true
}

type benchmarkView struct {
	ID        string          `json:"id"`
	CreatedAt time.Time       `json:"created_at"`
	Status    string          `json:"status"`
	Manifest  json.RawMessage `json:"manifest"`
	Metrics   json.RawMessage `json:"metrics"`
	Trials    []trialView     `json:"trials"`
}
type trialView struct {
	ID         string          `json:"id"`
	ScenarioID string          `json:"scenario_id"`
	Index      int             `json:"trial_index"`
	Status     string          `json:"status"`
	Result     json.RawMessage `json:"result"`
}

func (s *Server) benchmark(ctx context.Context, id string) (benchmarkView, error) {
	var out benchmarkView
	err := s.Pool.QueryRow(ctx, "SELECT id,created_at,status,manifest,result FROM benchmark_evaluations WHERE id=$1", id).Scan(&out.ID, &out.CreatedAt, &out.Status, &out.Manifest, &out.Metrics)
	if err != nil {
		return out, err
	}
	rows, err := s.Pool.Query(ctx, "SELECT id,scenario_id,trial_index,status,result FROM benchmark_trials WHERE evaluation_id=$1 ORDER BY scenario_id,trial_index", id)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	out.Trials = []trialView{}
	for rows.Next() {
		var t trialView
		if err = rows.Scan(&t.ID, &t.ScenarioID, &t.Index, &t.Status, &t.Result); err != nil {
			return out, err
		}
		out.Trials = append(out.Trials, t)
	}
	return out, rows.Err()
}
func (s *Server) inspectBenchmark(w http.ResponseWriter, r *http.Request) {
	id, ok := validUUID(w, r)
	if !ok {
		return
	}
	out, err := s.benchmark(r.Context(), id)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, 404, "evaluation not found")
		return
	}
	if err != nil {
		problem(w, 503, "cannot read evaluation")
		return
	}
	respond(w, 200, out)
}
func (s *Server) listBenchmarks(w http.ResponseWriter, r *http.Request) {
	rows, err := s.Pool.Query(r.Context(), "SELECT id,created_at,status,manifest #- '{dataset,content}' #- '{agent,content}' #- '{evaluator,content}' #- '{tool_schema,content}',result FROM benchmark_evaluations ORDER BY created_at DESC LIMIT 50")
	if err != nil {
		problem(w, 503, "cannot list evaluations")
		return
	}
	defer rows.Close()
	out := []benchmarkView{}
	for rows.Next() {
		var v benchmarkView
		if err = rows.Scan(&v.ID, &v.CreatedAt, &v.Status, &v.Manifest, &v.Metrics); err != nil {
			problem(w, 503, "cannot read evaluation")
			return
		}
		v.Trials = []trialView{}
		out = append(out, v)
	}
	if rows.Err() != nil {
		problem(w, 503, "cannot list evaluations")
		return
	}
	respond(w, 200, out)
}
func (s *Server) inspectTrial(w http.ResponseWriter, r *http.Request) {
	id, ok := validUUID(w, r)
	if !ok {
		return
	}
	var result, scenario json.RawMessage
	err := s.Pool.QueryRow(r.Context(), "SELECT result,scenario FROM benchmark_trials WHERE id=$1", id).Scan(&result, &scenario)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, 404, "trial not found")
		return
	}
	if err != nil {
		problem(w, 503, "cannot read trial")
		return
	}
	rows, err := s.Pool.Query(r.Context(), "SELECT payload FROM benchmark_events WHERE trial_id=$1 ORDER BY sequence", id)
	if err != nil {
		problem(w, 503, "cannot read events")
		return
	}
	defer rows.Close()
	events := []json.RawMessage{}
	for rows.Next() {
		var payload json.RawMessage
		if err = rows.Scan(&payload); err != nil {
			problem(w, 503, "cannot read event")
			return
		}
		events = append(events, payload)
	}
	if rows.Err() != nil {
		problem(w, 503, "cannot read events")
		return
	}
	respond(w, 200, map[string]any{"schema_version": "2.0", "trial_id": id, "scenario": scenario, "result": result, "events": events})
}
func (t TemporalStarter) StartBenchmark(ctx context.Context, id string) error {
	_, err := t.Client.ExecuteWorkflow(ctx, client.StartWorkflowOptions{ID: "benchmark-" + id, TaskQueue: "agentproof-v1", WorkflowIDReusePolicy: enums.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE}, "BenchmarkWorkflow", id)
	var already *serviceerror.WorkflowExecutionAlreadyStarted
	if errors.As(err, &already) {
		return nil
	}
	return err
}
func (s *Server) DispatchBenchmarks(ctx context.Context) {
	starter, ok := s.Starter.(interface {
		StartBenchmark(context.Context, string) error
	})
	if !ok {
		return
	}
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			rows, err := s.Pool.Query(ctx, "SELECT id,manifest->>'traceparent' FROM benchmark_evaluations WHERE status='queued' ORDER BY created_at LIMIT 100")
			if err != nil {
				continue
			}
			items := []struct{ id, parent string }{}
			for rows.Next() {
				var v struct{ id, parent string }
				if rows.Scan(&v.id, &v.parent) == nil {
					items = append(items, v)
				}
			}
			rows.Close()
			for _, v := range items {
				parent := otel.GetTextMapPropagator().Extract(ctx, propagation.MapCarrier{"traceparent": v.parent})
				call, cancel := context.WithTimeout(parent, 5*time.Second)
				_ = starter.StartBenchmark(call, v.id)
				cancel()
			}
		}
	}
}

type pairedOutcome struct {
	Verdict    string `json:"verdict"`
	Assertions []struct {
		ID      string `json:"id"`
		Verdict string `json:"verdict"`
	} `json:"assertions"`
}

func experimentMismatch(a, b benchmarkManifest) []string {
	reasons := []string{}
	if a.Trials != b.Trials || a.Split != b.Split {
		reasons = append(reasons, "trial count or split differs")
	}
	var left, right map[string]any
	if json.Unmarshal(a.Agent.Content, &left) != nil || json.Unmarshal(b.Agent.Content, &right) != nil {
		return append(reasons, "agent profile unavailable")
	}
	for _, field := range []string{"prompt", "prompt_version", "max_retries"} {
		if left[field] == nil || right[field] == nil || fmt.Sprint(left[field]) != fmt.Sprint(right[field]) {
			reasons = append(reasons, field+" differs or is unavailable")
		}
	}
	lc, lok := left["model_configuration"].(map[string]any)
	rc, rok := right["model_configuration"].(map[string]any)
	if !lok || !rok {
		return append(reasons, "model configuration unavailable")
	}
	for _, field := range []string{"provider", "base_url", "tool_calling", "timeout_seconds", "temperature", "top_p", "max_output_tokens", "max_requests", "max_tool_calls", "max_tokens", "max_estimated_cost_usd"} {
		if lc[field] == nil || rc[field] == nil || fmt.Sprint(lc[field]) != fmt.Sprint(rc[field]) {
			reasons = append(reasons, field+" differs or is unavailable")
		}
	}
	return reasons
}

func compareViews(left, right benchmarkView) (map[string]any, error) {
	var a, b benchmarkManifest
	if json.Unmarshal(left.Manifest, &a) != nil || json.Unmarshal(right.Manifest, &b) != nil {
		return nil, errors.New("invalid manifests")
	}
	if a.SchemaVersion != "2.0" || b.SchemaVersion != "2.0" || a.Dataset.ID != b.Dataset.ID || a.Dataset.Version != b.Dataset.Version || a.Dataset.SHA256 != b.Dataset.SHA256 || a.Evaluator.SHA256 != b.Evaluator.SHA256 || a.ToolSchemaVersion != b.ToolSchemaVersion || a.ToolSchema.SHA256 != b.ToolSchema.SHA256 {
		return nil, errors.New("incompatible dataset or evaluation contract")
	}
	ax := append([]string{}, a.ScenarioIDs...)
	bx := append([]string{}, b.ScenarioIDs...)
	sort.Strings(ax)
	sort.Strings(bx)
	if fmt.Sprint(ax) != fmt.Sprint(bx) {
		return nil, errors.New("incompatible scenario selection")
	}
	l, r := map[string]trialView{}, map[string]trialView{}
	keys := map[string]bool{}
	for _, t := range left.Trials {
		k := fmt.Sprintf("%s:%d", t.ScenarioID, t.Index)
		l[k] = t
		keys[k] = true
	}
	for _, t := range right.Trials {
		k := fmt.Sprintf("%s:%d", t.ScenarioID, t.Index)
		r[k] = t
		keys[k] = true
	}
	ordered := []string{}
	for k := range keys {
		ordered = append(ordered, k)
	}
	sort.Strings(ordered)
	counts := map[string]int{"improvement": 0, "regression": 0, "unchanged": 0, "missing": 0, "indeterminate": 0}
	pairs := []map[string]any{}
	for _, key := range ordered {
		x, lok := l[key]
		y, rok := r[key]
		var xv, yv pairedOutcome
		_ = json.Unmarshal(x.Result, &xv)
		_ = json.Unmarshal(y.Result, &yv)
		kind := "unchanged"
		if !lok || !rok || xv.Verdict == "" || yv.Verdict == "" {
			kind = "missing"
		} else if xv.Verdict == "INDETERMINATE" || yv.Verdict == "INDETERMINATE" {
			kind = "indeterminate"
		} else if xv.Verdict == "FAIL" && yv.Verdict == "PASS" {
			kind = "improvement"
		} else if xv.Verdict == "PASS" && yv.Verdict == "FAIL" {
			kind = "regression"
		}
		counts[kind]++
		sc := x.ScenarioID
		n := x.Index
		if !lok {
			sc = y.ScenarioID
			n = y.Index
		}
		regressions, improvements := []string{}, []string{}
		before := map[string]string{}
		for _, c := range xv.Assertions {
			before[c.ID] = c.Verdict
		}
		for _, c := range yv.Assertions {
			if before[c.ID] == "PASS" && c.Verdict == "FAIL" {
				regressions = append(regressions, c.ID)
			}
			if before[c.ID] == "FAIL" && c.Verdict == "PASS" {
				improvements = append(improvements, c.ID)
			}
		}
		pairs = append(pairs, map[string]any{"scenario_id": sc, "trial_index": n, "classification": kind, "baseline": x, "candidate": y, "assertion_regressions": regressions, "assertion_improvements": improvements})
	}
	comparable := counts["improvement"] + counts["regression"] + counts["unchanged"]
	var delta any = nil
	if comparable > 0 {
		delta = float64(counts["improvement"]-counts["regression"]) / float64(comparable)
	}
	type agentDetails struct {
		Policy             string `json:"policy"`
		ModelConfiguration struct {
			Model string `json:"model"`
		} `json:"model_configuration"`
	}
	var baselineAgent, candidateAgent agentDetails
	_ = json.Unmarshal(a.Agent.Content, &baselineAgent)
	_ = json.Unmarshal(b.Agent.Content, &candidateAgent)
	kind := "unknown_agents"
	label := "Agent type is unavailable; do not infer an offline or real-model result."
	if baselineAgent.Policy == "llm" && candidateAgent.Policy == "llm" {
		kind = "real_model"
		label = "Real-model agent outcomes are descriptive; matching alone does not establish model superiority."
	} else if baselineAgent.Policy != "" && candidateAgent.Policy != "" && (baselineAgent.Policy == "llm" || candidateAgent.Policy == "llm") {
		kind = "mixed_agents"
		label = "This comparison mixes real-model and offline reference-agent outcomes."
	} else if baselineAgent.Policy != "" && candidateAgent.Policy != "" {
		kind = "offline_control"
		label = "Offline reference-agent outcomes are not real-model results."
	}
	mismatches := []string{}
	if kind == "real_model" {
		mismatches = experimentMismatch(a, b)
	}
	notes := []string{"Paired by scenario ID and trial index; different trial counts produce missing pairs.", "INDETERMINATE and missing pairs are excluded from the conditional success delta.", label, "This delta does not establish model superiority or population performance."}
	if len(mismatches) > 0 {
		notes = append(notes, "Real-model profile or budget mismatch; this is not a matched experiment.")
	}
	return map[string]any{"schema_version": "2.0", "baseline_id": left.ID, "candidate_id": right.ID, "dataset_sha256": a.Dataset.SHA256, "counts": counts, "sample_count": len(pairs), "comparable_count": comparable, "paired_success_delta": delta, "pairs": pairs, "provisional": left.Status != "completed" || right.Status != "completed", "comparison_kind": kind, "baseline_model": baselineAgent.ModelConfiguration.Model, "candidate_model": candidateAgent.ModelConfiguration.Model, "experiment_eligible": kind == "real_model" && len(mismatches) == 0 && a.Trials > 0 && len(pairs) == len(a.ScenarioIDs)*a.Trials && left.Status == "completed" && right.Status == "completed" && counts["missing"] == 0, "experiment_mismatch_reasons": mismatches, "notes": notes}, nil
}
func (s *Server) compareBenchmarks(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 1024)
	var input struct {
		Baseline  string `json:"baseline_id"`
		Candidate string `json:"candidate_id"`
	}
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil || decoder.Decode(&struct{}{}) != io.EOF {
		problem(w, 400, "invalid comparison request")
		return
	}
	for _, id := range []string{input.Baseline, input.Candidate} {
		if _, err := uuid.Parse(id); err != nil {
			problem(w, 400, "invalid evaluation identifier")
			return
		}
	}
	a, err := s.benchmark(r.Context(), input.Baseline)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, 404, "evaluation not found")
		return
	}
	if err != nil {
		problem(w, 503, "cannot read baseline")
		return
	}
	b, err := s.benchmark(r.Context(), input.Candidate)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, 404, "evaluation not found")
		return
	}
	if err != nil {
		problem(w, 503, "cannot read candidate")
		return
	}
	out, err := compareViews(a, b)
	if err != nil {
		problem(w, 409, err.Error())
		return
	}
	respond(w, 200, out)
}
