"""Run with uv run --project engine python scripts/export_contracts.py."""

import json
from pathlib import Path

from agentproof.contracts import EvaluationResult, Manifest, Scenario, ToolEvent

root = Path(__file__).resolve().parents[1] / "packages/contracts"
for model, name in [
    (Scenario, "scenario"),
    (Manifest, "run-manifest"),
    (ToolEvent, "tool-event"),
    (EvaluationResult, "evaluation-result"),
]:
    (root / f"{name}.v1.schema.json").write_text(
        json.dumps(model.model_json_schema(), indent=2) + "\n"
    )
