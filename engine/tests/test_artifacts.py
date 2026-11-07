import copy

import pytest
from agentproof.artifacts import manifest, validate


def test_source_inventory_contains_runtime_and_locks_without_credentials():
    value = manifest()
    validate(value)
    assert {
        "engine/uv.lock",
        "go.sum",
        "package-lock.json",
        "engine/agentproof/benchmark/validated_evaluator.py",
        "engine/agentproof/benchmark/llm.py",
    } <= value["files"].keys()
    assert all(not p.endswith(".env") and ".local/" not in p for p in value["files"])
    assert all(len(h) == 64 for h in value["files"].values())


@pytest.mark.parametrize("change", ["version", "hash", "file", "missing", "extra"])
def test_source_inventory_rejects_tampering(change):
    value = copy.deepcopy(manifest())
    if change == "version":
        value["schema_version"] = "unsupported"
    elif change == "hash":
        value["sha256"] = "0" * 64
    elif change == "file":
        value["files"]["engine/uv.lock"] = "0" * 64
    elif change == "missing":
        value["files"].pop("go.sum")
    else:
        value["files"][".env"] = "0" * 64
    with pytest.raises(ValueError, match="unsupported or altered"):
        validate(value)
