import { useEffect, useState } from "react";
import { Play, GitCompareArrows, ChevronRight } from "lucide-react";
import { Button } from "./components/ui/button";
import "./benchmark.css";

type Verdict = "PASS" | "FAIL" | "INDETERMINATE";
type Scenario = { id: string; title: string; split: string; category: string };
type Dataset = {
  id: string;
  version: number;
  title: string;
  sha256: string;
  scenarios: Scenario[];
  review: string;
};
type Agent = {
  id: string;
  version: number;
  sha256: string;
  content: {
    title: string;
    model_configuration?: Record<string, unknown>;
    prompt_version: string;
    tool_schema_version: string;
  };
};
type Assertion = {
  id: string;
  verdict: Verdict;
  expected: unknown;
  actual: unknown;
  event_sequences: number[];
};
type Result = {
  verdict: Verdict;
  failure_category: string | null;
  assertions: Assertion[];
  effect_counts: Record<string, number>;
  trace_id: string;
  error: string | null;
  model_requests?: Record<string, unknown>[];
  provider_error?: string | null;
  evaluator_version: string;
};
type Trial = {
  id: string;
  scenario_id: string;
  trial_index: number;
  status: string;
  result: Result | null;
};
type Rate = {
  value: number | null;
  numerator: number;
  sample_count: number;
  scenario_count?: number;
  unknown_count?: number;
  scope?: string;
  interval?: { low: number; high: number } | null;
};
type Subtotal = {
  value: number | null;
  sample_count: number;
  request_count?: number;
  unknown_count?: number;
  complete?: boolean;
  success_count?: number;
};
type Duration = {
  p50: number | null;
  p95: number | null;
  sample_count: number;
  population_count?: number;
  missing_count?: number;
};
type Metrics = {
  cohort?: {
    planned_slots: number;
    started_trials: number;
    terminal_trials: number;
    pass_trials: number;
    fail_trials: number;
    indeterminate_trials: number;
    missing_or_pending_slots: number;
    failure_classes: Record<string, number>;
  };
  end_to_end_valid_completion?: Rate;
  started_valid_completion?: Rate;
  business_fulfillment?: Rate;
  unsafe_evidence?: {proven_unsafe_trials: number; unknown_safety_slots: number; unauthorized_attempts: number; invalid_argument_attempts: number; forbidden_effects: number};
  repeated_run_reliability?: {value: number | null; sample_count: number; planned_scenarios?: number; excluded_scenarios?: number; planned_repeats?: number; determinate_repeats?: number; indeterminate_repeats?: number; missing_repeats?: number};
  model_latency_ms?: Duration;
  active_task_latency_ms?: Duration;
  prestart_wait_ms?: Duration;
  trial_elapsed_ms?: Duration;
  evaluation_elapsed_ms?: {value: number | null; sample_count: number};
  estimated_cost_usd?: Subtotal;
  reported_cost_usd?: Subtotal;
  estimated_cost_per_success_usd?: Subtotal;
  reported_cost_per_success_usd?: Subtotal;
  pricing?: {source: string | null; checked_at: string | null; status: string} | null;
  total_trials: number;
  determinate_trials: number;
  indeterminate_trials: number;
  task_success_rate: Rate;
  unsafe_action_rate: Rate;
  recovery_success_rate: Rate;
  duplicate_side_effect_rate: Rate;
  tool_call_validity: Rate;
  execution_latency_ms: Duration;
  token_usage: {
    input: number | null;
    output: number | null;
    sample_count: number;
    request_count?: number;
    input_known_requests?: number;
    output_known_requests?: number;
  };
  inference_cost_usd: { value: number | null; sample_count: number };
  cost_per_success_usd: {
    value: number | null;
    sample_count: number;
    success_count: number;
  };
  notes: string[];
};
type Evaluation = {
  id: string;
  status: string;
  created_at: string;
  manifest: {
    agent: Agent;
    dataset: { id: string; version: number; sha256: string };
    split: string;
    trials: number;
    scenario_ids: string[];
  };
  metrics: Metrics | null;
  trials: Trial[];
};
type Detail = {
  trial_id: string;
  result: Result | null;
  events: {
    schema_version?: string;
    sequence: number;
    kind?: string;
    source?: string;
    metadata?: { arguments_valid?: boolean; authorized?: boolean; target_operation_key?: string };
    tool: string;
    response: unknown;
    fault?: string;
    authorized?: boolean;
    arguments_valid?: boolean;
    operation_key: string;
    transport_requests?: number;
  }[];
};
type Comparison = {
  comparison_kind?: "offline_control" | "real_model" | "mixed_agents";
  baseline_model?: string;
  candidate_model?: string;
  counts: Record<string, number>;
  sample_count: number;
  comparable_count: number;
  paired_success_delta: number | null;
  provisional: boolean;
  notes: string[];
  pairs: {
    scenario_id: string;
    trial_index: number;
    classification: string;
    baseline: Trial;
    candidate: Trial;
    assertion_regressions: string[];
    assertion_improvements: string[];
  }[];
};
async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch("/api/v2" + path, init);
  const value = await response.json();
  if (!response.ok)
    throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}
const percent = (v: number | null) =>
  v === null ? "Unavailable" : `${(v * 100).toFixed(1)}%`;
const ms = (v: number | null) =>
  v === null ? "Unavailable" : `${v.toFixed(1)} ms`;
const money = (v: number | null) =>
  v === null ? "Unavailable" : `$${v.toFixed(6)}`;
const badge = (value: string) => (
  <span className={`verdict ${value.toLowerCase()}`}>{value}</span>
);
function RateCard({ title, value, testId }: { title: string; value: Rate; testId?: string }) {
  return (
    <div className="metric-card">
      <span>{title}</span>
      <strong data-testid={testId}>{percent(value.value)}</strong>
      <small>
        {value.numerator} / {value.sample_count}
        {value.scenario_count !== undefined
          ? ` · ${value.scenario_count} scenarios`
          : ""}
        {value.unknown_count !== undefined
          ? ` · ${value.unknown_count} unknown`
          : ""}
      </small>
      {value.interval && (
        <small>
          Archived descriptive resampling range: {percent(value.interval.low)}–
          {percent(value.interval.high)}
        </small>
      )}
      {value.scope && <small>{value.scope}</small>}
    </div>
  );
}
export function BenchmarkPanel() {
  const [datasets, setDatasets] = useState<Dataset[]>([]),
    [agents, setAgents] = useState<Agent[]>([]),
    [dataset, setDataset] = useState("customer-support@5"),
    [agent, setAgent] = useState("support-safe"),
    [split, setSplit] = useState("development"),
    [trials, setTrials] = useState(1),
    [chosen, setChosen] = useState<string[]>([]),
    [history, setHistory] = useState<Evaluation[]>([]),
    [selected, setSelected] = useState(""),
    [evaluation, setEvaluation] = useState<Evaluation | null>(null),
    [detail, setDetail] = useState<Detail | null>(null),
    [error, setError] = useState(""),
    [starting, setStarting] = useState(false),
    [baseline, setBaseline] = useState(""),
    [candidate, setCandidate] = useState(""),
    [comparison, setComparison] = useState<Comparison | null>(null),
    [comparing, setComparing] = useState(false),
    [filter, setFilter] = useState("all");
  useEffect(() => {
    let active = true;
    void Promise.all([api<Dataset[]>("/datasets"), api<Agent[]>("/agents")])
      .then(([d, a]) => {
        if (active) {
          setDatasets(d);
          setAgents(a);
        }
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const data = await api<Evaluation[]>("/evaluations");
        if (active) setHistory(data);
      } catch (e) {
        if (active) setError(String(e));
      } finally {
        if (active) timer = setTimeout(load, 1200);
      }
    }
    void load();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const data = await api<Evaluation>("/evaluations/" + selected);
        if (active) setEvaluation(data);
        if (
          active &&
          data.status !== "completed" &&
          data.status !== "cancelled"
        )
          timer = setTimeout(load, 700);
      } catch (e) {
        if (active) {
          setError(String(e));
          timer = setTimeout(load, 1500);
        }
      }
    }
    void load();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [selected]);
  const ds = datasets.find((d) => `${d.id}@${d.version}` === dataset);
  const compatibleAgents = agents.filter((a) =>
    a.content.tool_schema_version === (ds?.version === 6 ? "support-tools-v2" : "support-tools-v1")
  );
  const available =
    ds?.scenarios.filter((s) => split === "all" || s.split === split) ?? [];
  const total = evaluation
      ? evaluation.manifest.scenario_ids.length * evaluation.manifest.trials
      : 0,
    completed = evaluation?.trials.filter((t) => t.result).length ?? 0;
  function selectEvaluation(id: string) {
    setSelected(id);
    setEvaluation(null);
    setDetail(null);
  }
  async function start() {
    setStarting(true);
    setError("");
    try {
      const response = await api<{ id: string }>("/evaluations", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": Array.from(
            crypto.getRandomValues(new Uint32Array(4)),
          ).join("-"),
        },
        body: JSON.stringify({
          dataset_id: ds?.id,
          dataset_version: ds?.version ?? 1,
          agent_id: agent,
          agent_version: 1,
          split,
          trials,
          scenario_ids: chosen,
        }),
      });
      selectEvaluation(response.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setStarting(false);
    }
  }
  async function inspect(id: string) {
    setError("");
    try {
      setDetail(await api<Detail>("/trials/" + id));
    } catch (e) {
      setError(String(e));
    }
  }
  async function compare() {
    setComparing(true);
    setError("");
    setComparison(null);
    try {
      setComparison(
        await api<Comparison>("/comparisons", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            baseline_id: baseline,
            candidate_id: candidate,
          }),
        }),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setComparing(false);
    }
  }
  const metrics = evaluation?.metrics;
  return (
    <div className="benchmark" data-testid="benchmark-panel">
      <div className="workspace">
        <section className="panel scenario">
          <div className="section-label">BENCHMARK CONFIGURATION</div>
          <h2>Customer support</h2>
          <label htmlFor="dataset">Dataset version</label>
          <select
            id="dataset"
            value={dataset}
            onChange={(e) => {
              setDataset(e.target.value);
              setAgent(e.target.value.endsWith("@6") ? "support-interaction-safe" : "support-safe");
              setChosen([]);
            }}
          >
            {datasets.map((d) => (
              <option key={`${d.id}@${d.version}`} value={`${d.id}@${d.version}`}>
                {d.title}
              </option>
            ))}
          </select>
          <label htmlFor="benchmark-agent">Agent configuration</label>
          <select
            id="benchmark-agent"
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
          >
            {compatibleAgents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.content.title}
              </option>
            ))}
          </select>
          <details><summary>Model configuration and limits</summary><pre>{JSON.stringify(agents.find(a => a.id === agent)?.content.model_configuration, null, 2)}</pre><p>Credentials stay on the worker. Budgets apply to each trial, including the capability probe.</p></details>
          <label htmlFor="split">Scenario split</label>
          <select
            id="split"
            value={split}
            onChange={(e) => {
              setSplit(e.target.value);
              setChosen([]);
            }}
          >
            <option value="development">Development</option>
            <option value="held-out">Held-out · evaluation only</option>
            <option value="all">All published scenarios</option>
          </select>
          <label htmlFor="trials">Trials per scenario</label>
          <input
            id="trials"
            type="number"
            min="1"
            max="5"
            value={trials}
            onChange={(e) => setTrials(Number(e.target.value))}
          />
          <details className="scenario-selection">
            <summary>
              Select scenarios · {chosen.length || available.length} selected
            </summary>
            <p>Leave all unchecked to run the complete selected split.</p>
            {available.map((s) => (
              <label key={s.id}>
                <input
                  type="checkbox"
                  checked={chosen.includes(s.id)}
                  onChange={(e) =>
                    setChosen(
                      e.target.checked
                        ? [...chosen, s.id]
                        : chosen.filter((id) => id !== s.id),
                    )
                  }
                />
                <span>
                  {s.id} · {s.title}
                </span>
              </label>
            ))}
          </details>
          <p className="hint">
            {(chosen.length || available.length) * trials} isolated trials ·
            selected model configuration
          </p>
          <Button
            onClick={start}
            disabled={
              starting ||
              !ds ||
              !Number.isInteger(trials) ||
              trials < 1 ||
              trials > 5
            }
          >
            <Play size={15} />
            {starting ? "Starting…" : "Run benchmark"}
          </Button>
          <p className="hint">
            {ds?.version === 6
              ? "These five source-visible cases are for development. Results describe the offline reference agent."
              : "Held-out cases are not used to tune either policy. These results describe reference behavior, not production LLM quality."}
          </p>
          <details>
            <summary>Version references</summary>
            <p>Dataset SHA-256</p>
            <code>{ds?.sha256}</code>
            <p>
              Prompt:{" "}
              {agents.find((a) => a.id === agent)?.content.prompt_version}
            </p>
            <p>
              Tools:{" "}
              {agents.find((a) => a.id === agent)?.content.tool_schema_version}
            </p>
          </details>
        </section>
        <section className="panel outcome" aria-live="polite">
          <div className="section-label">EVALUATION PROGRESS</div>
          {!selected ? (
            <div className="empty">
              <GitCompareArrows size={36} />
              <h2>Compare behavior with evidence.</h2>
              <p>
                Run a published dataset. Inspect each trial, then compare
                another agent configuration.
              </p>
            </div>
          ) : !evaluation ? (
            <p>Loading evaluation…</p>
          ) : (
            <>
              <div className="result-heading">
                <h2 data-testid="benchmark-status">{evaluation.status}</h2>
                {badge(evaluation.manifest.agent.id)}
              </div>
              <code data-testid="benchmark-id">{evaluation.id}</code>
              <div className="progress-row">
                <progress
                  max={total || 1}
                  value={completed}
                  aria-label="Completed trials"
                />
                <span data-testid="benchmark-progress">
                  {completed} / {total} trials
                </span>
              </div>
              <p className="hint">
                {evaluation.manifest.split} · {evaluation.manifest.trials}{" "}
                trial(s) per scenario · dataset v
                {evaluation.manifest.dataset.version}
              </p>
              {metrics && (
                <>
                  {metrics.cohort && (
                    <p className="hint" data-testid="cohort-counts">
                      Planned {metrics.cohort.planned_slots} · Started {metrics.cohort.started_trials} ·
                      Terminal {metrics.cohort.terminal_trials} · Determinate {metrics.determinate_trials} · PASS {metrics.cohort.pass_trials} ·
                      FAIL {metrics.cohort.fail_trials} · INDETERMINATE {metrics.cohort.indeterminate_trials} ·
                      Pending {metrics.cohort.missing_or_pending_slots}
                    </p>
                  )}
                  {metrics.cohort && (
                    <p className="hint" data-testid="failure-classes">
                      Failure classes: agent {metrics.cohort.failure_classes.agent} · provider {metrics.cohort.failure_classes.provider} · evaluator {metrics.cohort.failure_classes.evaluator} · infrastructure {metrics.cohort.failure_classes.infrastructure} · cancelled {metrics.cohort.failure_classes.cancelled}
                    </p>
                  )}
                  <div className="benchmark-metrics">
                    {metrics.end_to_end_valid_completion && (
                      <RateCard title="END-TO-END VALID COMPLETION" value={metrics.end_to_end_valid_completion} testId="end-to-end-completion" />
                    )}
                    <RateCard
                      title="CONDITIONAL PASS"
                      value={metrics.task_success_rate}
                      testId="conditional-task-success"
                    />
                    {metrics.business_fulfillment && (
                      <RateCard title="BUSINESS FULFILLMENT" value={metrics.business_fulfillment} testId="business-fulfillment" />
                    )}
                    <RateCard
                      title="UNSAFE TRIALS"
                      value={metrics.unsafe_action_rate}
                    />
                    {metrics.unsafe_evidence && (
                      <div className="metric-card">
                        <span>SAFETY EVIDENCE</span>
                        <strong>{metrics.unsafe_evidence.proven_unsafe_trials} proven unsafe</strong>
                        <small>{metrics.unsafe_evidence.unknown_safety_slots} unknown safety slots</small>
                        <small>{metrics.unsafe_evidence.unauthorized_attempts} unauthorized · {metrics.unsafe_evidence.invalid_argument_attempts} invalid arguments · {metrics.unsafe_evidence.forbidden_effects} forbidden effects</small>
                      </div>
                    )}
                    <RateCard
                      title="RECOVERY SUCCESS"
                      value={metrics.recovery_success_rate}
                    />
                    <RateCard
                      title="DUPLICATE EFFECTS"
                      value={metrics.duplicate_side_effect_rate}
                    />
                    <RateCard
                      title="TOOL-CALL VALIDITY"
                      value={metrics.tool_call_validity}
                    />
                    {metrics.repeated_run_reliability && (
                      <div className="metric-card">
                        <span>REPEATED-RUN RELIABILITY</span>
                        <strong>{percent(metrics.repeated_run_reliability.value)}</strong>
                        <small>{metrics.repeated_run_reliability.sample_count} complete / {metrics.repeated_run_reliability.planned_scenarios ?? metrics.repeated_run_reliability.sample_count} planned scenarios</small>
                        {metrics.repeated_run_reliability.excluded_scenarios !== undefined && <small>{metrics.repeated_run_reliability.excluded_scenarios} excluded · {metrics.repeated_run_reliability.indeterminate_repeats} indeterminate · {metrics.repeated_run_reliability.missing_repeats} missing repeats</small>}
                      </div>
                    )}
                    <div className="metric-card">
                      <span>STARTED-TO-TERMINAL LATENCY</span>
                      <strong>{ms(metrics.execution_latency_ms.p50)}</strong>
                      <small>p50 · p95 {ms(metrics.execution_latency_ms.p95)}</small>
                      <small>{metrics.execution_latency_ms.sample_count} observed / {metrics.execution_latency_ms.population_count ?? metrics.execution_latency_ms.sample_count} terminal</small>
                    </div>
                    {metrics.active_task_latency_ms && <div className="metric-card"><span>ACTIVE ATTEMPT TIME</span><strong>{ms(metrics.active_task_latency_ms.p50)}</strong><small>p95 {ms(metrics.active_task_latency_ms.p95)} · {metrics.active_task_latency_ms.sample_count} observed / {metrics.active_task_latency_ms.population_count} terminal</small></div>}
                    {metrics.prestart_wait_ms && <div className="metric-card"><span>PRESTART WAIT</span><strong>{ms(metrics.prestart_wait_ms.p50)}</strong><small>p95 {ms(metrics.prestart_wait_ms.p95)} · {metrics.prestart_wait_ms.sample_count} observed / {metrics.prestart_wait_ms.population_count} started</small></div>}
                    {metrics.evaluation_elapsed_ms && <div className="metric-card"><span>WHOLE EVALUATION</span><strong>{ms(metrics.evaluation_elapsed_ms.value)}</strong><small>Creation to aggregate finalization</small></div>}
                    {metrics.model_latency_ms && <div className="metric-card"><span>MODEL REQUEST LATENCY</span><strong>{ms(metrics.model_latency_ms.p50)}</strong><small>p95 {ms(metrics.model_latency_ms.p95)} · {metrics.model_latency_ms.sample_count} observed / {metrics.model_latency_ms.population_count ?? metrics.model_latency_ms.sample_count} requests, including failures</small></div>}
                    <div className="metric-card">
                      <span>KNOWN TOKEN SUBTOTALS</span>
                      <strong data-testid="token-usage">Input {metrics.token_usage.input ?? "Unavailable"} · Output {metrics.token_usage.output ?? "Unavailable"}</strong>
                      <small>Input {metrics.token_usage.input_known_requests ?? metrics.token_usage.sample_count} / {metrics.token_usage.request_count ?? metrics.token_usage.sample_count} requests · Output {metrics.token_usage.output_known_requests ?? metrics.token_usage.sample_count} / {metrics.token_usage.request_count ?? metrics.token_usage.sample_count}</small>
                    </div>
                    {(["estimated_cost_usd", "reported_cost_usd"] as const).map(key => {
                      const cost = metrics[key];
                      const perSuccess = key === "estimated_cost_usd" ? metrics.estimated_cost_per_success_usd ?? metrics.cost_per_success_usd : metrics.reported_cost_per_success_usd;
                      return <div className="metric-card" key={key}>
                        <span>{key === "estimated_cost_usd" ? "ESTIMATED COST" : "PROVIDER-REPORTED COST"}</span>
                        <strong data-testid={key}>{money(cost?.value ?? null)}</strong>
                        <small>{cost ? `Known subtotal · ${cost.sample_count} / ${cost.request_count} requests` : "Request coverage unavailable"}</small>
                        <small>Per PASS: {money(perSuccess?.value ?? null)}{perSuccess ? ` · ${perSuccess.success_count} PASS trials` : " · success count unavailable"}</small>
                        {cost?.complete === false && <small>Incomplete cost coverage</small>}
                      </div>;
                    })}
                  </div>
                  <p className="hint">
                    Conditional PASS uses {metrics.task_success_rate.sample_count} determinate trials.
                    {metrics.cohort ? ` End-to-end completion uses all ${metrics.cohort.planned_slots} planned slots.` : ` Coverage: ${metrics.determinate_trials} determinate · ${metrics.indeterminate_trials} indeterminate · ${metrics.total_trials} total.`}
                  </p>
                  {metrics.pricing && <p className="hint">Cost basis: {metrics.pricing.status} · checked {metrics.pricing.checked_at ?? "unavailable"} · {metrics.pricing.source ?? "source unavailable"}</p>}
                  <details>
                    <summary>Metric definitions and uncertainty</summary>
                    {metrics.notes.map((note) => (
                      <p key={note}>{note}</p>
                    ))}
                  </details>
                </>
              )}
              <h3>Scenario results</h3>
              <label htmlFor="result-filter" className="sr-only">
                Filter scenario results
              </label>
              <select
                id="result-filter"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              >
                <option value="all">All outcomes</option>
                <option>PASS</option>
                <option>FAIL</option>
                <option>INDETERMINATE</option>
              </select>
              <div className="table-scroll">
                <table className="result-table">
                  <thead>
                    <tr>
                      <th>Scenario</th>
                      <th>Trial</th>
                      <th>Outcome</th>
                      <th>Failure</th>
                      <th>Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {evaluation.trials
                      .filter(
                        (t) => filter === "all" || t.result?.verdict === filter,
                      )
                      .map((t) => (
                        <tr key={t.id}>
                          <td
                            title={
                              datasets.find((d) => d.version === evaluation.manifest.dataset.version)?.scenarios.find((s) => s.id === t.scenario_id)
                                ?.title
                            }
                          >
                            {t.scenario_id}
                          </td>
                          <td>{t.trial_index}</td>
                          <td>{badge(t.result?.verdict ?? t.status)}</td>
                          <td>{t.result?.failure_category ?? "—"}</td>
                          <td>
                            <button
                              className="inspect-button"
                              onClick={() => inspect(t.id)}
                              aria-label={`Inspect ${t.scenario_id} trial ${t.trial_index}`}
                            >
                              Inspect <ChevronRight size={12} />
                            </button>
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {detail && (
        <section
          className="panel evidence-panel"
          data-testid="assertion-evidence"
        >
          <div className="section-label">ASSERTION EVIDENCE</div>
          <h2>
            {detail.result
              ? `${detail.result.verdict} · ${detail.result.failure_category ?? "No failure"}`
              : "Trial in progress"}
          </h2>
          <code>{detail.trial_id}</code>
          {detail.result?.model_requests && <details><summary>Model requests and usage</summary><pre>{JSON.stringify(detail.result.model_requests, null, 2)}</pre></details>}
          {detail.result?.provider_error && <p>Provider: {detail.result.provider_error}</p>}
          {detail.result?.error && <p className="bad">{detail.result.error}</p>}
          {detail.result?.assertions.map((a) => (
            <details key={a.id}>
              <summary>
                {badge(a.verdict)} {a.id.replaceAll("_", " ")}
              </summary>
              <div className="evidence-values">
                <div>
                  <p>Expected</p>
                  <pre>{JSON.stringify(a.expected, null, 2)}</pre>
                </div>
                <div>
                  <p>Actual</p>
                  <pre>{JSON.stringify(a.actual, null, 2)}</pre>
                </div>
              </div>
              <p>
                Tool events:{" "}
                {a.event_sequences.length
                  ? a.event_sequences.join(", ")
                  : "No tool calls"}
              </p>
            </details>
          ))}
          <h3>Tool events</h3>
          <ol className="benchmark-events">
            {detail.events.map((e) => (
              <li key={e.sequence}>
                <details>
                  <summary>
                    {e.sequence} · {e.tool} ·{" "}
                    {e.schema_version === "3.0"
                      ? `${e.kind} · ${e.source}`
                      : e.fault === "none" ? "no injected fault" : e.fault}
                  </summary>
                  <p>
                    {e.schema_version === "3.0"
                      ? e.kind === "tool"
                        ? `Arguments: ${e.metadata?.arguments_valid ? "valid" : "invalid"} · Permission: ${e.metadata?.authorized ? "allowed" : "denied"}`
                        : `Origin: ${e.source}${e.metadata?.target_operation_key ? ` · Redelivery of ${e.metadata.target_operation_key}` : ""}`
                      : `Arguments: ${e.arguments_valid ? "valid" : "invalid"} · Permission: ${e.authorized ? "allowed" : "denied"} · Transport deliveries: ${e.transport_requests}`}
                  </p>
                  <code>{e.operation_key}</code>
                  <pre>{JSON.stringify(e.response, null, 2)}</pre>
                </details>
              </li>
            ))}
          </ol>
          <p>Evaluator: {detail.result?.evaluator_version}</p>
          <code>Trace: {detail.result?.trace_id}</code>
        </section>
      )}
      <section className="panel comparison-panel">
        <div className="section-label">PAIRED AGENT COMPARISON</div>
        <h2>Find improvements and regressions.</h2>
        <div className="comparison-controls">
          <div>
            <label htmlFor="baseline">Baseline evaluation</label>
            <select
              id="baseline"
              value={baseline}
              onChange={(e) => setBaseline(e.target.value)}
            >
              <option value="">Select a baseline</option>
              {history.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.manifest.agent.id} · {h.id.slice(0, 8)} · {h.status}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="candidate">Candidate evaluation</label>
            <select
              id="candidate"
              value={candidate}
              onChange={(e) => setCandidate(e.target.value)}
            >
              <option value="">Select a candidate</option>
              {history.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.manifest.agent.id} · {h.id.slice(0, 8)} · {h.status}
                </option>
              ))}
            </select>
          </div>
          <Button
            variant="outline"
            disabled={!baseline || !candidate || comparing}
            onClick={compare}
          >
            <GitCompareArrows size={15} />
            {comparing ? "Comparing…" : "Compare evaluations"}
          </Button>
        </div>
        {comparison && (
          <div data-testid="comparison-result">
            <p>
              {comparison.provisional
                ? "Provisional comparison · incomplete evaluations"
                : "Completed paired comparison"}{" "}
              · {comparison.comparable_count} comparable /{" "}
              {comparison.sample_count} pairs
            </p>
            <p className="hint" data-testid="comparison-kind">
              {comparison.comparison_kind === "real_model"
                ? "Real-model agent outcomes"
                : comparison.comparison_kind === "mixed_agents"
                  ? "Mixed real-model and offline agent outcomes"
                  : comparison.comparison_kind === "offline_control"
                    ? "Offline reference-agent outcomes"
                    : "Agent type unavailable"}
              {comparison.baseline_model && comparison.candidate_model
                ? ` · ${comparison.baseline_model} vs ${comparison.candidate_model}`
                : ""}
            </p>
            <div className="comparison-counts">
              {Object.entries(comparison.counts).map(([key, value]) => (
                <div key={key}>
                  <strong data-testid={`comparison-${key}`}>{value}</strong>
                  <span>{key}</span>
                </div>
              ))}
            </div>
            <p>
              Paired success change:{" "}
              {comparison.paired_success_delta === null
                ? "Unavailable"
                : `${(comparison.paired_success_delta * 100).toFixed(1)} percentage points`}
            </p>
            <div className="table-scroll">
              <table className="result-table">
                <thead>
                  <tr>
                    <th>Scenario</th>
                    <th>Trial</th>
                    <th>Change</th>
                    <th>Assertion changes</th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.pairs.map((p) => (
                    <tr key={`${p.scenario_id}:${p.trial_index}`}>
                      <td>{p.scenario_id}</td>
                      <td>{p.trial_index}</td>
                      <td>{badge(p.classification)}</td>
                      <td>
                        {p.assertion_regressions.length > 0 && (
                          <span className="bad">
                            Worse: {p.assertion_regressions.join(", ")}
                          </span>
                        )}
                        {p.assertion_improvements.length > 0 && (
                          <span className="good">
                            {" "}
                            Better: {p.assertion_improvements.join(", ")}
                          </span>
                        )}
                        {!p.assertion_improvements.length &&
                          !p.assertion_regressions.length &&
                          "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {comparison.notes.map((n) => (
              <p className="hint" key={n}>
                {n}
              </p>
            ))}
          </div>
        )}
      </section>
      <section className="history">
        <div className="section-label">BENCHMARK EVALUATIONS</div>
        {history.length ? (
          history.map((h) => (
            <button key={h.id} onClick={() => selectEvaluation(h.id)}>
              <code>{h.id.slice(0, 8)}</code>
              <span>{h.manifest.agent.id}</span>
              <span>{h.manifest.split}</span>
              <span className="good">{h.status}</span>
              <ChevronRight size={14} />
            </button>
          ))
        ) : (
          <p>No benchmark evaluations yet.</p>
        )}
      </section>
    </div>
  );
}
