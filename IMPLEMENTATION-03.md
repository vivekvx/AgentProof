# Implementation 03: benchmark and evaluation engine

Implemented on October 31, 2026. The existing architecture and v1 API remain intact.
No commit, push, deployment, or deletion of existing application data was performed.

## Delivered behavior

- A declarative benchmark engine with isolated PostgreSQL fixtures for each trial.
- An active customer-support dataset with 32 reviewed cases: 24 development and 8 held-out.
- Cancellation, status, refund, and address tasks, including invalid requests and policy boundaries.
- Two immutable, offline agent-configuration snapshots: safe reconciliation and blind retry.
- Versioned Pydantic/JSON Schema contracts for datasets, scenarios, fixtures, faults, assertions, configurations, manifests, and results.
- Immutable dataset publications and content-addressed run references.
- Repeated trials with stable identities and no shared order state.
- Durable Temporal execution, bounded execution retries, partial failure, and cancellation cleanup.
- Assertion-level PASS, FAIL, and INDETERMINATE evidence from actual state and effect records.
- Paired comparisons with improvements, regressions, unchanged, missing, and indeterminate counts.
- A benchmark dashboard inside the existing design, with progress, bounded result tables, evidence, metrics, and comparisons.
- OpenTelemetry correlations for dataset, scenario, trial, evaluator, agent, and attempt identifiers.

The agent receives only the public request, available tool schemas, permissions,
and execution limits. It receives no assertions, fault profiles, review text,
scenario identity, split label, or expected outcomes. Tool responses omit commit evidence.
Model telemetry excludes prompt, tool argument, and response content.
Public objects are deep copies. An agent cannot change harness permissions, approval
authority, or limits by mutating its public context. A regression test first reproduced
an unauthorized refund through shared mutable objects, then confirmed the fix.

## Dataset review and publication

The active publication is `customer-support`, version **2**, using schema version **2.0**.
Each scenario contains initial state, request, permissions, a fault profile, private
assertions, limits, a review rationale, and a known PASS reference outcome.
All 32 reference outcomes are checked by executing the safe policy against real fixtures.

Engineering review found that the candidate v1 request text included test titles.
Those titles could reveal fault details. Its bytes and existing publication rows
were retained. A new v2 publication replaces that text with ordinary customer requests.
The API offers only v2 for new evaluations. Historical v1 references remain readable.

Review means semantic engineering review and executable reference checks.
It does not mean independent human review. Held-out cases are not used for policy
branches or tuning. They are excluded by default. This source-controlled split is
not a sealed third-party benchmark. Operators can inspect the repository and evidence.

Scenarios cover normal cancellation, status reads, partial refunds, address updates,
already satisfied requests, shipment constraints, missing orders, wrong customers,
invalid amounts and addresses, read-only permissions, approval grants and denials,
all eight initial fault kinds, call budgets, and held-out combinations.

## Fault triggers and observed semantics

Triggers count matching tool invocations inside each trial. Completed response-ledger
entries are replayed before fault logic. Platform retries therefore do not shift a trigger.

| Fault | Trigger | Observable result |
| --- | --- | --- |
| Timeout before commit | Configured matching invocation, before mutation | Timeout response, no business effect |
| Timeout after commit | Mutation and ledger commit before response delivery | Same timeout response, committed effect |
| Temporary API failure | First configured matching invocations | Retryable unavailable response, no mutation |
| Permanent API failure | Matching invocations from the trigger onward | Non-retryable unavailable response, no mutation |
| Malformed response | Normal operation, then malformed delivery | Invalid response structure; mutation can already be committed |
| Duplicate tool request | Two transport deliveries with the same operation key | One effect and a recorded transport-delivery count of two |
| Delayed response | Committed response waits for a bounded configured delay | Actual asynchronous delivery delay |
| Permission denial | Matching invocation before mutation | Permission-denied response and no effect |

The before/after timeout regression test checks effect counts of **0 and 1** after
the first call. It also checks that the agent-visible responses are identical.
Timeouts are controlled tool-response simulations, not stalled network sockets.
A declared permission violation is an unsafe agent action. An injected server denial
of an otherwise permitted call is a fault, not an unsafe action by itself.

## Evaluation and reliability

The evaluator checks final state, effect counts, forbidden effects, unauthorized
attempts, typed arguments, approval ordering, execution limits, resolution, fault
observation, and required recovery. It reads PostgreSQL, not agent claims.

| Result | Classification | Handling |
| --- | --- | --- |
| PASS | No failure category | All scenario assertions pass |
| FAIL | `agent` | Preserve failing assertion evidence |
| INDETERMINATE | `evaluator` | Non-retryable evaluator defect; preserve state and other trials |
| INDETERMINATE | `infrastructure` | Execution retries, then durable failure recording |
| INDETERMINATE | `cancelled` | Finalize pending trials and retain completed trials |

Execution activities heartbeat. Infrastructure execution attempts are bounded at three.
Failure/finalization recording retries until PostgreSQL returns. A permanent outage
leaves cleanup pending rather than fabricating a terminal verdict.

Tool keys use `TRIAL_ID:support-tools-v1:SEQUENCE`. Unique constraints and deferred
foreign keys connect effects to tool events. Parent/trial row locks fence late writes.
A repeated transport delivery uses the same key. A separate agent decision uses a
new sequence and can expose a duplicate effect. These controls do not promise
exactly-once activity execution or exactly-once effects in an external API.

Database review found that JSON `null` could satisfy an older SQL result check.
Migration 006 rejects JSON null, arrays, missing result contracts, and contradictory
verdict/failure categories. Application result validation enforces the same classification.
Regression tests exercise these cases. Existing v1 tables and wire schemas are unchanged.

## Supported metrics

| Metric | Definition and availability |
| --- | --- |
| Task success rate | PASS / determinate trials. Correct refusal and blocked outcomes can pass |
| Unsafe action rate | Trials with a declared permission, ownership, or approval violation / determinate trials |
| Recovery success rate | Successful recovery / scenarios that require recovery |
| Duplicate side-effect rate | Trials with repeated business effects / determinate trials |
| Tool-call validity | Schema-valid calls / recorded calls |
| p50/p95 execution latency | Observed database wall time after trial initialization; includes retry downtime |
| Token usage | Recorded values and sample counts; unavailable for current offline controls |
| Inference cost | Sum of known recorded costs and their sample count; unavailable for current controls |
| Cost per success | Recorded total / PASS count, only with complete cost coverage |

Empty denominators and unknown usage/cost values remain unavailable, not zero.
Rates include sample and scenario counts. Empirical 95% intervals resample scenarios
as clusters, retaining repeated trials together. Single-scenario intervals are unavailable.
These intervals do not establish external-model population performance.
A fixed seed is used only to reproduce statistical resampling. It does not make an LLM deterministic.

## Observed comparison

The native end-to-end flow executed both controls on all 32 scenarios, twice per scenario.
These are observed offline reference-policy results, not production LLM benchmark scores.

| Configuration | PASS | Determinate trials |
| --- | --- | --- |
| Safe reconciliation v1 | 64 | 64 |
| Blind retry v1 | 30 | 64 |

Using safe reconciliation as baseline and blind retry as candidate produced:
**34 regressions, 30 unchanged pairs, 0 improvements, 0 missing, and 0 indeterminate**.
Case `cs-016` records one cancellation for safe reconciliation and two for blind retry.
The dashboard exposes the failed side-effect assertion and tool sequence.

Pairs use scenario ID and trial index. Dataset content hash, evaluator, tool schema,
and scenario selection must match. Incompatible comparisons return HTTP 409.
Different trial counts produce explicitly counted missing pairs. Incomplete results
are labelled provisional. Missing and indeterminate pairs do not enter success deltas.
Assertion changes are reported even when the overall verdict is unchanged.

Raw measurements and snapshots are in `.local/implementation-03/observed-comparison.json`.
Latency measurements in that file describe this local harness, not LLM inference performance.

## Tests and commands

The following commands were executed from the repository root:

```sh
TEST_DATABASE_URL=postgresql://agentproof@127.0.0.1:55432/agentproof_test go test ./...
go vet ./...
uvx ruff check engine/agentproof engine/tests scripts
npm run build
E2E_BASE_URL=http://127.0.0.1:5173 npm run test:e2e
```

Full native Python verification used:

```sh
TEST_DATABASE_URL=postgresql://agentproof@127.0.0.1:55432/agentproof_test \
TEST_TEMPORAL_ADDRESS=127.0.0.1:7233 \
E2E_BASE_URL=http://127.0.0.1:5173 \
JAEGER_QUERY_URL=http://127.0.0.1:16687 \
EVIDENCE_DIR=/Users/vivek/Projects/AgentProof/.local/implementation-03 \
uv run --project engine --frozen pytest -q
```

Observed native results before the final result-integrity guards:

- All Go tests, static checks, and application builds passed.
- **59 Python tests passed**, including the original 31 tests and live traces.
- **5 browser tests passed**, including all original browser tests.
- After the integrity guards, all **23 benchmark engine/contract/property tests** passed.
- After the public-context isolation fix, all **24 benchmark engine/contract/property tests** passed.
- Both trace regression tests passed after adding complete-parent delivery polling.

The first browser attempt read the previous run before the next POST returned.
The test now waits for the returned evaluation ID. No outcome assertion was removed.
The clean-container trace check exposed an early-delivery race in the existing v1 test.
Its missing parent arrived later. Evidence confirmed 15 spans with no missing parent.
The test now waits for complete parent closure and the API root before asserting.

Final clean-container commands:

```sh
docker compose --env-file infra/verification.env -p agentproof02 up -d --build --wait
docker compose --env-file infra/verification.env -p agentproof02 --profile verification build verify
docker compose --env-file infra/verification.env -p agentproof02 --profile verification run --rm verify
```

Final clean-container verification exited with code **0**:

- **64 Python tests passed** in 115.54 seconds, with no skipped tests.
- **5 browser tests passed** in 57.8 seconds.
- All Go tests passed. The Go server and Vite application builds passed.
- Final Ruff, `go vet ./...`, and whitespace checks passed.
- PostgreSQL, Temporal, API, and frontend containers report healthy. The worker and Jaeger are running.

The complete final log is `.local/implementation-03/clean-verify-release.log`.
These durations are observed test-suite durations, not product performance metrics.
All 10 pre-existing native v1 run records remain unchanged. The preservation snapshot
is `.local/implementation-03/preservation.json`.

## Completion evidence

| Required developer action | Verified result and evidence |
| --- | --- |
| Select a dataset | Dashboard selects the published customer-support v2 dataset; browser test passes |
| Select an agent | Both immutable offline configurations are available and exercised |
| Run selected scenarios | Browser test executes all 32 scenarios with two trials per configuration |
| Inspect outcomes and evidence | Browser test opens PASS and FAIL assertion evidence and tool events |
| Run a second configuration | Both safe reconciliation and blind retry complete through the full stack |
| Compare results | Paired comparison runs in both directions; incompatible selections are rejected |
| Identify regressions | Observed safe-to-blind comparison reports 34 regressions with stored evidence |
| Repeat without shared state | Fixture isolation, stable operation replay, repeated browser trials, and real worker-interruption tests pass |

Desktop overview, comparison, evidence, and mobile screenshots are stored in
`.local/implementation-03/benchmark-*.png`. The 390-pixel mobile check found no
horizontal overflow. Live trace checks passed for both Go and Python, including
parent closure and required correlation fields. Clean-container trace and worker
recovery evidence are stored in `.local/verification/`.


Coverage includes invalid schemas, external-model rejection, public/private boundaries,
all reference outcomes, effect constraints, repeated-trial isolation, evaluator correctness,
partial failure, cancellation, wall/call limits, missing/indeterminate comparisons,
incompatible comparisons, Temporal ActivityEnvironment, workflow replay, and real SIGKILL recovery.
Hypothesis tests exercise invalid refund values, quantile bounds, and rate/interval counts.

## Traces and evidence

The live benchmark trace test checks both Go and Python services. It checks dataset
version, scenario ID, trial ID, evaluator version, workflow/activity correlation,
attempt number, failure category, and complete parent references.
Raw assertions and request content are excluded from model telemetry.
Identifiers are stored in traces and database records. No run-ID Prometheus labels were added.

Native artifacts use `.local/implementation-03`. Clean CI artifacts use `.local/verification`.
Key files include `benchmark-trace.json`, `benchmark-kill-recovery.json`, result snapshots,
and the verification logs. Failed application runs remain in PostgreSQL.
Tests remove only their own temporary fixture rows after saving recovery evidence.

## Changed files

- `engine/agentproof/benchmark/`: contracts, registry, public agent policy, simulator, evaluator, metrics, execution, activities, workflow.
- `engine/agentproof/worker.py`: register benchmark workflow and activities beside v1.
- `apps/api/internal/server/benchmark.go`, `benchmark_test.go`: v2 discovery, creation, progress, evidence, comparison, request idempotency, tests.
- `apps/api/internal/server/server.go`, `apps/api/cmd/server/main.go`: route and dispatch registration.
- `packages/contracts/benchmarks/`, `agents/`, `tools/`, `v2/`, `evaluators/support.v1.json`: immutable assets and schemas.
- `packages/contracts/assets.go`: embed the new contract assets.
- `db/migrations/004_benchmarks.sql`, `005_benchmark_guards.sql`, `006_benchmark_result_integrity.sql`: isolated state, immutability, identity and result guards.
- `apps/web/src/BenchmarkPanel.tsx`, `benchmark.css`, `main.tsx`: dashboard extension using the existing visual system.
- `engine/tests/test_benchmark.py`, `test_benchmark_workflow.py`, `test_benchmark_trace.py`, `benchmark_process_worker.py`: new verification coverage.
- `engine/tests/conftest.py`, `test_migration.py`, `test_trace_delivery.py`: fixture support, migration count, reliable export polling.
- `tests/e2e/benchmark.spec.ts`: full repeated evaluation, evidence, comparisons, invalid configuration.
- `scripts/export_benchmark_contracts.py`, `Makefile`: reproducible schema generation and verification.
- `engine/pyproject.toml`, `engine/uv.lock`, `go.mod`, `go.sum`: Hypothesis and Go JSON Schema validation dependencies.
- `compose.yaml`, `.github/workflows/ci.yml`: shared verification artifact path.
- `README.md`, `IMPLEMENTATION-03.md`: usage, observed results, and limits.

## Remaining limits and risks

- The reference policies use typed requests. They do not test natural-language understanding.
- Execution wall budgets apply to each active infrastructure attempt. Recorded latency also includes retry downtime.
- Only two offline reference configurations are supported. External LLM execution, token accounting, pricing imports, and external-agent uploads are not implemented.
- This is an engineering-reviewed seed benchmark. Independent human review and third-party hidden evaluation remain open.
- Held-out data is source-controlled and visible to operators. It is not an access-controlled secret dataset.
- Response replay assumes the deterministic reference policies. External model support needs durable decision/message recording; a fixed seed is insufficient.
- Run snapshots pin data, tool schemas, configurations, and evaluator descriptors. Execution binaries are not content-addressed. Behavior changes require explicit new versions.
- Timing and cost comparisons are not causal model-performance studies. Local latency includes harness and retry effects.
- Long database outages leave terminal recording pending. Force termination bypasses cancellation cleanup.
- Jaeger uses development memory storage. Abrupt process death can lose unfinished spans; database evidence remains authoritative.
- GitHub-hosted CI was not triggered. Only the configured CI commands were executed locally. No push or deployment was authorized.
