"""Generate only v2 schemas; published dataset and v1 schema bytes stay unchanged."""

import json
from pathlib import Path

from agentproof.benchmark import contracts

root = Path(__file__).resolve().parents[1] / "packages/contracts/v2"
for name in [
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
]:
    (root / (name + ".schema.json")).write_text(
        json.dumps(getattr(contracts, name).model_json_schema(), indent=2) + "\n"
    )
