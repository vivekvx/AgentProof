import hashlib
import json
import os
from pathlib import Path

from .contracts import AgentConfiguration, Dataset, EvaluationManifest
from .interaction_contracts import (
    InteractionAgentConfiguration,
    InteractionDataset,
    InteractionManifest,
)

ROOT = Path(__file__).resolve().parents[3] / "packages/contracts"


def reference(path):
    candidate = ROOT / path
    if path.startswith("agents/") and not candidate.exists():
        directory = os.environ.get("AGENTPROOF_MODEL_CONFIG_DIR")
        if not directory:
            raise ValueError("model configuration directory is not set")
        candidate = Path(directory) / Path(path).name
    data = candidate.read_bytes()
    content = json.loads(data)
    return {
        "id": content["id"],
        "version": content["version"],
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": content,
    }


def dataset(version=5):
    model = InteractionDataset if version == 6 else Dataset
    return model.model_validate(
        reference(f"benchmarks/support.v{version}.json")["content"]
    )


def agent(agent_id):
    if not __import__("re").fullmatch(r"[a-z][a-z0-9-]{1,63}", agent_id):
        raise ValueError("unknown agent configuration")
    return AgentConfiguration.model_validate(
        reference(f"agents/{agent_id}.v1.json")["content"]
    )


def validate_snapshot(manifest):
    if manifest.get("dataset", {}).get("version") == 6:
        InteractionManifest.model_validate(manifest)
        if manifest["dataset"]["id"] != "customer-support":
            raise ValueError("unsupported dataset identity")
        for key, path in [
            ("dataset", "benchmarks/support.v6.json"),
            ("agent", "agents/support-interaction-safe.v1.json"),
            ("evaluator", "evaluators/support.v5.json"),
            ("tool_schema", "tools/support-tools.v2.json"),
        ]:
            if manifest[key] != reference(path):
                raise ValueError("unsupported immutable interaction reference")
        InteractionDataset.model_validate(manifest["dataset"]["content"])
        InteractionAgentConfiguration.model_validate(manifest["agent"]["content"])
        return
    EvaluationManifest.model_validate(manifest)
    agent(manifest["agent"]["id"])
    if manifest["dataset"]["version"] not in {1, 2, 3, 4, 5}:
        raise ValueError("unsupported dataset version")
    for key, path in [
        ("dataset", f"benchmarks/support.v{manifest['dataset']['version']}.json"),
        ("agent", f"agents/{manifest['agent']['id']}.v1.json"),
        (
            "evaluator",
            f"evaluators/support.v{max(1, manifest['dataset']['version'] - 1)}.json",
        ),
        ("tool_schema", "tools/support-tools.v1.json"),
    ]:
        if manifest[key] != reference(path):
            raise ValueError("unsupported immutable benchmark reference")
    Dataset.model_validate(manifest["dataset"]["content"])
    AgentConfiguration.model_validate(manifest["agent"]["content"])
