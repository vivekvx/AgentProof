# AgentProof

Use short, direct American English in responses. Follow the user's ASD-STE100-inspired response style.

Read `README.md` and `packages/contracts/README.md` before changing execution or contract behavior.
Preserve the React/Vite/TypeScript, Go/Chi/PostgreSQL, Python/Pydantic AI, Temporal, and OpenTelemetry boundaries.

- Keep provider model requests disabled in deterministic tests.
- Keep simulated order state scoped to each run.
- Commit each simulated side effect with its tool event and response ledger entry.
- Keep agent correctness failures separate from infrastructure failures.
- Do not claim exactly-once external execution from Temporal.
- Version scenario and evaluator behavior. Preserve immutable run references.
- Do not add Kubernetes, Kafka, billing, external agent uploads, or production credentials.
- Check current official library documentation when changing SDK integrations.

Run `make verify` with dedicated test PostgreSQL and Temporal services and the API and worker running.
Tests that require services must not be reported as passing when skipped.
Do not commit or push unless the user requests it.
