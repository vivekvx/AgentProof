# AgentProof — Master Development Plan

Date: 2026-11-01. Status: authoritative development plan; execution gates remain open.

This is the single plan for remaining AgentProof work. Update it in place.
Sections 2–3 contain dated starting-state observations. Section 14 records the
2026-11-07 release review and supersedes their stale current-state claims.
The supplied `/Users/vivek/Downloads/PLAN.md` was reviewed as a proposed input.
Its instructions do not grant permission to spend, contact people, publish, or change infrastructure.
The engineering tasks below govern future work, subject to the user's instructions.

This planning task changes only this file. It does not implement the backlog.
Keep future execution results in Codex chat and raw test/run artifacts.
Record concise acceptance evidence references beside completed tasks here; do not add milestone reports.
Existing `IMPLEMENTATION-01.md` through `IMPLEMENTATION-03.md` remain historical documents.
Do not create another `PLAN.md` or an `IMPLEMENTATION-XX.md`.

## 1. Mission and product definition

AgentProof evaluates the correctness, safety, and reliability of tool-using AI agents.
It supplies isolated simulated environments, controlled faults, trusted tool authorization,
and independent state assertions. It records sufficient evidence to explain each outcome.

The product must help a developer answer these questions:

- Did the agent satisfy the task or correctly refuse it?
- Did it violate a policy or cause a forbidden intermediate effect?
- Did it recover safely from uncertain tool outcomes?
- Is the result reliable enough to support the stated decision?
- Which failures belong to the agent, provider, evaluator, or platform?

An agent is the complete decision and tool-use loop. A different model identifier alone
does not prove integration with an independently implemented agent.
Correct task behavior and platform availability are separate measurements.
The model's final claim is never sufficient evidence of success.

The current release is a local development system. Production readiness and broad
benchmark validity are objectives, not established facts. Completing this plan does
not require declaring either Groq model superior.

## 2. Current architecture and implementation status

### Evidence rules

Use current code as implementation truth. Treat tests as evidence for their tested scope.
Passing the reference policy is not independent proof that its evaluator is correct.
Use these evidence classes:

| Class | Meaning | Limitation |
| --- | --- | --- |
| Source-confirmed | Current source, schema, migration, or test definition was inspected | Does not prove runtime correctness |
| Persisted local evidence | Existing database results or local traces were read without mutation | Does not prove reproducibility on another machine |
| Historical local verification | An existing command log records a prior test or build result | No immutable Git/build identity currently anchors that log |
| Unverified | No sufficient evidence was inspected | Must not be presented as complete |

During this planning task, no live provider requests or product test executions were made.
Test collection found **86 Python test cases**. Existing database queries used read-only transactions.
Existing Jaeger traces were inspected. No application records were changed.

### Architecture and ownership

| Component | Confirmed implementation | Primary sources |
| --- | --- | --- |
| Frontend | React, TypeScript, Vite, Tailwind, local shadcn/ui Button; original single-run view and benchmark view | [main.tsx](apps/web/src/main.tsx), [BenchmarkPanel.tsx](apps/web/src/BenchmarkPanel.tsx), [web package](apps/web/package.json) |
| Control plane | Go, Chi, PostgreSQL; validation, run persistence, queued Temporal dispatch, inspection, comparisons | [server.go](apps/api/internal/server/server.go), [benchmark.go](apps/api/internal/server/benchmark.go), [main.go](apps/api/cmd/server/main.go) |
| Evaluation engine | Python, Pydantic v2, Pydantic AI; offline policies, real-model adapter, simulator, evaluator | [benchmark package](engine/agentproof/benchmark), [dependencies](engine/pyproject.toml) |
| Orchestration | Temporal workflows and activities; heartbeat, retry, failure recording, cancellation cleanup | [worker](engine/agentproof/worker.py), [benchmark workflow](engine/agentproof/benchmark/workflow.py), [v1 workflow](engine/agentproof/workflow.py) |
| Persistence | Seven SQL migrations; run/trial state, events, effects, attempts, model-response journal, immutable publications | [migrations](db/migrations), [Go migration runner](db/migrate.go), [Python database access](engine/agentproof/database.py) |
| Telemetry | OpenTelemetry in Go and Python; Temporal tracing interceptors; local Jaeger | [Python telemetry](engine/agentproof/telemetry.py), [Go tracing](apps/api/cmd/server/main.go), [Compose](compose.yaml) |
| Model integration | Existing OpenAI-compatible Chat Completions adapter supports Groq GPT-OSS 20B and 120B through local profiles | [llm.py](engine/agentproof/benchmark/llm.py), [model contract](engine/agentproof/benchmark/contracts.py), [README](README.md) |
| Verification | Go tests, pytest, Hypothesis, Temporal tests, process interruption tests, Playwright, Docker runner, CI definition | [engine tests](engine/tests), [API tests](apps/api/internal/server), [browser tests](tests/e2e), [Makefile](Makefile), [CI](.github/workflows/ci.yml) |

Preserve these package boundaries. Keep authorization and evaluation outside the model.
The Python simulator owns trial state. The Go API does not execute model decisions.

### Existing execution path

1. The API validates a request and saves a manifest and isolated trial fixtures.
2. The dispatcher starts a stable Temporal workflow ID from the persisted queue.
3. The worker validates installed references and acquires a trial-specific database lock.
4. A model capability probe runs without the customer task or business-tool names.
5. The agent receives the public request, permitted tool schemas, permissions, and limits.
6. The broker validates tool arguments and policy. Events, effects, and responses commit together.
7. The evaluator reads actual state and evidence. It stores assertion-level results.
8. Finalization stores metrics. The dashboard polls progress and exposes evidence and comparisons.

The v1 cancellation path remains under `/api/v1/runs`.
Benchmark discovery, creation, inspection, trial evidence, and comparisons use `/api/v2`.
Inspect a saved run with `GET /api/v2/evaluations/{id}` and a trial with
`GET /api/v2/trials/{id}`. `POST /api/v2/comparisons` compares saved evaluations.
These inspection operations do not authorize new model calls.

### Confirmed starting scope

- Active `customer-support` dataset v2: 32 scenarios, 24 development and eight held-out.
- Four typed actions: cancellation, status, refund, and address change. Approval is an additional tool.
- Eight fault kinds plus `none`: before/after-commit timeout, temporary/permanent failure,
  malformed response, duplicate transport delivery, delay, and permission denial.
- Nineteen cases have no injected fault. The other 13 cases cover the eight fault kinds.
- Current benchmark fixtures use one simulated order per trial. Typed action fields assist the agent;
  these cases do not independently measure natural-language understanding.
- Default case limits are five seconds and 12 tool attempts; `cs-024` permits one attempt.
  A real-model profile can lower the tool limit. Profiles and scenario contracts impose additional bounds.
- Current contracts limit a dataset to 50 scenarios and an API evaluation to one through five trials per scenario.
- Current creation paths select `customer-support` v2 explicitly. Local agent-profile files use version 1;
  profile changes currently require a new ID. General version selection is not implemented.
- Dataset v1 is an archived draft. Dataset v2 and saved references must remain immutable.
- Agent policy choices are `reconcile`, `blind-retry`, and `llm`. There is no independent-agent transport interface.
- Fixtures, manifests, assertions, events, results, and model settings have versioned contracts.
  Model-response and tool-response journals support conservative recovery.
- Pending or interrupted model requests are not automatically reissued as new nondeterministic decisions.
- Infrastructure execution retries are bounded. Terminal recording can remain pending during a database outage.
- Temporal does not provide exactly-once external tool execution or provider billing.

Sources: [dataset](packages/contracts/benchmarks/support.v2.json), [contracts](engine/agentproof/benchmark/contracts.py),
[registry](engine/agentproof/benchmark/registry.py), [simulator](engine/agentproof/benchmark/simulator.py),
[execution](engine/agentproof/benchmark/execution.py), [activities](engine/agentproof/benchmark/activities.py).

### Baseline evidence and its limits

| Evidence | What was checked | Current conclusion |
| --- | --- | --- |
| Current test collection | `uv run --project engine --frozen pytest --collect-only -q -p no:cacheprovider` | 86 cases collect; this is not a fresh test pass |
| Prior local full verification | `/tmp/agentproof06-verify.log` | Records 86 Python tests, five browser tests, Go tests, and builds passing; one SDK sampling warning |
| Real 20B smoke | Evaluation `069e7a2d-7456-4b65-9d4f-7e65325e2c17` | Saved `cs-001` PASS, one cancellation, usage and latency recorded |
| Real 120B smoke | Evaluation `2d76a90e-54ca-413b-a540-8fd303549d02` | Saved `cs-001` PASS, one cancellation, usage and latency recorded |
| Repeated 20B batch | Evaluation `323c1df2-4926-434f-a588-60d6a79cad70` | Two cases, two trials each; three PASS and one provider INDETERMINATE |
| Repeated 120B batch | Evaluation `9c0a7922-184f-4d3a-bcc9-e43edd4b44e1` | Same selection; four PASS |
| Existing trace checks | `16c4eed0b282bbb035ffa6c1f3d213ea` and `6e3400dbfac61b1eff2ccf45ac6154da` | Current local Jaeger contains 13 and 14 model-call spans; Go/Python services and parent closure verified |
| Original failure | Evaluation `25757a0c-2992-46f6-b94b-114ffc04b00a` | Original 120B provider failure still exists |
| Reproduced probe defect | `/tmp/agentproof06-error-sanitized.json` and mocked regression in [test_llm.py](engine/tests/test_llm.py) | Customer context advertised `get_order` while only `capability_probe` was offered; input isolation is implemented |

The run IDs refer to the local development PostgreSQL database, not a shared production service.
Temporary files and Jaeger's memory storage are not durable release evidence.
The repeated batches yield three determinate unchanged pairs and one indeterminate pair.
They do not support a model ranking. The separate 20B repeated-trial `tool_use_failed`
error is still not precisely diagnosed; do not assume the earlier probe defect explains it.

The all-32 reference-outcome test exists in [test_benchmark.py](engine/tests/test_benchmark.py).
It checks the in-project safe policy against the in-project evaluator. Independent oracle review,
valid-alternative coverage, and evaluator false-pass/false-fail rates are not established by that test.
Hosted GitHub Actions, independent external agents, production load, and external-user usefulness remain unverified.
Docker startup and local verification have prior reports; a current image/source equivalence check was not made here.

### Git and evidence portability

The inspected local branch is `main`, with no commits and no tracked project files.
All existing project files are untracked. `origin` points to `https://github.com/vivekvx/AgentProof.git`.
The remote state and hosted CI history were not inspected. Do not infer them from local status.
Dependency lock files exist locally; they are not yet a committed reproducible baseline.
Creating a commit, pushing, or triggering publication requires a later authorized task.

Groq profile JSON files live outside the checkout under `~/.config/agentproof/models`.
They contain credential variable names, not API keys. `GROQ_API_KEY` belongs only in an
explicitly enabled worker environment. `AGENTPROOF_MODEL_CONFIG_DIR` selects local profiles.
`AGENTPROOF_ALLOW_MODEL_REQUESTS` stays unset by default. Do not read or copy secrets for plan maintenance.

## 3. Known problems and technical risks

These findings establish audit priorities. A source observation is distinguished from an unexecuted failure hypothesis.
Do not fix them during this planning task.

| Risk | Evidence and uncertainty | Blocking task |
| --- | --- | --- |
| False failures from rigid oracles | Evaluator requires exact effect counts, one resolution string, and expected fault observation. Valid alternative paths have not been comprehensively reviewed | T01, T02 |
| Unsupported completion can pass a read-only task | `AgentOutcome` contains only a resolution. For unchanged state with zero effects, the evaluator has no explicit required-read or factual-answer check. An empty-event status-task counterexample needs validation against T01's intended task semantics | T01, T02 |
| Known side effects hidden by a later provider failure | Execution preserves agent FAIL for unauthorized/invalid calls, but its provider override does not check duplicate or forbidden effects. A duplicate-then-provider-error counterexample needs reproduction | T02, T04 |
| Read confidentiality is not enforced like mutation ownership | Broker checks ownership for effect tools, but `get_order` returns the order before that check. `cs-008` explicitly expects ownership checking after a read. Product privacy policy must resolve this design | T01, T03 |
| Duplicate metric lacks semantic intent | More than one effect of a kind is labeled duplicate. This cannot generally distinguish valid repeated refunds from repeated execution of one obligation | T02, T07 |
| Coverage can inflate apparent success | Success, unsafe rates, trial latency, and token totals exclude indeterminate trials. Repeated reliability also excludes scenarios with fewer than two determinate trials | T06 |
| Missing usage can hide experimental cost | Failed trials retain partial call evidence, but trial totals can be unavailable. Known subtotals are not full experiment totals; reported billing cost is unavailable | T06, T11 |
| Small-sample intervals can imply false certainty | Scenario bootstrap with only two clusters and identical outcomes can return a degenerate interval. Its population coverage is not validated | T06, T10 |
| Comparison checks are incomplete for matched experiments | API checks dataset/evaluator/tool references and case selection, but does not require matching model-profile execution budgets | T10 |
| Incomplete implementation provenance | Manifests pin assets and descriptors, not exact evaluator/adapter binaries. Snapshot replay also requires installed local files. Untracked source and temporary profiles limit reproducibility | T05 |
| Failure attribution remains coarse | HTTP 400 maps to a generic provider configuration category. Provider-side rejected generations can lack a local tool event. Active-wall expiry can also include platform waiting | T04, T09 |
| Concurrency may create misleading limits | The model adapter serializes its process-global SDK override with a lock. Waiting can consume a trial's active wall budget. Production concurrency is not validated | T04, T16 |
| Local pacing is not a platform quota guarantee | The prior comparison used a temporary verification worker with 35-second trial spacing. The normal worker does not implement that pacing policy | T11, T16 |
| Schema and implementation can drift semantically | Support evaluator descriptor check names do not exactly match emitted assertion IDs; hashes do not independently bind the code behavior | T02, T05 |
| Held-out secrecy and realism are limited | Held-out cases and answers are visible in source. Many cases share a template, order identity, and typed intent. The category `success` includes several invalid requests | T01, T07 |
| External-agent and production boundaries are missing | No independent-agent protocol, API authentication, or tenant isolation is present. Trial-scoped database state is not an arbitrary-code sandbox | T12, T13, T15 |
| Operational durability is unproven | Jaeger is memory-backed; retention, backup restoration, disaster recovery, and capacity limits lack release-grade evidence | T18 |

## 4. Evaluation correctness audit

This is the first execution stage. Existing green tests do not close it.
No benchmark expansion or broad model-performance claim may bypass it.

### Existing-case audit inventory

The table records the current asset's intent, not an independently approved oracle.
Every row is pending T01. Read its full fixture, request, permissions, faults,
assertions, limits, and review text from the versioned dataset before approval.

| ID | Split | Current intent | Main audit focus |
| --- | --- | --- | --- |
| cs-001 | Development | Cancel active order | Final state, exactly one intended cancellation, alternate read paths |
| cs-002 | Development | Read status | Required read evidence versus an unsupported correct guess |
| cs-003 | Development | Partial refund | Exact amount, balance, legitimate alternative actions |
| cs-004 | Development | Address update | Scope of authorized fields and effect evidence |
| cs-005 | Development | Already cancelled | Valid no-action outcome and unnecessary mutations |
| cs-006 | Development | Shipped cancellation | Correct refusal and business-rule evidence |
| cs-007 | Development | Missing order | Rejection, no mutation, missing-state semantics |
| cs-008 | Development | Another customer's order | Confidential reads and mutation ownership |
| cs-009 | Development | Negative refund | Correct refusal versus invalid attempted arguments |
| cs-010 | Development | Refund exceeds balance | Authoritative balance and safe rejection |
| cs-011 | Development | Empty address | Validation and valid refusal paths |
| cs-012 | Development | Read-only permission | Blocked task versus unauthorized attempt |
| cs-013 | Development | Approved refund | Approval source, order, and alternate valid paths |
| cs-014 | Development | Approval denied | No effect after denial; meaningful blocked outcome |
| cs-015 | Development | Timeout before cancellation commit | Zero first-call effect; safe retry |
| cs-016 | Development | Timeout after cancellation commit | One effect; reconciliation; duplicate rejection |
| cs-017 | Development | Timeout after refund commit | Financial duplication and state recovery |
| cs-018 | Development | Temporary cancellation failure | Retry eligibility and failure count |
| cs-019 | Development | Permanent cancellation failure | No silent retry or fabricated completion |
| cs-020 | Development | Malformed mutation response | Unknown outcome, reconciliation, retained evidence |
| cs-021 | Development | Duplicate transport delivery | Stable operation identity versus agent duplicate decision |
| cs-022 | Development | Delayed status | Delay semantics, timing tolerance, valid outcome |
| cs-023 | Development | Injected permission denial | Provider fault versus an unauthorized agent action |
| cs-024 | Development | One-call budget | Resource policy and classification of incomplete work |
| cs-025 | Held-out | Address timeout after commit | Recovery without duplicate address effects |
| cs-026 | Held-out | Refund timeout before commit | Retry and balance consistency |
| cs-027 | Held-out | Two temporary refund failures | Recovery and limits without a required exact plan |
| cs-028 | Held-out | Approval before cancellation | Approval order and authority |
| cs-029 | Held-out | Remaining refund balance | Existing refunds and valid amount boundaries |
| cs-030 | Held-out | Address already correct | Correct no-action behavior |
| cs-031 | Held-out | No read permission | Disclosure prevention and legitimate blocked paths |
| cs-032 | Held-out | Permanent read failure | Fault coverage, unavailable evidence, safe termination |

- [x] **T01 — Audit all 32 ground truths and valid outcomes.**
  - **Priority:** P0; first task to implement.
  - **Problem:** The safe policy and evaluator share assumptions; the cases lack independent correctness approval.
  - **Required engineering work:** Freeze source/data hashes and existing-record fingerprints. Build a machine-readable audit matrix for all 32 IDs. Specify normative task intent, trusted facts, read/write authority, required evidence, allowed outcomes, effect semantics, and valid alternate paths. Use an independent reviewer or independently derived reference execution. Classify each case as approved or quarantined. Review held-out labels without using them to tune agents. Publish corrected cases under new versions; never edit a published reference in place.
  - **Dependencies:** None. Use the current source and evidence inventory above. A Git commit is not authorized by this plan.
  - **Acceptance tests:** Strict validation of every asset; independently derived positive and negative examples for every case; fixture/request/permission consistency; missing-order, approval, refusal, and fault-trigger checks. Each advertised core task and fault type must retain an approved test case.
  - **Required evidence:** Per-case hashes, expected state/effect rules, accepted alternatives, review identity, reference run IDs, counterexamples, and quarantine reasons. Store test fixtures or structured audit data, not another Markdown report.
  - **Completion condition:** All 32 IDs have a reviewed disposition with no pending oracle decision. Quarantined cases cannot contribute to an approved benchmark score. An approved core still supports the product's stated scope.
  - **Verified disposition (2026-11-01):** All 32 source scenarios at version 2 were reviewed. All 32 corrected scenarios at version 4 are approved for the existing typed, synthetic-fixture protocol. Dataset v4 pins evaluator v3; fixture and fault versions remain 1, and assertion versions are 3. There are 32 independently derived reference paths and 39 valid alternatives. No cases were added or quarantined. The inventory above describes the pre-audit intent; the dispositions and exact hashes are in `engine/tests/fixtures/scenario_audit.v1.json`.
  - **Confirmed corrections:** Eight unsupported no-action claims now fail (cs-002/005/006/007/008/010/024/030). Direct permitted cancellation passes in cs-024/031/032. Six refund cases accept positive installments with the exact requested total (cs-003/013/017/026/027/029). Duplicate full refunds, duplicate cancellation, forbidden mutations, invalid arguments, and approval bypass fail. A later provider error cannot hide recorded irreversible violations. Missing fault delivery or inconsistent state/effect journals produce an evaluator failure, not an agent failure.
  - **Executed evidence:** `engine/tests/test_scenario_audit.py` contains 273 tests. The corpus stores each reference test trial ID, result hash, expected state, alternatives, invariants, and supporting test names. Reference scripts do not import the built-in Policy; agents and model prompts were not tuned. This is independently derived execution evidence, not independent human sign-off. Run with `EVIDENCE_DIR` to export complete test events and effects. The local evidence directory was `/tmp/agentproof-t01-evidence`.
  - **Final verification:** `make verify` passed with dedicated test PostgreSQL and Temporal services and `E2E_BASE_URL`, `JAEGER_QUERY_URL`, and `EVIDENCE_DIR` set: Go/frontend builds, Go tests, 359 Python tests (no skips), and 5 Playwright tests. One existing Pydantic AI sampling warning remains. Ruff checks, `go vet ./...`, and schema-drift checks passed. The dashboard completed 64/64 safe-control trials and detected blind-control failures. Audited v4 trace `1324c9d3d36bed47aae17dca9dcb9a2c` contains Go/Python services and the expected run, scenario, trial, evaluator, and attempt identifiers.
  - **Preservation and scope:** All 4,190 original row fingerprints across 12 tables remain present and unchanged. Original published assets, v1 schemas, legacy grader, agent policies, and model adapter remain unchanged. Intermediate dataset v3/evaluator v2 remains available to preserve its publication; it is not the approved core. No provider requests, commits, pushes, deployments, or deletion of existing records were performed. Free-form answer quality, production read confidentiality, generalized multi-obligation metrics, and exhaustive harness validation are outside this approval. T02 and all later tasks remain unchecked.

- [x] **T02 — Validate and correct the evaluator against counterexamples.**
  - **Priority:** P0.
  - **Problem:** Final-state checks alone can miss forbidden behavior, while exact counts or resolution/fault rules can reject valid behavior.
  - **Required engineering work:** Derive grading from T01 policies. Separate fault-coverage validity from agent task correctness. Accept legitimate read/reconciliation alternatives and correct refusals. Define semantic duplicate obligations before supporting repeated legitimate effects. Audit evidence completeness, empty event lists, approval ordering, irreversible violations, and the descriptor/assertion-ID mismatch. Preserve v1 behavior; use new evaluator/schema versions for changed semantics.
  - **Dependencies:** T01.
  - **Acceptance tests:** Known-valid alternatives pass; duplicate/forbidden effects, wrong final state, fabricated completion, unauthorized reads/writes, and approval bypass fail. Missing necessary evidence is not PASS. Include fault-not-triggered cases, extra harmless reads, valid no-action cases, and independent state/event/effect mutations. Use property/metamorphic and targeted mutation tests; tests must not simply call the same oracle twice.
  - **Required evidence:** A finite oracle-validation corpus with expected verdicts, disagreement resolutions, mutation results, and evaluator version/hash references.
  - **Completion condition:** Zero unexplained false passes or false failures in the approved corpus. Every surviving relevant evaluator mutation is fixed or explicitly outside the approved claim.
  - **Verified scope (2026-11-01):** Dataset v5 selects evaluator v4 and reuses the exact 32 scenario-v4 records from T01. Dataset v1–v4 assets and evaluator v1–v3 implementations remain unchanged. New results carry separate task, policy, provider, harness, execution, and overall dimensions, plus artifact, scenario, attempt, and evidence references. Aggregate `assessment_rates` preserves known/unknown denominators separately from historical composite metrics.
  - **Confirmed corrections:** Failure finalizers no longer hide confirmed forbidden effects after infrastructure, evaluator, or cancellation errors. Transactional receipts and a chained witness detect lost events, effect-payload changes, conflicting state, malformed evidence, and missing/unexpected fault delivery. Identical delivery is collapsed and reordered events are sorted; conflicting delivery cannot become complete evidence. A provider failure is not an invented agent refusal. Direct zero-tool executions record a real attempt. Stored evidence can be checked against its frozen result without rewriting historical verdicts. Database and result-contract guards reject an inconsistent PASS.
  - **Precedence:** A confirmed policy violation remains overall FAIL even when another error occurs. Otherwise an execution error or incomplete/conflicting harness evidence gives INDETERMINATE. Complete evidence without an execution error uses the audited task verdict. Task success alone never implies a safe or complete evaluation. Missing evidence gives unknown safety unless a surviving independent witness confirms a violation.
  - **Finite validation evidence:** `engine/tests/fixtures/harness_validation.v1.json` records the expected verdicts, measured results, source/reference hashes, and test evidence. All 44 matrix cases agree: 7 PASS, 15 FAIL, and 22 INDETERMINATE. False PASS, false FAIL, and false INDETERMINATE counts are each zero; failing case-ID lists are empty. All 13 critical safety cases are detected. All 22 evidence-failure cases have the expected classification. All 9 targeted semantic mutations are detected by the independent oracle or result invariants; no selected mutation survives. Eight event delivery variants produce identical deterministic results, and four later-error conditions preserve a witnessed safety failure. These are finite-set measurements, not universal evaluator accuracy.
  - **Regression and recovery evidence:** All 273 T01 audit tests pass. The new grader also passes all 32 reference paths and 39 alternatives. Historical dataset v2/v3/v4 executions retain evaluator v1/v2/v3, and terminal results are not reinterpreted. A real Temporal worker kill after commit recovers with one effect and attempts 1/2; witness-storage interruption rolls back the event and effect together. Provider interruptions and failure finalizers are also checked at their actual execution/persistence boundaries with offline or mocked providers.
  - **Final verification:** `make verify` passed with dedicated test PostgreSQL and Temporal services and `E2E_BASE_URL`, `JAEGER_QUERY_URL`, and `EVIDENCE_DIR` set: Go/frontend builds, Go tests, 511 Python tests (no skips), and 5 Playwright tests. This includes 152 T02 tests. Ruff checks on changed Python files, `go vet ./...`, schema drift, and lossless legacy migration checks passed. One existing Pydantic AI sampling warning remains. Evidence exports are in `/tmp/agentproof-t02-evidence`.
  - **Preservation and tracing:** All 6,604 original row fingerprints across 16 tables remain present and unchanged. Historical publications, graders, agent/model adapter, and the T01 corpus retain their original bytes. Trace `2951ca648370f59c2ec620f644b951ef` contains Go/Python/Temporal execution with dataset 5, scenario 4, evaluator 4, run/trial/attempt identifiers, and outcome dimensions. Its stored evidence digest verifies successfully.
  - **Remaining limits and next gate:** Witnesses assume trusted local database producers; they are not signatures against an administrator or an external-agent authentication mechanism. Free-form answer quality remains outside the typed protocol. Broader deadline/provider-generation attribution and deployment/package attestation remain T04/T05. No live provider requests, new providers, UI features, benchmark expansion, commits, pushes, deployments, or deletion of existing records were performed. T03 is the next eligible task; it and all later tasks remain unchecked.

- [x] **T03 — Audit broker authorization, fixture isolation, and evidence integrity.**
  - **Priority:** P0.
  - **Problem:** Run isolation does not define read confidentiality, validate every rejected attempt, or establish trusted evidence against future external clients.
  - **Required engineering work:** Resolve `get_order` ownership/disclosure policy from T01. Check approval authority, argument validation, deep-copy boundaries, transport replay, semantic operation identity, and terminal fences. Define which evidence the harness produces and which an agent may supply. Preserve atomic event/effect/response transactions and immutable historical records.
  - **Dependencies:** T01.
  - **Acceptance tests:** Cross-trial and cross-customer access tests; wrong-order IDs; mutable-context attacks; malformed/unknown tools; approval bypass; repeated operation keys; distinct legitimate intents; writes after cancellation or completion; tampered event/effect evidence. Rejected attempts must have bounded audit evidence without executing an effect.
  - **Required evidence:** Threat/invariant matrix mapped to tests and trusted producer identities; state/effect counts before and after each attempted violation.
  - **Completion condition:** No unresolved critical broker bypass or fixture leak in the approved scope. Read and write rules agree with scenario grading.
  - **Verified scope and read policy (2026-11-01):** The approved scope is the existing synthetic, single-fixture tool protocol. `get_order` permission grants access to that trial's fixture, including the cross-customer inspection in `cs-008`. The typed customer field is a business actor, not an authenticated principal. All mutation and approval tools require fixture ownership; required consent comes from the stored request. This agrees with all 32 audited references and 39 valid alternatives. Production customer confidentiality and hostile Python code in the harness process are outside this scope; T12/T15 remain pending.
  - **Confirmed corrections:** The broker copies authority from the stored trial and rejects substituted private inputs. Agent context and response aliases cannot change it. Tool input has type, size, depth, and schema bounds; invalid attempts retain sanitized digest evidence without effects. Evaluator v4 requires its witness even if a caller disables the optional flag. Before replay or dispatch, the broker checks journal completeness, receipt chains, effect payloads, state, and agreement between indexed identities and JSON identities. Conflicts stop as harness failures. Separate bounded platform rejection rows record replay mismatches, integrity failures, and terminal calls without rewriting the execution digest or verdict. Migration `010_broker_rejections.sql` adds this journal without updating existing rows.
  - **Required evidence delivered:** `engine/tests/fixtures/broker_security.v1.json` maps 17 invariants to trusted producers and executable tests. `engine/tests/test_broker_security.py` has 47 passing cases. With `EVIDENCE_DIR=/tmp/agentproof-t03-evidence`, the tests emitted 54 before/after artifacts under `t03/`, including state and event/effect/receipt counts. Rejected cross-trial, customer, approval, malformed-input, replay, terminal, and corruption attempts cause no additional effect. Two valid refund installments retain two operation keys and total 2500 cents. A failed witness transaction rolls back all writes; recovery commits exactly one simulated effect. This is not a claim of exactly-once activity execution.
  - **Observed verification:** `make verify` passed with dedicated PostgreSQL, Temporal, API, worker, and Jaeger configured: 558 Python tests, 5 Playwright tests, zero failures and zero skips. `go test -json ./...` confirmed 16 passing Go tests, zero failures/skips. Both application builds, `go vet ./...`, and `uvx ruff check engine/agentproof engine/tests scripts` passed. Real worker interruption/recovery and the T01/T02 suites ran. The existing mocked SDK sampling-parameter warning remains. Logs: `/tmp/agentproof-t03-verify-final.log` and `/tmp/agentproof-t03-go.jsonl`. The two added indexed-identity regressions failed before their correction and passed afterward. T02's corrupted-event regression now asserts earlier broker detection, evaluator finalization, INDETERMINATE with a receipt-integrity issue, and zero effects.
  - **Trace and preservation evidence:** Jaeger trace `78ebfb58d9764735c97992b647bfb565` contains API and worker spans for dataset v5, evaluator v4, trial `75211e30-2a5c-5102-9a9c-861ee9d9416f`, and matching run/scenario/attempt identifiers. The trial passed with one cancellation effect and a valid stored witness. Full trace evidence is in `/tmp/agentproof-t03-evidence/validated-trace.json`. After verification, all 9,648 original rows across 18 public tables retained their pre-task row fingerprints. All prior published assets, grader implementations, contract schemas, migration files, and T01/T02 oracle JSON bytes retained their hashes. Only new verification records and the new audit table were added to the development database. Existing failed evidence and frozen verdicts were preserved.
  - **Gate disposition:** No critical broker bypass or fixture leak remains in the tested scope. The broker and evidence witnesses trust local harness/database producers; they do not protect against an administrator who rewrites both evidence and witnesses. Finite regression coverage does not prove universal security. No provider requests, benchmark expansion, later-task implementation, commit, push, deployment, or deletion of existing records occurred. T04 is next in the execution order and remains unchecked.

- [x] **T04 — Make failure attribution and uncertainty consistent.**
  - **Priority:** P0.
  - **Problem:** A later provider error can mask an already observed forbidden effect. Some provider-generation errors and time limits lack precise attribution.
  - **Required engineering work:** Define assertion verdicts separately from execution conditions. Preserve irreversible proven agent violations even if a provider subsequently fails. Keep unknown completion INDETERMINATE. Distinguish injected simulator faults, provider rejection/authentication/rate limit, evaluator defects, infrastructure loss, cancellation, and agent budget misuse. Specify handling of provider-side rejected tool generations and absent evidence. Keep safe error codes and sanitized context; never persist secrets or unbounded error bodies.
  - **Dependencies:** T02, T03.
  - **Acceptance tests:** Duplicate-then-provider-error and forbidden-effect-then-provider-error remain agent failures. Incomplete work interrupted by a provider failure remains indeterminate when correctness cannot be decided. Test invalid arguments, harness-induced tool mismatch, malformed error bodies, missing usage, outer timeouts, queue/SDK-lock waits, cancellation, and database outages. Every terminal result satisfies schema and database constraints.
  - **Required evidence:** A failure precedence table, mocked reproductions, verdict and condition fields for each test, and preserved original failure references.
  - **Completion condition:** Every observed failure maps to documented rules. No known security or irreversible effect violation disappears from results or denominators after another failure.

  - **Verified disposition (2026-11-01):** T04 is complete for the existing local, typed evaluation protocol. The active dataset v5, evaluator v4, 32 audited cases, 39 alternatives, historical publications, grader implementations, and T01/T02/T03 oracle assets retain their bytes. No schema migration or historical-result rewrite was needed.
  - **Confirmed corrections:** Outer deadlines, SDK-gate waits, and late returns no longer fabricate an agent execution-limit violation in evaluator-v4 runs. They supply an infrastructure execution condition; known task state and proven policy violations remain independent. Explicit configured agent limits still stop execution. Replay mismatches now reach the non-retryable evaluator failure path. Provider `tool_use_failed` maps to `tool_generation_rejected`, without inventing a locally executed tool or a policy violation from untrusted generation text. Automatic rate-limit retry exhaustion remains a provider rate-limit failure. Excess provider-reported usage stops tool execution as `provider_budget_overrun`. Provider diagnostic codes use a finite allowlist. Activity attempt records and Temporal failures use fixed safe codes instead of arbitrary exception bodies.
  - **Failure precedence evidence:** `engine/tests/fixtures/failure_attribution.v1.json` contains 15 condition-to-test rules and preserves the original failure references. Confirmed policy violations remain overall FAIL and remain in the assessment denominators after provider, evaluator, infrastructure, or cancellation errors. Otherwise interrupted execution or incomplete/conflicting evidence is INDETERMINATE. Complete, uninterrupted evaluations use the audited task result. Provider rejection, missing usage, injected simulator faults, invalid arguments, budget limits, replay defects, and infrastructure loss have distinct documented dispositions. No rule treats a provider rejection as proof of a locally executed unsafe action.
  - **Executed regression evidence:** `engine/tests/test_failure_attribution.py` adds 18 passing cases for deadline/budget boundaries with and without duplicate effects, SDK-gate waiting, late completion, non-retryable replay classification, malformed/oversized provider diagnostics, generation rejection, activity-error redaction, and rate-limit retry exhaustion. The existing mocked provider overrun regression now requires the provider classification while retaining its hard stop. The focused adapter and attribution suite passed 40 tests. Existing T01/T02/T03 tests cover references, alternatives, unsafe-before-provider failure, missing usage, cancellation, database/witness write failure, terminal constraints, and real worker interruption/recovery.
  - **Final verification:** `make verify` passed with dedicated PostgreSQL and Temporal services, API/worker, `E2E_BASE_URL`, `JAEGER_QUERY_URL`, and `EVIDENCE_DIR` configured: 576 Python tests, 5 Playwright tests, both builds, zero failures and zero skips. `go test -json ./...` confirmed 16 passing tests. `uvx ruff check engine/agentproof engine/tests scripts` and `go vet ./...` passed. Logs: `/tmp/agentproof-t04-verify-complete.log`, `/tmp/agentproof-t04-go.jsonl`, and `/tmp/t04-focused-final.log`. The existing mocked Pydantic AI sampling warning and Node color-environment warnings remain. Docker verification, hosted CI, production load, and real-provider calls were not executed or claimed by this task.
  - **Preservation and trace evidence:** Before/after row fingerprints confirm all 11,667 pre-task rows across 19 public tables remain unchanged. Hash checks confirm 48 publication/schema/migration/grader/oracle files remain unchanged. Offline evaluation `f9ddffb5-f674-4f1e-8c02-9553ee7122b4`, trial `5eca6973-fd6d-529d-9e70-0caa05943408`, passed with one cancellation effect and a verified stored witness. Jaeger trace `b5708a61612742e53fa2efaf3f926924` links API/worker spans and matching run, trial, scenario, dataset, and attempt identifiers; evidence is `/tmp/agentproof-t04-evidence/validated-trace.json`.
  - **Limits and next task:** Deadline attribution is conservative: infrastructure interruption does not identify which queue, provider wait, or scheduling delay consumed time. Separate latency measurements and capacity guarantees remain later work. Exact causality of historical provider generation rejection remains T09; it is not inferred from the safe error code. Finite mocked coverage does not prove all possible provider behavior. T05 is next and remains unchecked. No live LLM requests, new providers, benchmark expansion, frontend redesign, commits, pushes, deployments, or historical-data deletion occurred.

- [x] **T05 — Bind results to reproducible implementation and asset references.**
  - **Priority:** P0.
  - **Problem:** Descriptor hashes and local files do not identify exact execution code, and the checkout has no versioned baseline.
  - **Required engineering work:** Capture source tree/build identity, dependency lock digests, evaluator and adapter implementation versions, safe effective model settings, and immutable data/tool references. Define export/replay validation when operator profiles are absent or changed. Distinguish requested settings from settings removed or transformed by the SDK. Prepare a reviewable baseline; commit/push only in a separately authorized task.
  - **Dependencies:** T01.
  - **Acceptance tests:** Reject altered assets or unsupported implementation references. Reproduce a saved offline result from a clean locked environment without previous chat or temporary files. Verify old v1 references and approved archived versions remain inspectable. Secret scans cover exports and build contexts.
  - **Required evidence:** A portable artifact manifest, source/build and lock digests, safe run exports, and clean-environment reproduction logs. Git/hosted evidence must identify an actual commit when available.
  - **Completion condition:** Every new scored result can be traced to exact supported execution artifacts. Historical limitations remain explicit; no old result is silently relabeled.

  - **Completion evidence:** Migrations 011–012 add immutable source snapshots and separate result bindings for v1 runs, benchmark trials, and benchmark evaluations. New trial results reference the execution-manifest SHA-256; parent aggregates and v1 results use immutable database sidecars without changing their JSON contracts. The manifest captures the uncommitted source bytes and inventory digest, lock digests, available Git identity, Python and Go runtimes, runner and simulator implementation digests, exact approved asset bytes, prompt and model references, effective settings, limits, and fault. The result sidecar and portable bundle bind result and recorded-evidence digests. Historical results without these records remain explicitly unverifiable; no old verdict was relabeled.
  - **Acceptance evidence:** An offline dataset-v5/evaluator-v4 `cs-016` run completed PASS with one cancellation effect. Both its trial and parent bindings verified. Its bounded JSON export verified and replayed PASS without a model profile or credential, including in a clean isolated copy after frozen Python and locked Node installs and Go module verification. That copy passed 321 offline benchmark, scenario-audit, and provenance tests. Tamper, missing source/lock/evaluator, unsafe path, duplicate entry, credential-canary, terminal-without-digest, and interrupted-persistence regressions pass. A synthetic key was absent from the final Docker worker image, build log, source snapshot, inventory, bundle, and verification logs. Jaeger contained 22 linked API/worker spans with matching run, scenario, trial, and activity-attempt IDs. The Docker development stack was healthy. `make verify` passed 606 Python, 16 Go, and 5 Playwright tests with no failures or skips; Ruff and `go vet` passed. A pre-T05 row-hash snapshot confirmed all 15,714 historical rows across 19 tables remained unchanged.
  - **Limits:** The checkout has no Git commit baseline, so manifests record a null commit and a dirty/uncommitted source snapshot. Clean replay proves deterministic evaluation from recorded evidence; it does not reproduce provider generation or establish byte-identical macOS and Linux builds. Pydantic AI still warns that GPT-OSS reasoning ignores temperature and top-p; effective settings omit those parameters. Node still warns about `NO_COLOR` with `FORCE_COLOR`. No live provider calls, commit, push, or deployment occurred. T06 remains unchecked.

- [x] **T06 — Audit metrics, denominators, and dashboard claims.**
  - **Priority:** P0, because misleading coverage invalidates later comparisons.
  - **Problem:** Conditional success, incomplete usage, and repeated-run filtering can look stronger than total attempted performance.
  - **Required engineering work:** Implement the definitions in section 9. Show planned/started/terminal/determinate coverage and each failure class. Separate policy-correct task outcome from business fulfillment and end-to-end valid completion. Preserve partial request usage as a labeled subtotal. Define estimated and reported cost-per-success independently. Audit latency populations, repeated-run denominators, bootstrap degeneracy, and comparison labels that still describe real-model runs as offline outcomes.
  - **Dependencies:** T04, T05.
  - **Acceptance tests:** Hand-calculated mixed PASS/FAIL/INDETERMINATE/missing cohorts; zero denominators; partial/cancelled runs; missing prices or token fields; one valid repeat plus one indeterminate repeat; all-success tiny samples; correlated repetitions; failed-call latency. UI denominators and comparison exclusions must match raw trial records.
  - **Required evidence:** Formula-to-fixture mappings, calibrated statistical simulations where needed, and browser assertions for counts, unavailable values, and caution labels.
  - **Completion condition:** No missing observation is represented as zero, no conditional metric is displayed as all-attempt success, and uncertainty claims match validated methods and sample support.

  - **Completion evidence (2026-11-06):** `engine/tests/test_metrics_audit.py` maps hand-calculated six-slot mixed cohorts, partial and cancelled trials, zero denominators, missing usage and price, failed-request latency, delivered recovery faults, incomplete repeats, and tiny correlated all-success samples to exact summary fields. New summaries expose planned/started/terminal/determinate and failure-class counts; conditional PASS and end-to-end completion use different stated denominators; business fulfillment excludes correct refusals. Known token and estimated/reported cost subtotals carry separate request coverage. Cost per PASS requires terminal planned slots and complete observed request costs. Rates and quantiles are descriptive; unvalidated bootstrap intervals are absent. Go comparison tests verify real-model labels and missing/indeterminate pair exclusions. Browser tests verify raw cohort denominators, unavailable historical cost coverage, partial subtotals, caution labels, and comparison exclusions.
  - **Verification and preservation:** `make verify` passed with 612 Python tests, 6 Playwright tests, and Go package tests; a noncached Go JSON run passed 17 tests. Ruff check and format check passed; `go vet ./...` passed. Worker interruption and recovery regressions passed. The isolated Docker API, worker, web, PostgreSQL, Temporal, and Jaeger services were healthy. A new offline dataset-v5/evaluator-v4 `cs-016` run `106529c8-2247-402a-aaad-524f9f550efb` passed with one cancellation effect; trial and parent provenance bindings verified, and its exported trial verified and replayed PASS without provider credentials. Jaeger trace `224da2e6c52ff7978c735182ade1874d` contained 22 linked API/worker spans with matching run, scenario, trial, dataset, evaluator, and attempt identifiers. All 15,743 baseline rows across 23 native database tables retained their row fingerprints; dataset v5, evaluator v4, and T01/T02 fixture hashes were unchanged.
  - **Limits:** No live provider calls or model-ranking claims were made. Provider-reported billing cost remains unavailable when the provider does not supply it. Hosted CI and the optional clean-container verification run were not executed; the latter image download was stopped before the test command. The existing Pydantic AI sampling warning and Node color-environment warnings remain. T07 and later tasks remain unchecked.

## 5. Benchmark quality and expansion

Expansion starts only after the P0 gate. A target such as the supplied draft's 80 cases
is not a completion criterion. Current contracts permit at most 50 cases per dataset.
Coverage and independent ground truth take precedence over volume.

- [x] **T07 — Define realistic coverage and author only justified new cases.**
  - **Priority:** P1.
  - **Problem:** Current single-order typed tasks provide limited realism, difficulty balance, and policy coverage.
  - **Required engineering work:** Build a coverage map of task actions, information needs, decision points, permissions, approvals, no-action outcomes, recovery, and difficulty. Add multi-step and multi-turn tasks, ambiguous requests that require clarification, and controlled adversarial tool/user content. Extend fixtures and interaction contracts only where the coverage gap requires it. Define untrusted content carriers and semantic effect identity before adding multiple legitimate effects.
  - **Dependencies:** T01 through T06; P0 gate G1.
  - **Acceptance tests:** Each new case covers an identified gap, has an independent valid reference path and negative control, and accepts documented alternate valid paths. Clarification or refusal must not be mislabeled failure. Test adversarial instructions without real services or real customer data.
  - **Required evidence:** Coverage matrix, duplicate/near-duplicate review, case rationales, independent labels, fixture and evaluator references, and deterministic oracle tests.
  - **Completion condition:** The approved coverage specification is met without superficial case inflation or unresolved oracle defects.
  - **Completion evidence (2026-11-06):** `engine/tests/fixtures/benchmark_coverage_t07.v1.json` is the approved-for-authoring, unpublished coverage specification. It maps nine dimensions to the 32 published cases and five confirmed gaps. Five distinct synthetic candidates address scripted clarification, two legitimate refund obligations, untrusted tool text, fake user authority, and a conditional action choice. Each records a rationale, nearest existing semantic cases, a differentiator, difficulty, permissions, protected authority, and lower-trust content. The fixture SHA-256 is `4ef0d205853a8168a291b33ec2b5b5089c6dfdcfac879ded883761fecefac552`; the draft oracle source SHA-256 is `5ed02facb6a1ae400e8651708816e627b411e2c54e09f5e65b752b3a24bf2af8`. Dataset-v5 and evaluator-v4 hashes are pinned in the fixture.
  - **Oracle and negative-control evidence:** Ten manually derived valid reference/alternative paths and nine adverse paths have declared labels, final-state fields, effect counts, and rule reasons. The separate deterministic authoring oracle agrees on all 19 paths. It accepts unanswered clarification and safe refusal, distinct refund obligations, harmless extra reads, and transport redelivery. It rejects guessed amounts, forged user replies, fake approval, wrong conditional branches, unrequested effects, repeated semantic obligations, reused decision keys, wrong-order calls, missing permission, and tool-limit overflow. `engine/tests/test_coverage_design.py` passed 30 focused tests. These are finite draft-oracle checks, not a measured model success rate or independent human sign-off.
  - **Verification and preservation:** `make verify` passed 642 Python tests and 6 Playwright tests with no failures or skips. A noncached Go JSON run passed 17 tests; Ruff check/format and `go vet ./...` passed. The API still exposes 32 active dataset-v5 cases and rejects `t07-001` with HTTP 400. All 15,743 pre-change rows across 23 native database tables retained their fingerprints, with no new native rows. Dataset v5, evaluator v4, and T01/T02 fixture bytes retained their SHA-256 values. No active evaluator, metric, API, dashboard, or historical verdict changed.
  - **Publication boundary:** The candidates remain outside scored runs. T08 must version and publish the fixture, interaction, tool, and evaluator contracts and assign split families before an agent can run these cases through the API. The authoring oracle assumes harness-produced user replies and broker-generated operation keys; T08 must enforce and attest those producer identities. No live provider call, commit, push, deployment, or deletion occurred. T08 and later tasks remain unchecked.

- [x] **T08 — Publish audited datasets and protect evaluation splits.**
  - **Priority:** P1.
  - **Problem:** Expansion, corrected cases, schema bounds, and held-out handling need an explicit publication process.
  - **Required engineering work:** Maintain development and held-out splits. Track related template families to prevent leakage across splits. Keep policy tuning separate from held-out evaluation. Publish immutable versions with case/evaluator/tool compatibility. Update Go creation/discovery, Python registry, and request schemas together when supporting versions beyond the current hardcoded v2 path. Decide whether growth uses several bounded datasets or a versioned schema change before crossing 50 cases. The source-visible held-out set must not be called sealed or secret.
  - **Dependencies:** T07, T05. Small correctness publications required by T01/T02 may occur earlier; they are not benchmark expansion.
  - **Acceptance tests:** Old manifests still inspect correctly; changed published bytes fail validation; unsupported versions and split selections are rejected; no private assertions, reviews, faults, or expected outputs reach the agent. Test the chosen dataset size boundary and stable case identities.
  - **Required evidence:** Publication manifest, split and family coverage, content hashes, review approvals, and compatibility tests.
  - **Completion condition:** Every active published case has an approved oracle and split disposition. No unsupported schema-bound change or hidden-test claim is present.
  - **Publication evidence (2026-11-07):** Published `customer-support` v6 as five source-visible development cases (`t07-001` through `t07-005`, scenario version 1). All five have distinct template families, prior-case family links, engineering-reviewed reference paths, and the exact T07 source hashes. The publication explicitly limits approval to automated engineering review for development use; no independent human sign-off or sealed-test claim exists. The held-out selection is rejected for v6. Existing v5 remains 32 cases with its development and held-out dispositions. V6 SHA-256: `68ef67a9ab8c4f144430a9a9395c94492b61028ee18dbb9e21fdbdf5df060a89`; evaluator-v5: `dde8534f9c52931ede7c8c5dac992717d27c37f6c85ee75c5d6411fb14960d75`; tool-v2: `d32cc2b247863812218d577e97d6385265168d01cdb374b112a672064bd83440`. V3 schemas match generated contracts.
  - **Oracle and trust evidence:** The normal execution path passed all 10 labeled valid references or alternatives and failed all nine negative controls for their required issue codes and expected effects. The broker authenticates simulated user replies, generates operation keys, records rejected calls, and distinguishes redelivery from new decisions. Tests cover source spoofing, untrusted tool text, ambiguous obligations, unavailable approval tools, replay mismatch, worker interruption before and after commit, incomplete evidence, trial isolation, immutable references, and portable replay. No private oracle or fixture field enters the reference agent input.
  - **Verification and preservation:** `make verify` passed 676 Python tests and seven Playwright tests with no failures or skips; its Go tests passed. A noncached Go run with the dedicated test database passed 23 tests with no skips. Ruff check/format, `go vet ./...`, and six schema drift checks passed. The rebuilt isolated Docker services are healthy. A v6 API→Temporal→worker run completed five PASS trials; Jaeger trace `a000dd9ed2482292476b02aca9bb85c5` has 35 Go/Python spans, one run ID, five scenario/trial IDs, attempt 1, dataset 6, and evaluator v5. All 15,743 pre-change native database row fingerprints across 23 tables remain present; new verification rows were added. Published v5 and evaluator-v4 hashes remain `59f6e65240fe035c0c7e4ee629f3d2d8082471cdeb3201c4e8d119724a7f654b` and `41b78fbd23305cea7c3714a782f95c615c97ed349ec1daae1d4605ac54cc635c`. One existing Pydantic AI warning says reasoning ignores `temperature` and `top_p` in a mocked Groq test. V6 supports the offline reference agent only; live-model scoring of these cases is unverified.

## 6. Real-model experiments and statistical evaluation

Use the existing adapter. Provider-specific compatibility is not a reason to build a
second evaluation engine. This plan does not authorize new live calls.

- [x] **T09 — Close compatibility and provider-error evidence gaps.**
  - **Priority:** P1.
  - **Problem:** The original 120B probe mismatch is fixed, but another 20B `tool_use_failed` error remains unresolved. A generic HTTP category is not an exact cause.
  - **Required engineering work:** Preserve the existing errors. Reconstruct safe request parameters from supported records. Check official current provider APIs, tool choices, schemas, reasoning behavior, SDK transformations, and usage fields. Capture bounded sanitized diagnostics. Reproduce defects with mocked responses and correct only confirmed causes. Do not treat all tool-generation rejections as either provider outages or agent policy failures without evidence.
  - **Dependencies:** T04, T05; G1 before new scored live experiments.
  - **Acceptance tests:** Probe input advertises only its offered tool; business history contains no probe artifacts; supported effective parameters reach the wire; unsupported settings fail clearly; authentication, 429, `Retry-After`, provider generation errors, interruption, and incomplete usage remain correctly classified.
  - **Required evidence:** Sanitized request/error fixture pairs, exact SDK/provider versions, regression tests, and cause/disposition for each retained error.
  - **Completion condition:** Every known error has a confirmed correction or an explicit unresolved limitation. No repeated request loop is used to force a passing result.
  - **Evidence and disposition (2026-11-07):** Read-only inspection found three retained failed model calls. The original 120B run `25757a0c-2992-46f6-b94b-114ffc04b00a` has only `unsupported_capability_or_configuration` at probe request 1; the separately reproduced probe prompt mismatch has a mocked correction and successful later smoke, but that historical row cannot prove its exact HTTP cause. The 20B run `00863338-04fa-4b30-b0f0-3951bcc0aadc` has the same generic category at request 2 without status or error code; exact cause remains unavailable. The 20B run `323c1df2-4926-434f-a588-60d6a79cad70` has HTTP 400 and allowlisted `tool_use_failed` at request 3 after two completed calls. Its saved row lacks the failed generation and actual wire request, so the exact rejected call or schema defect remains unresolved. All three original rows and verdicts are unchanged; no HTTP status alone was promoted into a historical cause.
  - **Compatibility and diagnostic evidence:** The installed stack is `pydantic-ai-slim` 1.107.9, `openai` 3.42.0, and `httpx` 0.28.1. The provider endpoint is Groq OpenAI-compatible `/openai/v1` with `openai/gpt-oss-20b` and `openai/gpt-oss-120b`; Groq's server build version is unavailable. Current [Groq tool-use guidance](https://console.groq.com/docs/tool-use/local-tool-calling) documents a 400 response with `failed_generation` for invalid generated tool calls; [reasoning guidance](https://console.groq.com/docs/reasoning) documents GPT-OSS reasoning parameters. The installed Pydantic AI adapter maps `max_tokens` to wire `max_completion_tokens` and drops `temperature`/`top_p` when reasoning is active. The adapter now omits those ineffective GPT-OSS defaults and rejects nondefault values before a call. It records bounded actual-wire parameter names, tool names/schema digest, allowlisted provider code, presence of failed generation, status, rate-limit headers, usage coverage, and SDK versions. It records no messages, raw provider generation, exception text, or credentials. The 120B probe and 20B rejection are synthetic sanitized request/error fixtures in `engine/tests/test_llm.py`; they do not assert the missing historical wire bytes.
  - **Verification:** `make verify` passed 679 Python tests and seven Playwright tests with no skips or Python warnings; a noncached database-backed Go run passed 23 tests. Ruff check/format and `go vet ./...` passed. Focused `test_llm.py` passed 25 tests after the final test assertion. Mocked tests cover isolated probe history, supported wire fields, rejected unsupported sampling, authentication, 429 and `Retry-After`, `tool_use_failed`, interruption, usage gaps, bounded retries, and safety failure precedence. The full suite includes the 19 T07 paths, broker, provenance, replay, and Temporal recovery checks. Jaeger trace `acb0ddc2dd111fbd3eab5e4d99129291` has 35 API/worker spans with the run and five scenario/trial IDs. All 16,308 pre-change native database row fingerprints across 23 tables remain present, with zero changed or new rows. Published dataset v5/v6, evaluator v4/v5, and tool-v2 hashes remain unchanged. Node emitted only a `NO_COLOR`/`FORCE_COLOR` test-runner warning. No live provider request was made; the unresolved historical 20B causes require new authorized evidence, not speculative retries.

- [x] **T10 — Predeclare matched experiments and statistical decisions.**
  - **Priority:** P1.
  - **Problem:** Current small samples cannot support a ranking; API reference compatibility does not enforce equal profile budgets.
  - **Required engineering work:** Freeze audited scenario selections, trial counts, fixtures, evaluator/tool versions, business prompts, effective settings, and equal resource budgets for Groq 20B/120B. Identify the intended estimand, minimum relevant effect, precision goal, allowed failure coverage, sampling order, paired analysis, and stopping rule before execution. Account for repeated trials within scenarios. Define when samples support only descriptive results.
  - **Dependencies:** T06, T09. Use approved existing cases or T08 publications; expansion is not mandatory for a narrow experiment.
  - **Acceptance tests:** Reject or clearly label mismatched budgets and contract references. Test missing/indeterminate pairs and different trial counts. Validate interval coverage on correlated and rare-event fixtures. Single-case and tiny all-success cohorts cannot imply population certainty. A fixed seed must not be described as making an external model deterministic.
  - **Required evidence:** A versioned experiment configuration and analysis specification, with a predeclared request/token/time/cost ceiling and no model-ranking conclusion before its decision gate.
  - **Completion condition:** The experiment is reviewable before spending. Its matching, exclusions, precision, and stop rules are mechanically checkable.
  - **Predeclared specification (2026-11-07):** `packages/contracts/experiments/groq-gpt-oss-matched.v1.json` (SHA-256 `39ed7f3fbad790db739cfc7f8e49854bcc5e7181c2950ac9105b0089f5132445`) pins approved dataset v5, evaluator v4, tool schema v1, development cases `cs-001` and `cs-016`, their scenario hashes, two independent trials per case and model, both Groq GPT-OSS profile byte hashes, one prompt hash/version, equal effective settings, and equal per-trial limits. Eight slots permit at most 48 model requests, 80 tool calls, 512,000 configured tokens, 40 active task seconds, 180 total wall seconds, and $0.80 estimated spending. This is a ceiling and a pre-spend design, not authorization or a provider bill. The local external profile bytes matched their pinned hashes; clean CI can check the committed asset but cannot attest private local profile bytes without those files.
  - **Analysis gate:** The estimand is the mean paired PASS difference across the two selected scenario clusters; two repeats stay within each cluster. The minimum relevant difference is 0.2, target interval half-width is 0.1, 95% coverage is specified, and every planned pair must be determinate for a ranking decision. Sampling order alternates model order, and no slot is rerun to select a better score. Missing and indeterminate pairs stay in planned denominators but do not become PASS. A Hoeffding cluster interval and the decision gate are implemented in `engine/agentproof/benchmark/experiment.py`. This two-scenario pilot is descriptive: even all-success data yield a broad interval and cannot support a model ranking. Synthetic tests with perfectly correlated repeats covered rare-event prevalence 0.02 and prevalence 0.3 in 1,000 of 1,000 fixed-seed simulations each; this validates those fixtures, not external model determinism or general coverage. Unsafe outcomes and unknown safety coverage remain separate from task success.
  - **Matching and verification:** The Go comparison adds `experiment_eligible` and explicit mismatch reasons for prompts, splits, repeats, effective settings, and budgets, while keeping the existing descriptive pair output. Incompatible dataset/evaluator/tool references remain rejected. `make verify` passed 684 Python tests and seven Playwright tests without failures or skips. A noncached database-backed Go run passed 24 tests; Ruff check/format and `go vet ./...` passed. The full suite includes broker, provenance, replay, workflow recovery, and all 19 T07 labeled paths. A fresh Jaeger trace `a890e484e7305de95bc6a28e39aeddc0` has 35 API/worker spans with one run and five scenario/trial IDs. All 16,316 pre-change native database row fingerprints across 23 tables remain unchanged, with no new rows. Dataset v5/v6, evaluator v4/v5, and tool-v2 hashes remain unchanged. Node reported only a `NO_COLOR`/`FORCE_COLOR` warning. No live model request was made. T11 must verify current pricing, quotas, profile availability, and explicit spending authorization before execution.

- [ ] **T11 — Execute bounded experiments and preserve every trial.**
  - **Priority:** P1.
  - **Problem:** Quota pacing, partial accounting, and selective reruns can distort experiments or exceed authorized scope.
  - **Required engineering work:** In a later authorized live task, verify credential availability without exposure, provider model availability, current pricing, and account limits. Run one smoke trial per model first. Expand only under T10's budget. Use explicit quota-aware scheduling; do not rely on an undocumented temporary worker. Retain all planned slots, attempts, errors, incomplete usage, and final states. Reset fixtures between independent trials and replay journals on platform recovery.
  - **Dependencies:** T10 and explicit bounded live-execution authorization. Do not enable paid billing or account upgrades.
  - **Acceptance tests:** Mocked CI verifies hard request/tool/token/time limits, conservative spending reservations, missing usage, quota headers, safe cancellation, and no automatic uncertain request reissue. Live smoke verifies real tool use, trusted assertions, usage, and trace correlation before a larger predeclared batch. Rate-limit and budget stops must remain visible.
  - **Required evidence:** Immutable manifests, all trial slots, state/effect assertions, per-request usage/latency, pricing source/check date, safe provider errors, traces, and full cohort accounting.
  - **Completion condition:** The bounded batch stops at its declared rule. Every slot has a result or explicit missing/pending reason. Claims remain descriptive or inconclusive unless T10's statistical gate is met.

## 7. Independent external-agent integration

Prove one independent implementation before considering a general integration marketplace.
Do not upload or execute arbitrary user code inside the existing worker.

- [x] **T12 — Define the minimum independent-agent contract.**
  - **Priority:** P1.
  - **Problem:** Current reference policies and model configurations run inside AgentProof's own loop.
  - **Required engineering work:** Define a versioned public task envelope, permitted tool discovery/call/result protocol, run/trial correlation, agent final outcome, deadlines, cancellation, resume behavior, and error taxonomy. Separate agent-owned internals from harness-owned authorization and grading. Select the smallest transport in an ADR, without adding an agent framework. Define scoped disposable credentials, endpoint allowlists, replay identity, and expiry before remote use.
  - **Dependencies:** T02 through T06; G1. Minimum transport security is required here and cannot wait for P2.
  - **Acceptance tests:** Contract fixtures for success, correct refusal, unknown tools, malformed messages, unauthorized calls, deadline expiry, cancellation, replay mismatch, unavailable agent, and missing internal telemetry. No private assertions or direct database capabilities are exposed. Unknown external state must not trigger an unauthorized repeated action.
  - **Required evidence:** Versioned schemas (`packages/contracts/v4/`), protocol and HTTP conformance tests (`engine/tests/test_agent_protocol.py`, `engine/tests/test_external_http_integration.py`), the trust-boundary map in `README.md`, and ADR-08 below.
  - **Verified evidence (2026-11-09):** The independent HTTP client completed authorized, refused, unsafe, expired, cancelled, and timeout-after-commit trials through Go, Temporal, the Python broker, and the evaluator. The test verified replay rejection, evidence persistence, offline replay, and Go/Python Jaeger spans. `make verify` passed.
  - **Completion condition:** Another engineer can implement an independent client without importing AgentProof's reference policy or accessing its private oracle.

- [ ] **T13 — Test a genuinely independent agent end to end.**
  - **Priority:** P1.
  - **Problem:** New model IDs do not demonstrate external-agent interoperability or useful failure detection.
  - **Required engineering work:** Integrate at least one separately implemented agent with its own decision/tool loop through T12. Use simulated capabilities and disposable test credentials. Reuse the evaluator without per-agent core changes. Record which internal signals the agent cannot provide; internal chain-of-thought is not required evidence.
  - **Dependencies:** T12 and approved test cases from G1 or T08.
  - **Acceptance tests:** A valid task passes; an intentional forbidden action or duplicate fails; a timeout-after-commit case is evaluated from final state; independent trials remain isolated. Disconnect/cancellation and missing optional telemetry cannot fabricate a PASS or unfair policy FAIL.
  - **Required evidence:** Independent implementation identity, conformance results, manifests, trusted state/effect records, failed-run evidence, and stated telemetry limitations.
  - **Completion condition:** At least one agent built independently of the reference implementation completes a scored test, and AgentProof explains an intentional failure without agent-specific evaluator changes.

- [ ] **T14 — Validate developer usefulness with an independent user.**
  - **Priority:** P1.
  - **Problem:** Internal engineering tests do not establish that another developer can use or interpret the product.
  - **Required engineering work:** Arrange an authorized pilot with an independent developer. Have them set up the documented environment, connect an agent, run a test, and inspect a pass and a meaningful failure. Capture setup friction and missing evidence in issues or Codex chat. Do not send invitations or messages without authorization.
  - **Dependencies:** T13, T06, and participant consent/availability.
  - **Acceptance tests:** The participant can complete the workflow without hidden chat history, production credentials, or evaluator edits. Their explanation of the failure matches trusted evidence.
  - **Required evidence:** Authorized participant feedback, reproducible steps, run IDs, and resolutions or explicit limits for material blockers.
  - **Completion condition:** One independent developer demonstrates the intended workflow and understands its result. If no participant is available, this gate remains open; internal testing is not a substitute.

## 8. Security and production readiness

The current loopback development system is not approved for public or multi-tenant use.
Keep production engineering proportional to an explicit deployment scope.

- [ ] **T15 — Define and verify the production security boundary.**
  - **Priority:** P2.
  - **Problem:** The API has no authentication or tenant isolation, and trusted operator profiles are not an untrusted endpoint sandbox.
  - **Required engineering work:** Select a documented single-tenant or multi-tenant deployment scope. Add authentication and authorization for that scope before exposure. Verify read/write boundaries, external endpoint allowlists, SSRF defenses, egress, credential scope, secret rotation, SQL roles, evidence integrity, and sensitive-data handling. Minimize stored provider reasoning/content. Do not require a container/code-execution platform unless the approved scope requires it.
  - **Dependencies:** T03, T12 and a deployment-scope ADR. External protocol minimum safeguards already belong to T12.
  - **Acceptance tests:** Unauthorized API access, cross-principal reads/writes, stale tokens, forbidden egress, malicious callback addresses, prompt/tool injection, evidence tampering, and secret leakage fail safely. Sensitive content and credentials must not appear in traces, browser payloads, exports, or CI logs. Use only isolated test services.
  - **Required evidence:** Threat model, scoped access matrix, negative tests, dependency/security scan results, and dispositions for findings.
  - **Completion condition:** No unresolved critical boundary bypass remains for the declared deployment. A single-tenant release must not claim multi-tenant isolation.

- [ ] **T16 — Verify concurrent execution, budgets, and recovery.**
  - **Priority:** P2.
  - **Problem:** Local sequential runs do not establish safe capacity, shared quota control, or recovery under simultaneous faults.
  - **Required engineering work:** Declare a reference workload and bounded concurrency envelope. Exercise API dispatch, workflow queues, the SDK request gate, database locks/connections, shared provider quotas, aggregate budgets, and storage growth. Test worker, API, PostgreSQL, and Temporal interruption before/after commits. Distinguish queue waiting from agent time limits. Preserve uncertain model-call accounting and terminal-write fencing.
  - **Dependencies:** T04, T05, T15.
  - **Acceptance tests:** Concurrent trials never share state or exceed authorized aggregate limits. Retried platform operations do not add unintended effects. Late activities cannot alter terminal results. Cancellation preserves completed trials. Outages produce correct pending/terminal states and classifications. Perform restoration only in dedicated test environments.
  - **Required evidence:** Workload definition, observed throughput/latency/resource distributions, queue delays, quota observations, interruption histories, final database assertions, and known capacity limits.
  - **Completion condition:** Measured behavior meets the predeclared workload criteria without unexplained state loss, duplicate effects, or false agent failures caused by platform waiting. No exactly-once external execution claim is made.

- [ ] **T17 — Establish an authorized Git baseline and verify hosted CI.**
  - **Priority:** P2 release gate; baseline provenance work begins in T05.
  - **Problem:** Local logs and a CI YAML file do not prove a hosted run or a recoverable committed source baseline.
  - **Required engineering work:** After explicit authorization, create/review the source baseline and push it to the intended remote. Verify GitHub Actions against that exact commit. Keep ordinary CI fully mocked and credential-free. Confirm locks, schema generation checks, browser builds, service dependencies, and artifact handling in a clean environment.
  - **Dependencies:** T05, T06 and explicit commit/push authorization. This plan does not authorize either operation.
  - **Acceptance tests:** Existing plus new correctness, contract, property, integration, recovery, and browser checks run without service-test skips. Missing test-service variables must fail the full verification command. Secret scans pass. Hosted artifacts identify the exact commit and include actual job outcomes.
  - **Required evidence:** Commit/tree identity, hosted job URL and status, test counts, dependency locks, build identifiers, and retained sanitized artifacts.
  - **Completion condition:** A real hosted run passes for the approved baseline. Local execution of equivalent commands is labeled local and cannot close this gate.

- [ ] **T18 — Set operational limits, observability, retention, and restore procedures.**
  - **Priority:** P2.
  - **Problem:** Memory-only telemetry and unbounded historical storage do not provide production operations or recovery evidence.
  - **Required engineering work:** Use T16 measurements to select SLOs, alert thresholds, concurrency/storage ceilings, and backpressure. Define trace retention/export, evidence access, backups, restoration, and outage procedures. Separate provider failures from agent correctness alerts. Avoid run/trial identifiers in metric labels. Define an owner-approved retention policy without deleting existing evidence in this task or by default.
  - **Dependencies:** T15, T16, T17.
  - **Acceptance tests:** Trace export and parent correlation survive the declared operating conditions. Test exporter failure, queue backlog, database saturation, backup restoration into an isolated copy, and reconciliation of state/effects/history. Alerts trigger on controlled threshold breaches. Retention/access rules preserve required failed-run evidence.
  - **Required evidence:** Accepted SLO definitions with measured support, alert tests, restore logs and checksums, capacity limits, and approved operating procedures.
  - **Completion condition:** Operators can detect, explain, and recover from the declared failure cases. SLOs are supported by measurements rather than invented targets or metrics.

## 9. Metrics and acceptance criteria

Implement and verify these definitions through T06 and T10. They are target definitions,
not claims that every corresponding field already exists.

| Metric or acceptance area | Required definition and evidence |
| --- | --- |
| Cohort accounting | State planned slots, started trials, terminal trials, PASS, FAIL, INDETERMINATE, cancelled, and missing/pending counts. Track provider attempts separately from independent trials |
| Conditional task success | PASS / (PASS + FAIL), with the exact determinate denominator and coverage beside it |
| End-to-end valid completion | PASS / predeclared planned slots, with missing and execution-condition breakdowns. Also show started-trial completion where useful; do not silently change the population |
| Business fulfillment | Achieved authorized business goal / eligible fulfillment tasks. Correct refusal can be policy-correct PASS without being a completed business operation |
| Unsafe actions | Record unauthorized/forbidden attempts and effects, plus trials containing them. Preserve proven violations on indeterminate executions. Separate agent attempts rejected at the provider from locally dispatched tools |
| Recovery success | Correct state and effect invariants / trials with a defined recovery opportunity. Distinguish a fault not delivered from an agent failing after delivery |
| Duplicate side effects | Repeated execution of the same semantic obligation, distinguished from duplicate transport delivery and separate legitimate obligations |
| Tool-call validity | Valid attempted arguments / observable attempted calls, with rejected, unobserved, and provider-rejected-call coverage |
| Repeated-run reliability | Define the scenario/trial eligibility rule before execution. Show planned repeats, determinate repeats, complete-repeat scenario count, and indeterminate exclusions |
| Latency | Separate provider-request, active task, queue/pacing, and whole-evaluation durations. Report populations, missing/censored durations, sample counts, p50/p95, and their descriptive limits |
| Tokens | Retain available input/output usage per request, including failures. Show known subtotals and coverage. An absent usage field is unavailable, not zero |
| Cost | Label estimated versus provider-reported cost. Pin pricing source/date and assumptions. A partially observed subtotal is not a total invoice or a guaranteed spending cap |
| Cost per success | Define estimated and reported variants separately. Include relevant failed-attempt costs and state coverage; do not produce a complete-cost number when potentially billed calls are unknown |
| Uncertainty | Use validated methods that respect scenario clustering, repeated trials, pairing, and rare unsafe events. Label tiny samples descriptive. Do not interpret a degenerate bootstrap interval as certainty |
| Comparison | Match dataset, fixture, evaluator, tool schema, business prompt policy, and execution limits. Show every pair and exclusion. Record model changes intentionally, including effective generation settings |
| Oracle quality | All approved positive/alternate and adversarial negative controls have expected verdicts. No unexplained critical false pass/failure remains in the validation corpus |
| Evidence coverage | Required final state, relevant intermediate effects, authorization, references, and correlations exist for every scored claim. Missing evidence cannot be silently treated as a successful check |

The intended success-rate precision, minimum relevant difference, tolerated execution-failure
coverage, workload envelope, and operational SLO values are not decided yet.
T10 and T16/T18 must set them before the corresponding experiment or load test.
If cost or quota prevents adequate evidence, report an inconclusive result and stop.
Do not create a ranking requirement that can only be met by unrestricted provider spending.

## 10. Dependency order and execution gates

| Gate | Required order and evidence | What it permits |
| --- | --- | --- |
| G0 — Plan established | This file is validated against current source and available evidence | Start T01 in a later implementation task |
| G1 — Evaluation correctness | T01, then T02/T03/T05; T04 after T02/T03; T06 after T04/T05 | Approved-case benchmark work, bounded experiment design, and independent-agent protocol work |
| G2 — Benchmark and experiment readiness | T07 then T08 for expansion; T09 then T10 for model studies, using G1-approved cases | T11 only after explicit bounded live authorization; no case-volume prerequisite for a narrow study |
| G3 — Independent-agent usefulness | T12, then T13, then authorized T14 | Claims about independent-agent compatibility and pilot usefulness, limited to tested scope |
| G4 — Production evidence | T15, T16, T17, then T18; all earlier trust gates required for release | Production-readiness review only, not deployment authorization |
| G5 — Completion review | T19 verifies all prerequisites and open-risk dispositions | A scoped completion claim; deployment still needs authorization |

T07/T08, T09/T10, and T12 can progress in parallel after their stated prerequisites.
This is scheduling guidance, not authorization to spawn agents, contact users, or spend.
Hosted CI preparation may start earlier, but its release gate needs the corrected baseline.
No dependency permits grading against an unaudited oracle.

### Standard verification for later implementation tasks

Use [README.md](README.md) and [Makefile](Makefile) for startup and commands.
The native development layout uses web `5173`, API `8080`, PostgreSQL `55432`, and Temporal `7233`.
The isolated Docker layout in `infra/verification.env` uses `5174`, `8081`, `55433`, and `7234`.
Local Jaeger for the isolated layout is on `16687`, with OTLP HTTP on `4319`.
Confirm actual endpoints before testing; do not assume running services are unrelated or disposable.

For an already running dedicated native test environment, the full command is:

```sh
TEST_DATABASE_URL=postgresql://agentproof@127.0.0.1:55432/agentproof_test \
TEST_TEMPORAL_ADDRESS=127.0.0.1:7233 \
E2E_BASE_URL=http://127.0.0.1:5173 \
JAEGER_QUERY_URL=http://127.0.0.1:16687 \
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319 \
EVIDENCE_DIR=/tmp/agentproof-verification \
make verify
```

This command builds applications and creates/removes its own dedicated test fixtures.
It was not executed for this planning task. Do not run it against application or production databases.
Use `go vet ./...`, `uvx ruff check engine/agentproof engine/tests scripts`, and schema drift checks as applicable.
Use the existing Docker verification path for clean-environment checks.
Service-dependent skips must be reported as skips, not passing integrations.
Do not run live providers in routine CI. Changes need the tests relevant to their actual risk;
do not duplicate implementation logic in low-value tests.

### Conflicts and unresolved decisions

- Immutable publication versus oracle correction: publish a new supported version; preserve old bytes and results. Quarantine through approved selection/publication rules, not historical deletion.
- Valid alternatives versus security: permit alternatives only after T01 defines the invariant. Duplicate irreversible effects do not become acceptable merely because final state matches.
- Error isolation versus known violations: an execution error does not erase a proven violation; an unfinished ordinary goal does not automatically become an agent failure.
- All-32 audit versus held-out tuning: authorized reviewers inspect ground truth; agent development does not use held-out answers. A source-visible split is not a sealed test bank.
- Scenario growth versus the 50-case contract: choose a versioned publication strategy before crossing the bound. No automatic target of 80 cases is adopted.
- Free quotas versus statistical power: predeclare a bounded study. Insufficient evidence ends in an inconclusive result, not retries until success.
- External-agent usefulness versus production hardening: minimum capability/transport security precedes remote tests; full production readiness is a later gate.
- Git provenance versus authorization: preserve hashes now. Do not commit or push simply because the plan identifies a missing baseline.
- Preservation versus future retention: define and approve a retention policy separately. Current records and failed evidence are not cleanup targets.
- Production scope and measurable SLOs remain owner decisions, informed by T15/T16/T18 evidence. They are not assumed in this plan.

## 11. Non-goals and scope restrictions

- Maintain React/Vite/TypeScript, Go/Chi, Python/Pydantic AI, Temporal, PostgreSQL, and OpenTelemetry boundaries.
- Preserve the existing frontend design, working offline controls, v1 contracts, histories, and failed-run evidence.
- No Kubernetes, Kafka, billing product, account upgrade, model gateway, or new agent framework.
- No arbitrary external-agent uploads or untrusted code execution in the current worker.
- No production credentials, production order mutations, or personal login tokens as API keys.
- No blanket live-provider authorization. Use explicit opt-in and bounded authorized experiments; disable calls afterward.
- No LLM judge may override deterministic security or state violations. A future subjective rubric would require a separate justified ADR and human calibration; it is not required for the current core.
- No hidden-test secrecy, exactly-once execution, model superiority, capacity, or production-readiness claims without scoped evidence.
- No new progress reports or duplicated plans. Do not rewrite historical implementation reports to change past observations.
- No commit, push, deployment, destructive migration, data deletion, or external messaging without the applicable authorization.
- This planning task stops after creating and validating this file. It authorizes no backlog implementation.

## 12. Architecture decision log

Keep decisions in this section. Add date, evidence, consequences, and superseded decision references when changing one.

| ID | Date | Status | Decision and consequence |
| --- | --- | --- | --- |
| ADR-01 | 2026-11-01 | Accepted constraint | Preserve the current monorepo architecture and original v1 path. Fix verified defects within existing boundaries |
| ADR-02 | 2026-11-01 | Accepted constraint | Evaluate trusted final state and relevant intermediate effects outside the agent. Do not grade from agent success claims |
| ADR-03 | 2026-11-01 | Accepted constraint | Preserve immutable data/evaluator references and failed records. Correct semantics through supported new versions |
| ADR-04 | 2026-11-01 | Accepted constraint | Keep execution conditions distinct from assertion verdicts. Unknown completion is not a fabricated PASS or policy FAIL |
| ADR-05 | 2026-11-01 | Accepted constraint | Use the existing OpenAI-compatible adapter for Groq. Keep model calls opt-in and CI mocked. Do not repeat trials merely to obtain success |
| ADR-06 | 2026-11-01 | Accepted constraint | T01–T06 precede expansion and ranking claims. Case count is not benchmark quality |
| ADR-07 | 2026-11-01 | Accepted constraint | One root PLAN.md is authoritative. The downloaded draft is input; historical reports are evidence claims, not implementation truth |
| ADR-08 | 2026-11-09 | Accepted for T12 | Use authenticated HTTP/JSON v1 on the existing loopback Go API. The API binds each request to a trial-scoped credential and forwards calls as Temporal Updates to the existing Python broker. Use stable per-trial operation IDs and bounded session expiry. This keeps the external client off Temporal, avoids a second broker, and leaves fixture access and grading in trusted code. Verified by `engine/tests/test_external_http_integration.py`; do not expose the local API publicly |
| ADR-09 | 2026-11-07 | Accepted for T08 | Publish separate immutable, bounded dataset versions; each version has at most 50 cases. V6 uses schema v3 for new interaction behavior. Do not silently merge versions or expand a version past its bound |
| ADR-10 | 2026-11-07 | Accepted for T10 | Use a pinned, eight-slot descriptive Groq pilot. Analyze paired differences by scenario cluster; require complete determinate pairs and a conservative 95% interval that meets the predeclared effect and precision gates before any ranking. Retain missing slots and stop at declared ceilings |
| ADR-11 | 2026-11-01 | Pending T15/T18 | Select deployment scope, access model, retention, telemetry persistence, restore goals, and measured SLOs before release |

Accepted constraints are planning decisions. They do not mark implementation tasks complete.

## 13. Final project completion criteria

- [ ] **T19 — Complete the evidence-based project review.**
  - **Priority:** P2 final gate.
  - **Problem:** A working demo, passing test count, or small model comparison cannot alone establish a trustworthy finished product.
  - **Required engineering work:** Review every prior task, approval, artifact reference, and unresolved risk against the declared release scope. Confirm that another engineer can reproduce the supported workflow from the repository and documented configuration. Record limitations and the approved claim boundary in this plan; report execution outcomes in Codex chat.
  - **Dependencies:** T01 through T18 and their authorization gates. Any reduced release scope requires an explicit owner decision; it must not silently mark unmet criteria complete.
  - **Acceptance tests:** An independent agent runs approved scenarios through the normal interface; valid alternatives and deliberate security violations receive correct results; repeated trials stay isolated; interrupted execution preserves evidence; dashboard counts match records; clean local and actual hosted checks pass; scoped security, capacity, and restore tests meet their accepted criteria.
  - **Required evidence:** Reviewed case dispositions, evaluator-validation corpus, reproducible run/build references, matched experiment accounting, independent-agent/pilot evidence, hosted CI URLs, security tests, operational measurements, and retained failure examples.
  - **Completion condition:** All criteria below have evidence and no undisclosed blocking exception. An inconclusive model comparison is acceptable; unsupported claims are not.

Release criteria:

1. All original 32 cases have reviewed dispositions, with an approved core and no silently scored quarantined case.
2. Approved evaluator controls show no unexplained critical false pass or false failure. Proven forbidden effects survive later execution errors.
3. Benchmark expansion meets its justified coverage specification and publication/split rules.
4. Real-model experiments preserve every planned slot and explain denominators, unknown data, and limits. No ranking exceeds the evidence.
5. At least one independently implemented agent and one authorized independent developer complete the intended workflow.
6. Existing v1 behavior, records, contracts, and historical failures remain preserved under supported upgrades.
7. Source, dependencies, data, tools, evaluators, and model settings have reproducible references. Hosted CI passes against the actual approved commit.
8. The declared deployment has verified access controls, isolation, budgets, concurrent recovery, trace evidence, retention, and restore procedures.
9. The README supports setup and use without chat history. This PLAN.md remains the sole development plan.
10. No remaining critical correctness or security issue is hidden. Any explicitly accepted lower-priority limit has an owner, scope, and evidence-based reason.

Until these gates close, describe AgentProof as the implemented local evaluation system
with the specific verified capabilities and limitations above, not as a completed production benchmark platform.

## 14. Release-readiness correction — 2026-11-07

This review changes priorities and release gates, not the meaning or acceptance criteria of
T11–T19. It supersedes the old Git snapshot in section 2 and the **ordering** in G4.
T01–T10 remain checked with their recorded, limited evidence. T11–T19 remain open.
No historical asset, verdict, database record, or task disposition was changed here.

### Verified state and claim boundary

- Current source has React/Vite, Go/Chi, Python/Pydantic AI, Temporal, PostgreSQL,
  brokered simulated tools, versioned evaluation assets, portable offline replay,
  OpenTelemetry, and a local Jaeger service. The 14 migrations include immutable
  evidence and provenance guards. Dataset v5 has 32 audited cases; v6 has five
  source-visible interaction cases. V6 supports the offline reference agent only.
- T01–T10 are complete for their stated **synthetic and local** scopes. The code and
  checked-in oracle fixtures support those dispositions. Independent human labels,
  external-agent interoperability, population-level model quality, and production
  safety do not follow from those task checkmarks.
- Git `main` and `origin/main` both point to
  `66adfbfa66031c3634010614cc102da67c0131fe` (20 commits, clean checkout before
  this plan edit). The hosted [Verify run](https://github.com/vivekvx/AgentProof/actions/runs/42395114096)
  for that SHA completed successfully: Docker startup, the verification container,
  684 Python tests, and seven Playwright tests passed. Its one test job succeeded and
  uploaded a `verification-evidence` artifact. These counts come from the hosted log.
  The prior local full verification also passed; neither run tests an external agent.
  CI does not run Ruff or `go vet` as separate gates and its uploaded artifact is not
  a portable release report. T17 therefore stays open for evidence and gate review.
- The public API has no authentication or tenant boundary; Compose binds its ports to
  loopback. The only agent loops are built into this repository. Groq model calls are
  opt-in, and the T10 eight-slot study is a predeclared design, not a completed run.
  Jaeger uses memory storage. There is no basis for a public-service, enterprise,
  universal-safety, response-quality, or model-superiority claim.

### Architecture triage

| Disposition | Finding and release action |
| --- | --- |
| KEEP | Keep the React/Go/Python/Temporal/PostgreSQL boundaries, trusted broker, state/effect assertions, immutable asset versions, and offline replay. They support the tested execution path. Keep OpenTelemetry correlation for evidence, with a declared retention limit. |
| FREEZE | The v1 path and older dataset/evaluator implementations overlap newer paths but support historical manifests. Freeze their behavior; remove no compatibility code without a migration and replay proof. Freeze dashboard expansion and benchmark case growth until an external use case shows a gap. |
| FIX | API access lacks a boundary for any exposed deployment (T15). The model adapter's process-wide SDK gate serializes calls and can consume task time while waiting; measure and bound this under concurrent load (T16). Per-trial limits do not prove a shared provider quota or spending ceiling (T11/T16). CI lacks separate Ruff/`go vet` gates and a reviewed portable result summary (T17). |
| DEFER | A generic agent framework or model gateway, more providers, subjective response judging, more cases, a larger dashboard, and durable tracing infrastructure have no first-release justification. Revisit only with a measured coverage, user, or operating need. |
| REMOVE | No code or dependency is proven unused or harmful enough to remove in this review. Review a candidate and its historical references before any later deletion. |

The two broker/evaluator paths are versioned behavior, not evidence of a safe
deduplication opportunity. Go request validation and Python broker validation protect
different trust boundaries. No framework replacement is proposed.

### Missing validation and release disposition

| Product question | Current evidence or gap | Release disposition |
| --- | --- | --- |
| Independent agent | Only in-project policies and model loop exist; no external protocol. | BLOCK engineering release: T12/T13. |
| Trustworthy external tool and state evidence | Local broker records atomic events/effects and final state. External identity, transport replay, and agent-supplied evidence boundaries are untested. | BLOCK engineering release: T12/T13; T15 before exposure. |
| Genuine defect detection | Negative controls are scripted. No separately built agent has produced an independently reproduced useful finding. | BLOCK externally validated release: T13/T14/T19. An intentional defect suffices only for engineering conformance. |
| False alarms and missed violations | T02 has a finite mutation/counterexample corpus; no independent-agent disagreement review exists. | BLOCK broad evaluator-validity claims. T13 negative controls and T14 adjudicated disputes are required. |
| Response quality | Typed resolutions and state effects are scored; free-form answer usefulness is not. | DEFER for a tool/state/safety-only first release. It blocks a general customer-support-quality claim. |
| Controlled experiments and budgets | T10 pins eight slots; T11 live execution and shared quota behavior remain open. | BLOCK a real-model comparison claim: T11, then T16 aggregate-limit tests. No live request without explicit authorization. |
| Credentials and customer data | Synthetic local fixtures are bounded; the API has no authentication or tenant isolation. | BLOCK any exposed deployment: T15. First release must state its access and data limits. |
| Concurrent failure recovery | Sequential interruption cases pass; simultaneous worker, service, queue, and quota behavior is unmeasured. | BLOCK engineering release for the declared workload: T16/T18. |
| Clean hosted CI | One hosted run passed at the pushed SHA, with 684 Python and seven browser tests. Static gates and portable artifact review remain open. | T17 closes before an engineering release. |
| Reproducible report | T05 exports and replays offline evidence. A third party has not reproduced a current external-agent result from a clean checkout. | BLOCK engineering release: T13/T17/T19. |

### Corrected T11–T19 order and outcome mapping

The original IDs and acceptance criteria above remain authoritative. This order
reduces waiting and avoids spending on a model study before the product can test an
independent agent. A task shown in parallel still needs its own dependencies.

| Order | Original task → release outcome | Why now; prerequisite or gate |
| --- | --- | --- |
| 1 | **T17 → D**, finish hosted CI baseline | The push and first hosted run are verified. Review artifact contents, test-service skip behavior, static checks, and exact commit evidence. T05/T06 and prior push authorization are satisfied. No new push is authorized here. |
| 2 | **T12 → B/D**, minimum external-agent contract | Specify one small transport, disposable identity, allowlist, cancellation, and replay boundary before integrating another agent. T02–T06 are complete. |
| 3 | **T13 → B/C**, independent-agent conformance | Run a separately built loop against approved cases, valid alternatives, forbidden effects, and timeout-after-commit. Record false alarms and missing evidence. Depends on T12. A remote exposure also needs T15 first. |
| Parallel after T10 | **T11 → A**, bounded live-model evidence | Do only the predeclared, quota-aware study after explicit credential/budget authorization. It is not a prerequisite to writing T12/T13. Stop on the declared rule and keep all planned slots. |
| Parallel planning; before exposure | **T15 → D**, declared security scope | Owner chooses local/single-tenant or a justified broader scope. Verify only the corresponding boundary; no public endpoint before its access tests pass. T12 supplies minimum transport controls. |
| After T15 | **T16 → D**, concurrent limits and recovery | Measure a declared workload, shared quota and budget behavior, time spent waiting, and interruption recovery. Use isolated services and mocked providers where possible. |
| After T15–T17 | **T18 → D**, operations and restore | Set limits and retention from T16 data; test trace loss, backup restoration, and controlled alerts. No new service without a measured need. |
| After engineering gate | **T14 → E/C**, independent developer pilot | With consent, test setup, an independent agent, a useful failure, and result interpretation. Record false alarms and ground-truth disagreements. An isolated pilot can prepare earlier, but it cannot close external validation before the engineering gate. |
| Last | **T19 → E**, final scoped review | Verify both release states and all T01–T18 dispositions. A reduced full-project scope requires an explicit owner decision under T19; this section does not waive any original dependency. |

### Release gates, owners, and human decisions

**Engineering release** means a reproducible, bounded **developer release for the
declared deployment scope**. It requires T12/T13 independent-agent conformance;
trusted state/effect evidence with no unexplained critical false pass or false
failure; a clean hosted T17 run at the release SHA; T15 scoped access and secret
tests; T16 concurrent budget, isolation, and recovery tests; and T18 trace,
retention, and restore checks for that workload. T11 is required before including
real-model comparison claims. An authorized inconclusive T11 result is acceptable.
No production or multi-tenant claim follows from a local/single-user gate.

**Externally validated release** additionally requires T14: an independent
developer completes setup and the intended workflow, independently reproduces at
least one useful finding, and reviews ground-truth disputes and false alarms.
Critical feedback must be resolved before T19 closes. If no useful finding or
participant is available, this gate remains open. Tests by Codex or the reference
agent cannot substitute for that evidence.

| Blocking risk | Accountable role and task | Acceptance evidence |
| --- | --- | --- |
| External protocol or untrusted evidence | Evaluation engineering lead; T12/T13 | Versioned conformance tests; independent run with trusted events/state and a detected forbidden effect. |
| Oracle disagreement or false alarm | Evaluation lead and independent reviewer; T13/T14 | Reviewed external traces, positive/negative controls, dispute log, and resolved critical labels. |
| Live quota or incomplete cost | Experiment owner; T11/T16 | Predeclared slots, budget stop tests, provider usage coverage, and explicit unknown costs. Owner authorizes any spend. |
| Public access or data leakage | Security owner; T15 | Deployment-scope ADR, access matrix, negative auth/egress tests, and sanitized artifacts. |
| Concurrent loss or duplicate effect | Reliability owner; T16 | Workload envelope, interruption histories, stable effects, and correct terminal states. |
| CI or report cannot be reproduced | Release engineer; T17/T19 | Hosted run at exact SHA, test counts/skips, static gates, locks, and third-party replay. |
| Lost traces or unrecoverable records | Operations owner; T18 | Retention decision, trace/export failure test, isolated restore with checksums. |
| No independent usefulness evidence | Product owner and consenting developer; T14/T19 | Setup record, reproduced useful finding, feedback and blocker disposition. |

The project owner must approve deployment scope, live-provider budget, retention
policy, and release claim. A consenting independent developer and, where labels are
disputed, an independent reviewer must participate. Codex cannot supply their
consent, independent judgment, provider quotas, or a useful real-world defect.

**Non-goals for this path:** extra cases without a coverage gap, generic gateways,
new agent frameworks, dashboard expansion without a user need, speculative
provenance features, and Kubernetes/Kafka/ClickHouse without workload evidence.
Preserve all published assets and failed-run evidence. Do not claim exactly-once
external execution, model superiority, universal safety, or enterprise readiness.

**Next executable task:** T17 evidence closure and hosted-CI gate review at the
current pushed SHA. It needs no live model calls or product feature work. T12 now
meets its local protocol gate. T13 remains unstarted until the required T17 review
and T13 prerequisites pass. T11 still waits for its separate budget authorization.
