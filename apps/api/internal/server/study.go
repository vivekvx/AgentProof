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
	"regexp"
	"sort"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/vivekvx/AgentProof/packages/contracts"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

type studyProfile struct {
	ID      string `json:"id"`
	Version int    `json:"version"`
	Model   string `json:"model"`
	SHA256  string `json:"sha256"`
}
type studyPlan struct {
	SchemaVersion string `json:"schema_version"`
	ID            string `json:"id"`
	Version       int    `json:"version"`
	Status        string `json:"status"`
	Dataset       struct {
		ID      string `json:"id"`
		Version int    `json:"version"`
		SHA256  string `json:"sha256"`
	} `json:"dataset"`
	Evaluator struct {
		Version string `json:"version"`
		SHA256  string `json:"sha256"`
	} `json:"evaluator"`
	ToolSchema struct {
		Version string `json:"version"`
		SHA256  string `json:"sha256"`
	} `json:"tool_schema"`
	Split             string            `json:"split"`
	ScenarioIDs       []string          `json:"scenario_ids"`
	ScenarioSHA256    map[string]string `json:"scenario_sha256"`
	TrialsPerScenario int               `json:"trials_per_scenario"`
	Profiles          []studyProfile    `json:"profiles"`
	PromptVersion     string            `json:"prompt_version"`
	PromptSHA256      string            `json:"prompt_sha256"`
	PerTrialLimits    struct {
		MaxRequests         int     `json:"max_requests"`
		MaxToolCalls        int     `json:"max_tool_calls"`
		MaxTokens           int     `json:"max_tokens"`
		MaxActiveSeconds    int     `json:"max_active_seconds"`
		MaxEstimatedCostUSD float64 `json:"max_estimated_cost_usd"`
	} `json:"per_trial_limits"`
	StudyCeiling struct {
		PlannedSlots        int     `json:"planned_slots"`
		MaxRequests         int     `json:"max_requests"`
		MaxToolCalls        int     `json:"max_tool_calls"`
		MaxTokens           int     `json:"max_tokens"`
		MaxActiveSeconds    int     `json:"max_active_seconds"`
		MaxWallSeconds      int     `json:"max_wall_seconds"`
		MaxEstimatedCostUSD float64 `json:"max_estimated_cost_usd"`
	} `json:"study_ceiling"`
}

func studyPlanReference() (contracts.Reference, studyPlan, error) {
	var raw []byte
	var err error
	if path := os.Getenv("AGENTPROOF_STUDY_PLAN_PATH"); path != "" {
		raw, err = os.ReadFile(path)
	} else {
		raw, err = contracts.Assets.ReadFile("experiments/groq-gpt-oss-matched.v1.json")
	}
	if err != nil {
		return contracts.Reference{}, studyPlan{}, err
	}
	var plan studyPlan
	if err = json.Unmarshal(raw, &plan); err != nil {
		return contracts.Reference{}, plan, err
	}
	ref := contracts.Reference{ID: plan.ID, Version: plan.Version, SHA256: fmt.Sprintf("%x", sha256.Sum256(raw)), Content: raw}
	if plan.SchemaVersion != "experiment-plan-v1" || plan.Version != 1 || plan.Status != "predeclared-not-authorized-to-run" || plan.Dataset.ID != "customer-support" || plan.Dataset.Version != 5 || plan.Split != "development" || plan.TrialsPerScenario != 2 || len(plan.ScenarioIDs) != 2 || len(plan.Profiles) != 2 || plan.StudyCeiling.PlannedSlots != 8 || plan.StudyCeiling.MaxRequests != 48 || plan.StudyCeiling.MaxToolCalls != 80 || plan.StudyCeiling.MaxTokens != 512000 || plan.StudyCeiling.MaxActiveSeconds != 40 || plan.StudyCeiling.MaxWallSeconds != 180 || plan.StudyCeiling.MaxEstimatedCostUSD != 0.8 || plan.PerTrialLimits.MaxRequests != 6 || plan.PerTrialLimits.MaxToolCalls != 10 || plan.PerTrialLimits.MaxTokens != 64000 || plan.PerTrialLimits.MaxActiveSeconds != 5 || plan.PerTrialLimits.MaxEstimatedCostUSD != 0.1 {
		return ref, plan, errors.New("study differs from bounded eight-slot plan")
	}
	if os.Getenv("AGENTPROOF_STUDY_PLAN_PATH") != "" && plan.ID != "agentproof-mocked-t16" {
		return ref, plan, errors.New("only the isolated mock study may override the pinned plan")
	}
	if os.Getenv("AGENTPROOF_STUDY_PLAN_PATH") == "" && plan.ID != "groq-gpt-oss-matched-development" {
		return ref, plan, errors.New("pinned study identity changed")
	}
	return ref, plan, nil
}

func canonicalDigest(raw json.RawMessage) (string, error) {
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", err
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", sha256.Sum256(canonical)), nil
}

func studyAssets(plan studyPlan) (contracts.Reference, contracts.Reference, contracts.Reference, []json.RawMessage, []contracts.Reference, error) {
	dataset, installed, err := builtinDataset(5)
	if err != nil {
		return contracts.Reference{}, contracts.Reference{}, contracts.Reference{}, nil, nil, err
	}
	evaluator := contracts.Load("evaluators/support.v4.json")
	tools := contracts.Load("tools/support-tools.v1.json")
	if dataset.SHA256 != plan.Dataset.SHA256 || evaluator.SHA256 != plan.Evaluator.SHA256 || tools.SHA256 != plan.ToolSchema.SHA256 || plan.Evaluator.Version != "support-evaluator-v4" || plan.ToolSchema.Version != "support-tools-v1" {
		return dataset, evaluator, tools, nil, nil, errors.New("study artifact reference changed")
	}
	scenarios := map[string]json.RawMessage{}
	for _, raw := range installed.Scenarios {
		var sc scenarioAsset
		if err = json.Unmarshal(raw, &sc); err != nil {
			return dataset, evaluator, tools, nil, nil, err
		}
		scenarios[sc.ID] = raw
	}
	selected := make([]json.RawMessage, 0, 2)
	for _, id := range plan.ScenarioIDs {
		raw, ok := scenarios[id]
		if !ok {
			return dataset, evaluator, tools, nil, nil, errors.New("unknown study scenario")
		}
		var sc scenarioAsset
		_ = json.Unmarshal(raw, &sc)
		if sc.Split != plan.Split {
			return dataset, evaluator, tools, nil, nil, errors.New("study split differs")
		}
		hash, hashErr := canonicalDigest(raw)
		if hashErr != nil || hash != plan.ScenarioSHA256[id] {
			return dataset, evaluator, tools, nil, nil, errors.New("study scenario changed")
		}
		selected = append(selected, raw)
	}
	profiles := make([]contracts.Reference, 0, 2)
	for _, p := range plan.Profiles {
		ref, loadErr := loadAgent(p.ID)
		if loadErr != nil || ref.Version != p.Version || ref.SHA256 != p.SHA256 {
			return dataset, evaluator, tools, nil, nil, errors.New("study profile changed or unavailable")
		}
		var config struct {
			Policy             string `json:"policy"`
			Prompt             string `json:"prompt"`
			PromptVersion      string `json:"prompt_version"`
			MaxRetries         int    `json:"max_retries"`
			ModelConfiguration struct {
				Model               string  `json:"model"`
				BaseURL             string  `json:"base_url"`
				MaxRequests         int     `json:"max_requests"`
				MaxToolCalls        int     `json:"max_tool_calls"`
				MaxTokens           int     `json:"max_tokens"`
				MaxEstimatedCostUSD float64 `json:"max_estimated_cost_usd"`
			} `json:"model_configuration"`
		}
		if err = json.Unmarshal(ref.Content, &config); err != nil {
			return dataset, evaluator, tools, nil, nil, err
		}
		promptHash := fmt.Sprintf("%x", sha256.Sum256([]byte(config.Prompt)))
		if config.Policy != "llm" || config.PromptVersion != plan.PromptVersion || promptHash != plan.PromptSHA256 || config.MaxRetries != 0 || config.ModelConfiguration.Model != p.Model || config.ModelConfiguration.MaxRequests != 6 || config.ModelConfiguration.MaxToolCalls != 10 || config.ModelConfiguration.MaxTokens != 64000 || config.ModelConfiguration.MaxEstimatedCostUSD != 0.1 {
			return dataset, evaluator, tools, nil, nil, errors.New("study profile settings differ")
		}
		if os.Getenv("AGENTPROOF_STUDY_PLAN_PATH") != "" && config.ModelConfiguration.BaseURL != "http://127.0.0.1:18080/v1" {
			return dataset, evaluator, tools, nil, nil, errors.New("mock study endpoint is not local")
		}
		profiles = append(profiles, ref)
	}
	return dataset, evaluator, tools, selected, profiles, nil
}

type studySnapshot struct {
	planID, status                                                       string
	evaluationIDs                                                        []string
	total, completed, passed, failed, indeterminate, terminalEvaluations int
}

type studyQuery interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}

// readStudySnapshot derives status and counts from one PostgreSQL statement snapshot.
func readStudySnapshot(ctx context.Context, q studyQuery, id string) (studySnapshot, error) {
	var v studySnapshot
	var stored string
	var evaluations int
	err := q.QueryRow(ctx, `SELECT s.plan_id,s.status,
   COALESCE(array_agg(e.evaluation_id::text ORDER BY e.profile_id) FILTER (WHERE e.evaluation_id IS NOT NULL), ARRAY[]::text[]),
   count(e.evaluation_id), count(e.evaluation_id) FILTER (WHERE b.status IN ('completed','cancelled') AND b.result IS NOT NULL),
   COALESCE(sum(t.total),0), COALESCE(sum(t.completed),0), COALESCE(sum(t.passed),0), COALESCE(sum(t.failed),0), COALESCE(sum(t.indeterminate),0)
 FROM benchmark_studies s
 LEFT JOIN benchmark_study_evaluations e ON e.study_id=s.id
 LEFT JOIN benchmark_evaluations b ON b.id=e.evaluation_id
 LEFT JOIN LATERAL (SELECT count(*) AS total, count(*) FILTER (WHERE result IS NOT NULL) AS completed,
   count(*) FILTER (WHERE result->>'verdict'='PASS') AS passed,
   count(*) FILTER (WHERE result->>'verdict'='FAIL') AS failed,
   count(*) FILTER (WHERE result->>'verdict'='INDETERMINATE') AS indeterminate
   FROM benchmark_trials WHERE evaluation_id=e.evaluation_id) t ON true
 WHERE s.id=$1 GROUP BY s.id`, id).Scan(&v.planID, &stored, &v.evaluationIDs, &evaluations, &v.terminalEvaluations, &v.total, &v.completed, &v.passed, &v.failed, &v.indeterminate)
	v.status = stored
	sort.Strings(v.evaluationIDs)
	if stored == "running" && evaluations == 2 && v.total == 8 && v.completed == 8 && v.terminalEvaluations == 2 {
		v.status = "completed"
	}
	return v, err
}

func (s *Server) createStudy(w http.ResponseWriter, r *http.Request) {
	if os.Getenv("AGENTPROOF_ENABLE_STUDY_DISPATCH") != "1" {
		problem(w, 403, "study dispatch is disabled")
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if !regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`).MatchString(key) {
		problem(w, 400, "study idempotency key is required")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	var input struct {
		SchemaVersion string `json:"schema_version"`
		PlanID        string `json:"plan_id"`
	}
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil || decoder.Decode(&struct{}{}) != io.EOF || input.SchemaVersion != "study-create-v1" {
		problem(w, 400, "invalid study request")
		return
	}
	planRef, plan, err := studyPlanReference()
	if err != nil {
		problem(w, 503, "study plan unavailable")
		return
	}
	dataset, evaluator, toolSchema, scenarios, profiles, err := studyAssets(plan)
	if err != nil {
		problem(w, 503, "study assets unavailable or changed")
		return
	}
	requestJSON, _ := json.Marshal(input)
	tx, err := s.Pool.Begin(r.Context())
	if err != nil {
		problem(w, 503, "database unavailable")
		return
	}
	defer tx.Rollback(r.Context())
	// Serialize admissions across API instances. The lock is held through commit,
	// so a simultaneous request sees the committed study before admitting one.
	if _, err = tx.Exec(r.Context(), "SELECT pg_advisory_xact_lock(hashtextextended('agentproof:active-study',11))"); err != nil {
		problem(w, 503, "study admission unavailable")
		return
	}
	if _, err = tx.Exec(r.Context(), "SELECT pg_advisory_xact_lock(hashtextextended($1,11))", key); err != nil {
		problem(w, 503, "study admission unavailable")
		return
	}
	var previousID string
	var same bool
	err = tx.QueryRow(r.Context(), "SELECT study_id::text,request=$2::jsonb FROM benchmark_study_create_keys WHERE key=$1", key, requestJSON).Scan(&previousID, &same)
	if err == nil {
		if !same {
			problem(w, 409, "study key was used with another request")
			return
		}
		snapshot, queryErr := readStudySnapshot(r.Context(), tx, previousID)
		if queryErr != nil {
			problem(w, 503, "study lookup failed")
			return
		}
		respond(w, 202, map[string]any{"id": previousID, "status": snapshot.status, "evaluation_ids": snapshot.evaluationIDs})
		return
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		problem(w, 503, "study key lookup failed")
		return
	}
	if input.PlanID != plan.ID {
		problem(w, 400, "study plan identity differs")
		return
	}
	var active bool
	err = tx.QueryRow(r.Context(), `SELECT EXISTS (
		SELECT 1 FROM benchmark_studies s
		WHERE (
			(SELECT count(*) FROM benchmark_study_evaluations e WHERE e.study_id=s.id) <> 2
			OR EXISTS (SELECT 1 FROM benchmark_study_evaluations e
				JOIN benchmark_evaluations b ON b.id=e.evaluation_id
				WHERE e.study_id=s.id AND b.result IS NULL)
			OR (SELECT count(*) FROM benchmark_study_trials t WHERE t.study_id=s.id::text) <> 8
			OR EXISTS (SELECT 1 FROM benchmark_study_trials st
				JOIN benchmark_trials t ON t.id=st.trial_id
				WHERE st.study_id=s.id::text AND t.result IS NULL)
		)
	)`).Scan(&active)
	if err != nil {
		problem(w, 503, "study lookup failed")
		return
	}
	if active {
		problem(w, 409, "another study is active")
		return
	}
	studyID := uuid.New()
	limits := json.RawMessage(`{"slots":8,"requests":48,"tool_calls":80,"tokens":512000,"active_ms":40000,"cost_usd":"0.8"}`)
	reserve := json.RawMessage(`{"slots":1,"requests":6,"tool_calls":10,"tokens":64000,"active_ms":5000,"cost_usd":"0.1"}`)
	if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_study_budgets(study_id,limits,reserved_slots,reserved_requests,reserved_tool_calls,reserved_tokens,reserved_active_ms,reserved_cost_usd) VALUES($1,$2,8,48,80,512000,40000,0.8)", studyID.String(), limits); err != nil {
		problem(w, 503, "budget admission failed")
		return
	}
	if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_studies(id,plan_id,plan_sha256,plan) VALUES($1,$2,$3,$4)", studyID.String(), plan.ID, planRef.SHA256, planRef.Content); err != nil {
		problem(w, 503, "study admission failed")
		return
	}
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(r.Context(), carrier)
	evaluationIDs := []string{}
	for _, profile := range profiles {
		evalID := uuid.New()
		manifest := benchmarkManifest{SchemaVersion: "5.0", ID: evalID.String(), Dataset: dataset, Agent: profile, Evaluator: evaluator, ToolSchema: toolSchema, ToolSchemaVersion: "support-tools-v1", Split: plan.Split, Trials: plan.TrialsPerScenario, ScenarioIDs: plan.ScenarioIDs, Traceparent: carrier.Get("traceparent"), StudyID: studyID.String(), StudyPlan: &planRef}
		body, _ := json.Marshal(manifest)
		if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_evaluations(id,manifest) VALUES($1,$2)", evalID.String(), body); err != nil {
			problem(w, 503, "evaluation admission failed")
			return
		}
		if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_study_evaluations(study_id,evaluation_id,profile_id) VALUES($1,$2,$3)", studyID.String(), evalID.String(), profile.ID); err != nil {
			problem(w, 503, "study evaluation binding failed")
			return
		}
		for _, raw := range scenarios {
			var scenario scenarioAsset
			_ = json.Unmarshal(raw, &scenario)
			for n := 1; n <= plan.TrialsPerScenario; n++ {
				trialID := uuid.NewSHA1(evalID, []byte(fmt.Sprintf("%s:%d", scenario.ID, n)))
				if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_trials(id,evaluation_id,scenario_id,trial_index,scenario,state) VALUES($1,$2,$3,$4,$5,$6)", trialID.String(), evalID.String(), scenario.ID, n, raw, scenario.Fixture.Order); err != nil {
					problem(w, 503, "study trial admission failed")
					return
				}
				if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_evidence_heads(trial_id) VALUES($1)", trialID.String()); err != nil {
					problem(w, 503, "study evidence admission failed")
					return
				}
				slot := fmt.Sprintf("%s:%s:%d", profile.ID, scenario.ID, n)
				if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_study_reservations(study_id,slot_id,state,reserved) VALUES($1,$2,'reserved',$3)", studyID.String(), slot, reserve); err != nil {
					problem(w, 503, "study slot reservation failed")
					return
				}
				if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_study_trials(trial_id,study_id,slot_id) VALUES($1,$2,$3)", trialID.String(), studyID.String(), slot); err != nil {
					problem(w, 503, "study trial binding failed")
					return
				}
			}
		}
		evaluationIDs = append(evaluationIDs, evalID.String())
	}
	if len(evaluationIDs) != 2 {
		problem(w, 503, "study slot count differs")
		return
	}
	if _, err = tx.Exec(r.Context(), "INSERT INTO benchmark_study_create_keys(key,request,study_id) VALUES($1,$2,$3)", key, requestJSON, studyID.String()); err != nil {
		problem(w, 503, "study key admission failed")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		problem(w, 503, "study commit failed")
		return
	}
	for _, id := range evaluationIDs {
		if starter, ok := s.Starter.(interface {
			StartBenchmark(context.Context, string) error
		}); ok {
			ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
			_ = starter.StartBenchmark(ctx, id) // The queued-row dispatcher retries a failed start.
			cancel()
		}
	}
	sort.Strings(evaluationIDs)
	respond(w, 202, map[string]any{"id": studyID.String(), "status": "running", "evaluation_ids": evaluationIDs})
}

func (s *Server) inspectStudy(w http.ResponseWriter, r *http.Request) {
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		problem(w, 400, "invalid study id")
		return
	}
	snapshot, err := readStudySnapshot(r.Context(), s.Pool, id.String())
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, 404, "study not found")
		return
	}
	if err != nil {
		problem(w, 503, "study unavailable")
		return
	}
	respond(w, 200, map[string]any{"id": id.String(), "plan_id": snapshot.planID, "status": snapshot.status, "planned_slots": 8, "completed_trials": snapshot.completed, "pass": snapshot.passed, "fail": snapshot.failed, "indeterminate": snapshot.indeterminate, "pending": 8 - snapshot.completed, "pending_evaluations": 2 - snapshot.terminalEvaluations, "evaluation_ids": snapshot.evaluationIDs})
}

func (s *Server) cancelStudy(w http.ResponseWriter, r *http.Request) {
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		problem(w, 400, "invalid study id")
		return
	}
	tx, err := s.Pool.Begin(r.Context())
	if err != nil {
		problem(w, 503, "study cancellation failed")
		return
	}
	defer tx.Rollback(r.Context())
	var status string
	err = tx.QueryRow(r.Context(), "SELECT status FROM benchmark_studies WHERE id=$1 FOR UPDATE", id.String()).Scan(&status)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, 409, "study is not running")
		return
	}
	if err != nil {
		problem(w, 503, "study cancellation failed")
		return
	}
	if status != "running" {
		problem(w, 409, "study is not running")
		return
	}
	var pending bool
	err = tx.QueryRow(r.Context(), `SELECT EXISTS (
		SELECT 1 FROM benchmark_study_evaluations e
		JOIN benchmark_trials t ON t.evaluation_id=e.evaluation_id
		WHERE e.study_id=$1 AND t.result IS NULL
	)`, id.String()).Scan(&pending)
	if err != nil {
		problem(w, 503, "study cancellation failed")
		return
	}
	if !pending {
		problem(w, 409, "study is not running")
		return
	}
	if _, err = tx.Exec(r.Context(), "UPDATE benchmark_studies SET status='cancelled' WHERE id=$1", id.String()); err != nil {
		problem(w, 503, "study cancellation failed")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		problem(w, 503, "study cancellation failed")
		return
	}
	s.signalStudyCancellation(r.Context(), id.String())
	respond(w, 202, map[string]string{"id": id.String(), "status": "cancelled"})
}

func (s *Server) signalStudyCancellation(ctx context.Context, id string) {
	rows, err := s.Pool.Query(ctx, "SELECT evaluation_id::text FROM benchmark_study_evaluations WHERE study_id=$1", id)
	if err == nil {
		for rows.Next() {
			var evaluationID string
			if rows.Scan(&evaluationID) == nil {
				if canceler, ok := s.Starter.(interface {
					CancelBenchmark(context.Context, string) error
				}); ok {
					ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
					_ = canceler.CancelBenchmark(ctx, evaluationID)
					cancel()
				}
			}
		}
		rows.Close()
	}
}

// expireStudiesOnce initiates cancellation at the declared wall deadline.
// Provider admission also checks this deadline in the shared budget row.
// Finalization may finish later and remains the worker's responsibility.
func (s *Server) expireStudiesOnce(ctx context.Context) error {
	_, err := s.Pool.Exec(ctx, `UPDATE benchmark_studies s SET status='cancelled'
		WHERE s.status='running' AND s.created_at <= clock_timestamp()-interval '180 seconds'
		AND EXISTS (SELECT 1 FROM benchmark_study_evaluations e
			JOIN benchmark_evaluations b ON b.id=e.evaluation_id
			WHERE e.study_id=s.id AND b.result IS NULL)`)
	if err != nil {
		return err
	}
	// Retry signaling after an API restart until the worker has committed
	// terminal evaluations. Repeated Temporal cancellation is idempotent.
	rows, err := s.Pool.Query(ctx, `SELECT s.id::text FROM benchmark_studies s
		WHERE s.status='cancelled' AND s.created_at <= clock_timestamp()-interval '180 seconds'
		AND EXISTS (SELECT 1 FROM benchmark_study_evaluations e
			JOIN benchmark_evaluations b ON b.id=e.evaluation_id
			WHERE e.study_id=s.id AND b.result IS NULL)`)
	if err != nil {
		return err
	}
	ids := []string{}
	for rows.Next() {
		var id string
		if rows.Scan(&id) == nil {
			ids = append(ids, id)
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, id := range ids {
		s.signalStudyCancellation(ctx, id)
	}
	return nil
}

func (s *Server) ExpireStudies(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = s.expireStudiesOnce(ctx)
		}
	}
}
