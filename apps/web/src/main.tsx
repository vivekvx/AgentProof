import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ShieldCheck, Play, ArrowRight, Activity } from "lucide-react";
import { Button } from "./components/ui/button";
import "./style.css";
import { BenchmarkPanel } from "./BenchmarkPanel";

type Event = {
  sequence: number;
  tool: string;
  outcome: string;
  timestamp: string;
  trace_id: string;
  span_id: string;
  side_effect_committed: boolean;
  output: unknown;
};
type Result = {
  status: string;
  failure_kind: string | null;
  side_effect_count: number;
  trace_id: string;
  final_state: { status?: string };
  checks: {
    name: string;
    passed: boolean;
    expected: unknown;
    actual: unknown;
  }[];
  error: string | null;
};
type Run = {
  id: string;
  status: string;
  created_at: string;
  manifest: {
    agent: string;
    scenario: { sha256: string };
    evaluator: { sha256: string };
  };
  result: Result | null;
  events: Event[];
};
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch("/api/v1" + path, init);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}
function App() {
  const [view, setView] = useState("single");
  const [agent, setAgent] = useState("safe"),
    [runs, setRuns] = useState<Run[]>([]),
    [selected, setSelected] = useState<string | null>(null),
    [run, setRun] = useState<Run | null>(null),
    [error, setError] = useState(""),
    [starting, setStarting] = useState(false);
  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const items = await request<Run[]>("/runs");
        if (active) {
          setRuns(items);
          setError("");
        }
      } catch (e) {
        if (active) setError(String(e));
      }
    };
    void load();
    const timer = setInterval(load, 1500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    const load = async () => {
      try {
        const item = await request<Run>("/runs/" + selected);
        if (active) setRun(item);
      } catch (e) {
        if (active) setError(String(e));
      }
    };
    void load();
    const timer = setInterval(load, 800);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [selected]);
  async function start() {
    setStarting(true);
    setError("");
    try {
      const created = await request<{ id: string }>("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent }),
      });
      setRun(null);
      setSelected(created.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setStarting(false);
    }
  }
  return (
    <div className="shell">
      <header>
        <a className="brand" href="/">
          <ShieldCheck size={24} /> AgentProof <span>LAB</span>
        </a>
        <span className="environment">LOCAL EVALUATION</span>
      </header>
      <main>
        <div className="eyebrow">
          {view === "benchmark"
            ? "RELIABILITY / BENCHMARKS"
            : "RELIABILITY / SCENARIO 001"}
        </div>
        <h1>
          {view === "benchmark" ? (
            <>
              Evaluate agent behavior.
              <br />
              Compare actual outcomes.
            </>
          ) : (
            <>
              Can your agent handle
              <br />
              an uncertain outcome?
            </>
          )}
        </h1>
        <p className="intro">
          {view === "benchmark" ? (
            <>
              Run versioned support scenarios with controlled faults.
              <br />
              Inspect each outcome across isolated trials.
            </>
          ) : (
            <>
              An order cancellation succeeds. The API response times out.
              <br />
              Check what the agent does next.
            </>
          )}
        </p>
        <nav className="view-tabs" aria-label="Evaluation mode">
          <button
            role="tab"
            aria-selected={view === "single"}
            onClick={() => setView("single")}
          >
            Single scenario
          </button>
          <button
            role="tab"
            aria-selected={view === "benchmark"}
            onClick={() => setView("benchmark")}
          >
            Benchmarks
          </button>
        </nav>
        {view === "benchmark" ? (
          <BenchmarkPanel />
        ) : (
          <>
            <div className="workspace">
              <section className="panel scenario">
                <div className="section-label">TEST CONFIGURATION</div>
                <h2>Order cancellation</h2>
                <div className="fault">
                  <Activity size={18} />
                  <div>
                    <strong>Timeout after commit</strong>
                    <p>Cancellation commits before the timeout response.</p>
                  </div>
                </div>
                <label htmlFor="agent">Reference agent</label>
                <select
                  id="agent"
                  value={agent}
                  onChange={(e) => setAgent(e.target.value)}
                >
                  <option value="safe">Safe agent · check order state</option>
                  <option value="duplicate">
                    Duplicate agent · retry cancellation
                  </option>
                </select>
                <p className="hint">
                  Deterministic tools. No external model calls.
                </p>
                <Button onClick={start} disabled={starting}>
                  <Play size={15} />
                  {starting ? "Starting…" : "Start evaluation"}
                </Button>
                <div className="assertions">
                  <div className="section-label">EXPECTED FINAL STATE</div>
                  <p>
                    <ArrowRight size={14} /> Order status: cancelled
                  </p>
                  <p>
                    <ArrowRight size={14} /> Cancellation side effects: 1
                  </p>
                  <p>
                    <ArrowRight size={14} /> Timeout injection: confirmed
                  </p>
                </div>
              </section>
              <section className="panel outcome" aria-live="polite">
                <div className="section-label">EVALUATION RESULT</div>
                {!selected ? (
                  <div className="empty">
                    <ShieldCheck size={38} />
                    <h2>Evidence starts with a run.</h2>
                    <p>
                      Start the test to inspect the final state and tool
                      timeline.
                    </p>
                  </div>
                ) : !run ? (
                  <p>Loading run…</p>
                ) : (
                  <>
                    <div className="result-heading">
                      <h2 data-testid="outcome">
                        {run.result?.status.replaceAll("_", " ") ?? run.status}
                      </h2>
                      <span className={"badge " + (run.result?.status ?? "")}>
                        {run.manifest.agent} agent
                      </span>
                    </div>
                    <code className="run-id">{run.id}</code>
                    {run.result && (
                      <>
                        <div className="metrics">
                          <div>
                            <span>ORDER STATE</span>
                            <strong>
                              {run.result.final_state.status ?? "Unknown"}
                            </strong>
                          </div>
                          <div>
                            <span>SIDE EFFECTS</span>
                            <strong data-testid="side-effects">
                              {run.result.side_effect_count}
                            </strong>
                          </div>
                          <div>
                            <span>FAILURE TYPE</span>
                            <strong>{run.result.failure_kind ?? "None"}</strong>
                          </div>
                        </div>
                        {run.result.error && (
                          <p role="alert">{run.result.error}</p>
                        )}
                        <div className="checks">
                          {run.result.checks.map((check) => (
                            <div key={check.name}>
                              <span className={check.passed ? "good" : "bad"}>
                                {check.passed ? "PASS" : "FAIL"}
                              </span>
                              <span>{check.name.replaceAll("_", " ")}</span>
                              <code>
                                {String(check.actual)} / expected{" "}
                                {String(check.expected)}
                              </code>
                            </div>
                          ))}
                        </div>
                      </>
                    )}
                    <h3>Tool timeline</h3>
                    <ol className="timeline">
                      {run.events.map((event) => (
                        <li key={event.sequence}>
                          <span className="step">{event.sequence}</span>
                          <div>
                            <strong>{event.tool}</strong>
                            <span
                              className={
                                event.outcome === "ok" ? "good" : "warning"
                              }
                            >
                              {event.outcome.replaceAll("_", " ")}
                            </span>
                            <p>
                              {event.side_effect_committed
                                ? "Cancellation side effect committed"
                                : "Order state read"}{" "}
                              · {new Date(event.timestamp).toLocaleTimeString()}
                            </p>
                            <code>span {event.span_id}</code>
                          </div>
                        </li>
                      ))}
                    </ol>
                    {run.result && (
                      <details>
                        <summary>Trace and immutable references</summary>
                        <p>Trace ID</p>
                        <code>{run.result.trace_id}</code>
                        <p>
                          <a
                            href={`http://127.0.0.1:16686/trace/${run.result.trace_id}`}
                            target="_blank"
                            rel="noreferrer"
                            className="good"
                          >
                            Open exported trace in Jaeger ↗
                          </a>
                        </p>
                        <p>Scenario SHA-256</p>
                        <code>{run.manifest.scenario.sha256}</code>
                        <p>Evaluator SHA-256</p>
                        <code>{run.manifest.evaluator.sha256}</code>
                        <p>
                          Exported spans are available in Jaeger at
                          localhost:16686 when telemetry is enabled.
                        </p>
                      </details>
                    )}
                  </>
                )}
              </section>
            </div>
            {error && (
              <p role="alert" className="error">
                {error}
              </p>
            )}
            <section className="history">
              <div className="section-label">RECENT RUNS</div>
              {runs.length === 0 ? (
                <p>No runs yet.</p>
              ) : (
                runs.map((item) => (
                  <button
                    key={item.id}
                    onClick={() => {
                      setRun(null);
                      setSelected(item.id);
                    }}
                  >
                    <code>{item.id.slice(0, 8)}</code>
                    <span>{item.manifest.agent} agent</span>
                    <span
                      className={
                        item.result?.status === "failed" ? "bad" : "good"
                      }
                    >
                      {item.result?.status ?? item.status}
                    </span>
                    <time>
                      {new Date(item.created_at).toLocaleTimeString()}
                    </time>
                    <ArrowRight size={14} />
                  </button>
                ))
              )}
            </section>
          </>
        )}
      </main>
      <footer>AgentProof · Evidence from final state, not agent claims.</footer>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
