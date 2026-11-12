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

v3 = root.parent / "v3"
v3.mkdir(exist_ok=True)
(v3 / "AgentConfiguration.schema.json").write_text(
    json.dumps(contracts.AgentConfigurationV3.model_json_schema(), indent=2) + "\n"
)

v5 = root.parent / "v5"
v5.mkdir(exist_ok=True)
(v5 / "StudyEvaluationManifest.schema.json").write_text(
    json.dumps(contracts.StudyEvaluationManifest.model_json_schema(), indent=2) + "\n"
)
