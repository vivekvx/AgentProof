"""Check that every published JSON schema matches its Pydantic contract."""

import json
from pathlib import Path

from agentproof import contracts
from agentproof.benchmark import contracts as benchmark
from agentproof.benchmark import interaction_contracts
from jsonschema import Draft202012Validator, SchemaError

ROOT = Path(__file__).resolve().parents[1] / "packages/contracts"
SCHEMAS = {
    "scenario.v1.schema.json": contracts.Scenario,
    "run-manifest.v1.schema.json": contracts.Manifest,
    "tool-event.v1.schema.json": contracts.ToolEvent,
    "evaluation-result.v1.schema.json": contracts.EvaluationResult,
    **{
        f"v2/{name}.schema.json": getattr(benchmark, name)
        for name in (
            "ModelConfiguration",
            "Dataset",
            "Scenario",
            "Fixture",
            "FaultProfile",
            "Assertions",
            "AgentConfiguration",
            "EvaluationResult",
            "EvaluationConfiguration",
            "EvaluationManifest",
        )
    },
    "v3/AgentConfiguration.schema.json": benchmark.AgentConfigurationV3,
    "v5/StudyEvaluationManifest.schema.json": benchmark.StudyEvaluationManifest,
    **{
        f"v3/{name}.schema.json": getattr(interaction_contracts, name)
        for name in (
            "InteractionDataset",
            "InteractionScenario",
            "InteractionAgentConfiguration",
            "InteractionConfiguration",
            "InteractionManifest",
            "InteractionResult",
        )
    },
}
PROTOCOL_SCHEMAS = (
    "v4/AgentOutcome.schema.json",
    "v4/AgentTaskEnvelope.schema.json",
    "v4/AgentToolCall.schema.json",
    "v4/AgentToolResult.schema.json",
)


def main() -> None:
    mismatches = []
    for relative_path, model in SCHEMAS.items():
        path = ROOT / relative_path
        try:
            actual = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            mismatches.append(
                f"{relative_path}: unavailable or invalid JSON ({type(exc).__name__})"
            )
            continue
        if actual != model.model_json_schema():
            mismatches.append(f"{relative_path}: differs from {model.__name__}")
    for relative_path in PROTOCOL_SCHEMAS:
        path = ROOT / relative_path
        try:
            schema = json.loads(path.read_text())
            Draft202012Validator.check_schema(schema)
            if schema.get("additionalProperties") is not False:
                mismatches.append(
                    f"{relative_path}: protocol schema must reject extra fields"
                )
        except (OSError, json.JSONDecodeError, SchemaError) as exc:
            mismatches.append(
                f"{relative_path}: unavailable or invalid schema ({type(exc).__name__})"
            )
    if mismatches:
        raise SystemExit("Contract schema check failed:\n" + "\n".join(mismatches))
    print(
        f"Contract schemas match: {len(SCHEMAS)} generated, "
        f"{len(PROTOCOL_SCHEMAS)} protocol schemas valid"
    )


if __name__ == "__main__":
    main()
