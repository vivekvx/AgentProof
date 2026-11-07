package server

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/vivekvx/AgentProof/packages/contracts"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/contrib/opentelemetry"
	"go.temporal.io/sdk/interceptor"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"time"
)

type Starter interface {
	Start(context.Context, string) error
}
type TemporalStarter struct{ Client client.Client }

func (t TemporalStarter) Start(ctx context.Context, id string) error {
	_, err := t.Client.ExecuteWorkflow(ctx, client.StartWorkflowOptions{ID: "evaluation-" + id, TaskQueue: "agentproof-v1", WorkflowIDReusePolicy: enums.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE}, "EvaluationWorkflow", id)
	var already *serviceerror.WorkflowExecutionAlreadyStarted
	if errors.As(err, &already) {
		return nil
	}
	return err
}
func TemporalOptions(address string) (client.Options, error) {
	tracing, err := opentelemetry.NewTracingInterceptor(opentelemetry.TracerOptions{})
	if err != nil {
		return client.Options{}, err
	}
	return client.Options{HostPort: address, Interceptors: []interceptor.ClientInterceptor{tracing}}, nil
}

type Server struct {
	Pool    *pgxpool.Pool
	Starter Starter
}
type Run struct {
	ID        string            `json:"id"`
	CreatedAt time.Time         `json:"created_at"`
	Status    string            `json:"status"`
	Manifest  json.RawMessage   `json:"manifest"`
	Result    json.RawMessage   `json:"result"`
	Events    []json.RawMessage `json:"events"`
}

func respond(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func problem(w http.ResponseWriter, status int, message string) {
	respond(w, status, map[string]string{"error": message})
}
func ParseAgent(w http.ResponseWriter, r *http.Request) (string, error) {
	r.Body = http.MaxBytesReader(w, r.Body, 1024)
	var input struct {
		Agent string `json:"agent"`
	}
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(&input); err != nil {
		return "", err
	}
	if err := d.Decode(&struct{}{}); err != io.EOF {
		return "", errors.New("one JSON object required")
	}
	if input.Agent != "safe" && input.Agent != "duplicate" {
		return "", errors.New("invalid agent")
	}
	return input.Agent, nil
}
func (s *Server) Routes() http.Handler {
	r := chi.NewRouter()
	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if err := s.Pool.Ping(ctx); err != nil {
			problem(w, 503, "database unavailable")
			return
		}
		respond(w, 200, map[string]string{"status": "ok"})
	})
	s.benchmarkRoutes(r)
	r.Post("/api/v1/runs", s.create)
	r.Get("/api/v1/runs", s.list)
	r.Get("/api/v1/runs/{id}", s.inspect)
	return otelhttp.NewHandler(r, "agentproof.api")
}
func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	agent, err := ParseAgent(w, r)
	if err != nil {
		problem(w, 400, "agent must be safe or duplicate; send one JSON object")
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if key != "" && !regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`).MatchString(key) {
		problem(w, 400, "invalid Idempotency-Key")
		return
	}
	tx, err := s.Pool.Begin(r.Context())
	if err != nil {
		problem(w, 503, "cannot persist run")
		return
	}
	defer tx.Rollback(r.Context())
	id := uuid.NewString()
	if key != "" {
		if _, err = tx.Exec(r.Context(), "SELECT pg_advisory_xact_lock(hashtextextended($1, 1))", key); err != nil {
			problem(w, 503, "cannot lock request")
			return
		}
		var oldAgent, oldID string
		err = tx.QueryRow(r.Context(), "SELECT agent,run_id FROM run_requests WHERE idempotency_key=$1", key).Scan(&oldAgent, &oldID)
		if err == nil {
			if oldAgent != agent {
				problem(w, 409, "Idempotency-Key already used with another agent")
				return
			}
			w.Header().Set("Location", "/api/v1/runs/"+oldID)
			respond(w, 202, map[string]string{"id": oldID, "status": "queued"})
			return
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			problem(w, 503, "cannot read request")
			return
		}
	}
	scenario := contracts.Load("scenarios/cancel-order.v1.json")
	trace.SpanFromContext(r.Context()).SetAttributes(attribute.String("agentproof.run_id", id), attribute.String("agentproof.scenario.id", scenario.ID), attribute.String("agentproof.scenario.sha256", scenario.SHA256))
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(r.Context(), carrier)
	manifest := map[string]any{"schema_version": "1.0", "run_id": id, "agent": agent, "agent_version": "reference-v1",
		"scenario": scenario, "evaluator": contracts.Load("evaluators/cancellation.v1.json"), "traceparent": carrier.Get("traceparent")}
	data, err := json.Marshal(manifest)
	if err != nil {
		problem(w, 500, "manifest failed")
		return
	}
	if _, err = tx.Exec(r.Context(), "INSERT INTO runs(id,manifest) VALUES($1,$2)", id, data); err != nil {
		problem(w, 503, "cannot persist run")
		return
	}
	if key != "" {
		if _, err = tx.Exec(r.Context(), "INSERT INTO run_requests VALUES($1,$2,$3)", key, agent, id); err != nil {
			problem(w, 503, "cannot persist request identity")
			return
		}
	}
	if err = tx.Commit(r.Context()); err != nil {
		problem(w, 503, "cannot commit run")
		return
	}
	// The row is a durable dispatch record. The dispatcher retries start after crashes.
	w.Header().Set("Location", "/api/v1/runs/"+id)
	respond(w, 202, map[string]string{"id": id, "status": "queued"})
}
func (s *Server) inspect(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	if _, err := uuid.Parse(id); err != nil {
		problem(w, 400, "invalid run ID")
		return
	}
	var run Run
	err := s.Pool.QueryRow(r.Context(), "SELECT id,created_at,status,manifest,result FROM runs WHERE id=$1", id).Scan(&run.ID, &run.CreatedAt, &run.Status, &run.Manifest, &run.Result)
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, 404, "run not found")
		return
	}
	if err != nil {
		problem(w, 503, "cannot read run")
		return
	}
	rows, err := s.Pool.Query(r.Context(), "SELECT payload FROM tool_events WHERE run_id=$1 ORDER BY sequence", id)
	if err != nil {
		problem(w, 503, "cannot read events")
		return
	}
	defer rows.Close()
	run.Events = []json.RawMessage{}
	for rows.Next() {
		var event json.RawMessage
		if err := rows.Scan(&event); err != nil {
			problem(w, 503, "cannot read event")
			return
		}
		run.Events = append(run.Events, event)
	}
	if rows.Err() != nil {
		problem(w, 503, "cannot read events")
		return
	}
	respond(w, 200, run)
}
func (s *Server) list(w http.ResponseWriter, r *http.Request) {
	rows, err := s.Pool.Query(r.Context(), "SELECT id,created_at,status,manifest,result FROM runs ORDER BY created_at DESC LIMIT 50")
	if err != nil {
		problem(w, 503, "cannot list runs")
		return
	}
	defer rows.Close()
	runs := []Run{}
	for rows.Next() {
		var run Run
		if err := rows.Scan(&run.ID, &run.CreatedAt, &run.Status, &run.Manifest, &run.Result); err != nil {
			problem(w, 503, "cannot read run")
			return
		}
		run.Events = []json.RawMessage{}
		runs = append(runs, run)
	}
	if rows.Err() != nil {
		problem(w, 503, "cannot list runs")
		return
	}
	respond(w, 200, runs)
}
func (s *Server) Dispatch(ctx context.Context) {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			rows, err := s.Pool.Query(ctx, "SELECT id,manifest->>'traceparent' FROM runs WHERE status='queued' ORDER BY created_at LIMIT 100")
			if err != nil {
				slog.Error("dispatch query failed", "error", err)
				continue
			}
			type dispatch struct {
				id     string
				parent string
			}
			ids := []dispatch{}
			for rows.Next() {
				var item dispatch
				if rows.Scan(&item.id, &item.parent) == nil {
					ids = append(ids, item)
				}
			}
			rows.Close()
			for _, item := range ids {
				id := item.id
				parentCtx := otel.GetTextMapPropagator().Extract(ctx, propagation.MapCarrier{"traceparent": item.parent})
				startCtx, cancel := context.WithTimeout(parentCtx, 5*time.Second)
				err := s.Starter.Start(startCtx, id)
				cancel()
				if err != nil {
					slog.Warn("workflow dispatch will retry", "run_id", id, "error", err)
				}
			}
		}
	}
}
