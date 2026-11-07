# Version 1 contracts

The Pydantic models in `engine/agentproof/contracts.py` define the public JSON contracts.
The committed JSON Schema files are generated with `make contracts`.
Tests reject schema drift. All contracts reject unknown fields.

The Go API embeds scenario and evaluator files. Each run manifest stores the full
content, version, and SHA-256 of the exact source bytes. The worker checks all three
against its installed files. Change the version and filename when behavior changes.
Keep old implementations available when supporting old runs.

`reference-v1` uses Pydantic AI FunctionModel and disables provider model requests.
`cancellation-v1` checks final order state, cancellation ledger count, and fault evidence.
The API is versioned under `/api/v1`. Evaluation failures use `failure_kind: agent`.
Exhausted execution activity retries use `failure_kind: infrastructure` and no verdict checks.

Implementation 02 keeps these v1 schemas unchanged. Internal `run_failures.kind`
separates evaluator defects, infrastructure faults, and cancellation. They map to
the existing coarse infrastructure failure result. The result `error` starts with
the internal classification. No agent verdict checks are returned for these cases.
The optional `Idempotency-Key` HTTP header does not change request or response JSON.
