"""Regenerate only version 3 interaction schemas."""

import json
from pathlib import Path

from agentproof.benchmark import interaction_contracts

root = Path(__file__).resolve().parents[1] / "packages/contracts/v3"
root.mkdir(exist_ok=True)
for name in (
    "InteractionDataset",
    "InteractionScenario",
    "InteractionAgentConfiguration",
    "InteractionConfiguration",
    "InteractionManifest",
    "InteractionResult",
):
    (root / (name + ".schema.json")).write_text(
        json.dumps(getattr(interaction_contracts, name).model_json_schema(), indent=2)
        + "\n"
    )
