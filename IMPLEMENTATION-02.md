# Implementation 02: reliability and verification

Implementation date: October 31, 2026.
The selected architecture, dashboard design, scenario files, evaluator descriptor,
and version 1 JSON schemas remain unchanged. No commit, push, or deployment was performed.

## Changes

- Added ordered database migrations with a migration ledger and an upgrade regression test.
- Added explicit tool operation keys: `RUN_ID:reference-v1:tool:SEQUENCE`.
- Added unique indexes, operation identity checks, and a deferred foreign key from each side effect to its tool event.
- Added optional HTTP `Idempotency-Key` handling. Concurrent repeated requests return one run. Changed requests return HTTP 409.
- Added database row guards that stop simulator writes after a terminal result.
- Made terminal failure recording atomic and idempotent. The first terminal result wins.
- Added attempt evidence with workflow, activity, and attempt identifiers.
- Added activity heartbeats and durable terminal cleanup after cancellation or exhausted execution retries.
- Separated non-retryable evaluator defects from retryable infrastructure exceptions.
- Added workflow patch markers and replay tests for Implementation 01 histories.
- Preserved the active Temporal activity span as the Python execution parent.
- Added scenario and attempt trace attributes and a live Jaeger export test.
- Added container builds for the API, worker, dashboard, and Linux verification runner.
- Fixed Temporal volume permissions before starting the non-root server.
- Replaced CI port-probing and host background processes with the complete Compose environment.

## Bugs and regression evidence

| Finding | Correction | Regression evidence |
| --- | --- | --- |
| Run creation could repeat after an uncertain HTTP response | Optional request key, transaction lock, and unique request row | Eight concurrent Go requests return one run; a changed agent returns 409 |
| Simulator operation identities were implicit | Explicit keys and database uniqueness/FK constraints | Stable retry keys, separate duplicate-decision keys, rejected duplicate effects |
| Cancellation could leave a queued or running database row | Shielded terminal recording activity | Cancellation before execution and during execution reaches a terminal database result |
| Active cancellation can arrive inside `ActivityError` | Use Temporal's `is_cancelled_exception` | Real Temporal cancellation ends as CANCELLED rather than a completed infrastructure failure workflow |
| A late thread could write after terminal recording | Run row locks and database triggers | Terminal simulator writes are rejected; late cancellation preserves an existing verdict |
| Evaluator defects were retried and classified as infrastructure outages | Non-retryable `EvaluatorFailure`, separate internal classification | Evaluator defect: one attempt; infrastructure error: three attempts |
| Database initialization had no ordered migration record | Transactional migration ledger | Existing safe and duplicate data survive upgrade; a second migration call changes nothing |
| A new failure activity name could break old workflow replay | Patch marker and frozen legacy command path | Successful and failed Implementation 01 histories replay with the new workflow |
| Python execution ignored the current Temporal activity parent | Preserve valid active context | Jaeger trace has both services, all tool event span IDs, and no missing parent references |
| Temporal could not open `/data/temporal.db` in Docker | Initialize the dedicated volume as UID/GID 1000 | Server health passes; server runs as `temporal`; volume is owned by `temporal` |

The first cancellation regression run exposed the wrapped cancellation bug.
The first trace-test assertions assumed a fixed HTTP span name and immediate export.
The installed Go instrumentation uses `POST`. Jaeger can return HTTP 404 before a
batch is delivered. The test now checks service and run attributes and polls for delivery.
It still fails on other HTTP errors or incomplete traces.

The first clean-runner launch also found that the browser base image does not
include `make`. The verification image now installs it explicitly.

## Failure classification

The existing public v1 result format is preserved. It has no separate evaluator or
cancellation enum. Internal classification is stored in `run_failures.kind` and
included at the start of the result `error` string.

| Condition | Public result | Internal classification | Execution behavior |
| --- | --- | --- | --- |
| Final-state or side-effect assertion fails | `failed`, `failure_kind: agent` | Agent verdict checks | Store the verdict; do not rerun the agent |
| Injected timeout after cancellation commits | Tool outcome `timeout_after_commit` | Expected scenario fault | Agent receives an unknown-outcome response; evaluator checks database state |
| Evaluator code raises a non-database exception | `infrastructure_failed`, infrastructure kind, no verdict checks | `evaluator` | One execution attempt; store observed state and error evidence |
| Database/transport/runtime infrastructure error | Same compatible platform-failure result | `infrastructure` | At most three execution attempts, then durable terminal recording |
| Temporal cancellation before completion | Same compatible platform-failure result | `cancelled` | Record observed state; finish the workflow as CANCELLED |
| Cancellation after a verdict commits | Existing verdict is retained | No replacement failure row | Committed evaluation wins |
| Worker process is killed | No immediate verdict | Unfinished attempt retained | Heartbeat timeout and retry; replay committed tool responses |
| Terminal recorder cannot reach PostgreSQL | Terminal recording remains pending | Error retained in Temporal history | Retry until database recovery; do not claim completion |
| Invalid create request | HTTP 400, no new run | Request validation | No retry |
| Reused request key with another agent | HTTP 409, no new run | Request conflict | No retry |
| Run creation during a database outage | HTTP 503 | Infrastructure | Caller can reuse its request key after recovery |

The worker does not silently enable model requests or retry unauthorized external actions.
All operations remain inside the simulated environment.

## Recovery checks

The tests start real subprocess workers on unique Temporal task queues. They use
SIGKILL, then start replacement workers. Test hooks exist only in the test worker.

| Kill checkpoint | Durable side effects after the kill | Final result after retry |
| --- | --- | --- |
| Inside the first tool transaction, before commit | 0 | Passed, 1 side effect, 2 tool events |
| After the first tool transaction commits, before response delivery | 1 | Passed, 1 side effect, 2 tool events |
| After the evaluation result commits, before activity acknowledgement | 1 | Original result retained, 1 side effect, 2 tool events |

Each case retains unfinished attempt 1 and records completed attempt 2.
Separate tests check concurrent execution, isolated run state, terminal-write guards,
idempotent terminal recording, and unique operation constraints.

A separate service test stopped Temporal in the dedicated Compose project. The API
persisted a queued run. The run completed with one side effect after Temporal restarted.
The test then stopped PostgreSQL. Run creation returned HTTP 503. After restart,
existing results and events were unchanged. A retried keyed request created one run.
The script restored both services in `finally` blocks.

## Commands and observed results

All commands run from `/Users/vivek/Projects/AgentProof`.
The dedicated Docker context is `colima-agentproof`; the test project is `agentproof02`.
`infra/verification.env` uses alternate loopback ports and preserves the original native services.

```sh
docker --context colima-agentproof compose --env-file infra/verification.env -p agentproof02 up -d --build --wait
docker --context colima-agentproof compose --env-file infra/verification.env -p agentproof02 --profile verification build verify
docker --context colima-agentproof compose --env-file infra/verification.env -p agentproof02 --profile verification run --rm verify
python3 scripts/verify_service_recovery.py --context colima-agentproof --project agentproof02
```

Observed before the final clean runner:

- Existing 16 Python tests passed after the first reliability changes.
- Nine real reliability tests passed after the cancellation fix.
- Four Temporal workflow tests passed, including two legacy replay tests.
- Migration upgrade, duplicate-effect constraint, and late-cancellation tests passed.
- Live Go/Python export to Jaeger passed.
- All three browser tests passed against the container API, worker, and dashboard.
- Go tests passed, including concurrent request idempotency.
- `go vet ./...`, Python static checks, and whitespace checks passed.
- Temporal and PostgreSQL service recovery passed.

The final clean Linux runner completed `make verify` with exit code 0:

- API and frontend builds passed.
- All Go tests passed, including request concurrency and database integration.
- **31 Python tests passed, with no skipped tests.**
- **3 Playwright tests passed** against the complete container stack.

The runner used Linux arm64, Go 1.26.5, Node.js 24.22.1, Python 3.13.13,
and the locked dependencies. Host `.venv` and `node_modules` directories were
excluded from the image. A source checksum check matched the trace and migration
test files inside the image to the workspace files.

Raw output is `.local/implementation-02/ci-clean.log`. The initial missing-`make`
launch failure is retained separately in `ci-clean-missing-make.log`.

The existing native API and worker were then restarted with the verified source.
All eight existing native run manifests, statuses, and results remained unchanged
through migration. Native smoke runs again produced safe/pass/1 and duplicate/fail/2.
Evidence is in `.local/implementation-02/native-upgrade.json`.

## Trace evidence

The inspected trace is `4a96e701270ca5a4cbb98742e7490e65`.
It contains 15 spans from `agentproof-api` and `agentproof-worker`.
The check confirms HTTP creation, workflow start, activity start/execution,
`evaluation.execute`, Pydantic AI calls, `tool.cancel_order`, and `tool.get_order`.
Every recorded tool span ID appears in the trace. Every parent reference resolves
inside the same trace. Execution attributes include:

- `agentproof.run_id`
- `agentproof.scenario.id` and `agentproof.scenario.sha256`
- `agentproof.evaluator.sha256`
- `temporal.workflow.id` and `temporal.workflow.run_id`
- `temporal.activity.id` and `temporal.activity.attempt`

Open the running [Jaeger trace](http://127.0.0.1:16687/trace/4a96e701270ca5a4cbb98742e7490e65).
Raw evidence is in `.local/implementation-02/jaeger-trace.json`.
The inspected UI screenshot is `.local/implementation-02/jaeger.png`.
Recovery evidence is saved as JSON files named after each kill checkpoint and failure class.
CI uploads `.local/implementation-02` even when verification fails.

## Completion criteria

| Criterion | Observed result | Evidence |
| --- | --- | --- |
| All existing tests pass | Met; the original tests remain in the 31-test Python suite and browser/Go suites | `ci-clean.log` |
| Worker interruption tests pass | Met; real SIGKILL before tool commit, after tool commit, and after result commit | `before_tool_commit.json`, `after_tool_commit.json`, `after_activity_commit.json` |
| Platform retries do not add unintended effects | Met; each recovered safe run has 1 side effect and 2 tool events; keyed concurrent HTTP requests create one run | Recovery JSON and Go integration tests |
| Database results remain consistent | Met; terminal guards, identity constraints, lossless migration, late cancellation, and service restart checks pass | Migration/reliability tests, `service-recovery.json`, `native-upgrade.json` |
| Docker startup is verified | Met; PostgreSQL, Temporal, API, worker, dashboard, and Jaeger started; the non-root Temporal server passed health checks | `docker-start-all.log`, `docker-services.json`, `app-startup.log` |
| Complete trace appears in Jaeger | Met; 15 spans, both services, run/scenario/attempt attributes, tool span IDs, and closed parent references | `jaeger-trace.json`, `jaeger.png`, live trace link |
| Repository CI checks pass locally | Met; the configured Compose verification runner completed `make verify` successfully | `ci-clean.log` |
| Every failure has a documented classification | Met for this slice's supported request, agent, evaluator, infrastructure, interruption, and cancellation cases | Failure table, attempt/failure records, classification tests |

No blocking issue remains in the verified local flow. GitHub-hosted Actions was
not run, because this work was not pushed.

## Files changed

- `apps/api/internal/server/server.go`, `integration_test.go`: request idempotency and trace attributes.
- `db/migrate.go`, `db/migrations/002_reliability.sql`, `003_request_identity.sql`: ordered upgrades, identity constraints, terminal guards, attempt/failure records.
- `engine/agentproof/database.py`, `reliability.py`: database and reliability helpers.
- `engine/agentproof/execution.py`, `simulator.py`, `worker.py`, `workflow.py`: transaction fencing, classification, retry and cancellation handling, attempt tracing.
- `engine/tests/conftest.py`, `test_workflow.py`: new migrations, fixture cleanup, legacy replay coverage.
- `engine/tests/process_worker.py`, `legacy_workflow.py`, `test_reliability.py`, `test_migration.py`, `test_trace_delivery.py`: regression and live verification tests.
- `Dockerfile.dev`, `.dockerignore`, `compose.yaml`, `infra/verification.env`: complete container environment and isolated test ports.
- `apps/web/vite.config.ts`, `playwright.config.ts`: container proxy and external test URL configuration. Dashboard components and styling remain unchanged.
- `.github/workflows/ci.yml`, `Makefile`: shared container verification commands.
- `scripts/verify_service_recovery.py`: recovery of the selected test services.
- `README.md`, `packages/contracts/README.md`, `IMPLEMENTATION-02.md`: setup, compatibility, classification, and evidence.

## Stop the isolated environment

The native dashboard is available at [localhost:5173](http://127.0.0.1:5173).
The verified container dashboard is available at [localhost:5174](http://127.0.0.1:5174).
To stop only the isolated Docker stack while retaining its data:

```sh
docker --context colima-agentproof compose --env-file infra/verification.env -p agentproof02 down
```

Do not add `-v` when results and workflow history must be retained.

## Limits and open issues

- GitHub-hosted Actions has not been triggered. No push was authorized. The local container commands are the same commands configured in CI.
- A permanent PostgreSQL outage leaves terminal cleanup pending. Restoring database access is required.
- Temporal force termination bypasses workflow cleanup. Cancellation is the supported path tested here.
- Existing Implementation 01 histories keep their original command sequence. New runs use the hardened path.
- Jaeger uses in-memory development storage. Its traces do not survive a collector restart; exported JSON evidence remains available.
- A process killed before span export can lose unfinished spans. Database tool events and attempt evidence remain authoritative for recovery.
- The existing pinned Jaeger 1 image reports an upstream end-of-life notice. Replacing the telemetry deployment is deferred from this architecture-preserving reliability change.
- The simulator is scoped by run. It is not an external-agent process sandbox.
- Temporal activities can execute more than once. Safety here depends on database transactions, stable operation identities, replay, and terminal-state guards.
