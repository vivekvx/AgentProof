"""Bounded JSON evidence bundle; verification and replay never call a provider."""

import argparse
import base64
import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from .artifacts import ROOT, digest
from .artifacts import manifest as installed_source
from .benchmark import (
    audited_evaluator,
    audited_evaluator_v3,
    evidence,
    interaction_evaluator,
    validated_evaluator,
)
from .benchmark import (
    evaluator as legacy_evaluator,
)
from .benchmark.contracts import AgentOutcome, Scenario
from .benchmark.interaction_contracts import InteractionOutcome, InteractionScenario
from .database import connect
from .provenance import SECRET, canonical, value_hash, verify_binding

VERSION = "evaluation-bundle-v1"
MAX_BYTES = 24_000_000
PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")


def distinct(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate bundle entry")
        result[key] = value
    return result


def export_trial(conn, trial_id):
    if verify_binding(conn, trial_id, "benchmark") is not True:
        raise ValueError(
            "historical or damaged provenance cannot be exported as verified"
        )
    trial = conn.execute(
        "SELECT t.scenario,t.state,t.result,t.agent_outcome,e.manifest FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id WHERE t.id=%s",
        (trial_id,),
    ).fetchone()
    if not trial or not trial[2]:
        raise ValueError("trial is not terminal")
    ref = conn.execute(
        "SELECT p.sha256,p.manifest,p.result_sha256,s.inventory,s.content FROM benchmark_execution_provenance p JOIN execution_source_snapshots s ON s.sha256=p.source_sha256 WHERE p.trial_id=%s",
        (trial_id,),
    ).fetchone()
    events, effects, receipts, head = evidence.load(conn, trial_id)
    data = {
        "scenario": trial[0],
        "state": trial[1],
        "result": trial[2],
        "outcome": trial[3],
        "run_manifest": trial[4],
        "events": events,
        "effects": effects,
        "receipts": receipts,
        "head": head,
    }
    bundle = {
        "schema_version": VERSION,
        "trial_id": trial_id,
        "execution_sha256": ref[0],
        "execution": ref[1],
        "source": ref[3],
        "source_files": ref[4],
        "result_sha256": ref[2],
        "evidence": data,
        "evidence_sha256": value_hash(data),
    }
    payload = canonical(bundle)
    if len(payload) > MAX_BYTES or SECRET.search(payload):
        raise ValueError("oversized or sensitive bundle")
    return bundle


def validate_bundle(bundle, *, require_installed=False, root=ROOT):
    if (
        type(bundle) is not dict
        or set(bundle)
        != {
            "schema_version",
            "trial_id",
            "execution_sha256",
            "execution",
            "source",
            "source_files",
            "result_sha256",
            "evidence",
            "evidence_sha256",
        }
        or bundle["schema_version"] != VERSION
    ):
        raise ValueError("unsupported bundle format")
    if (
        value_hash(bundle["execution"]) != bundle["execution_sha256"]
        or bundle["execution"].get("schema_version") != "execution-provenance-v1"
    ):
        raise ValueError("execution manifest digest mismatch")
    source = bundle["source"]
    files = bundle["source_files"]
    if (
        not isinstance(files, dict)
        or set(files) != set(source.get("files", {}))
        or value_hash(source["files"]) != source["sha256"]
        or source["sha256"] != bundle["execution"]["source_sha256"]
    ):
        raise ValueError("source digest mismatch")
    for name, expected in source["files"].items():
        if (
            not PATH.fullmatch(name)
            or name.startswith(".")
            and name not in {".dockerignore"}
            or any(x in {".", ".."} for x in name.split("/"))
        ):
            raise ValueError("unsafe bundle path")
        try:
            raw = base64.b64decode(files[name], validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid source encoding") from error
        if digest(raw) != expected:
            raise ValueError("modified source file")
    if require_installed and installed_source(root) != source:
        raise ValueError("installed source or dependency lock differs")
    execution = bundle["execution"]
    if execution.get("runner_version") != "agentproof-runner-v1":
        raise ValueError("unsupported runner implementation")
    if any(
        execution.get("lock_sha256", {}).get(k) != source["files"][k]
        for k in ("go.sum", "package-lock.json", "engine/uv.lock")
    ):
        raise ValueError("dependency lock reference mismatch")
    simulator_path = (
        "engine/agentproof/benchmark/interaction_simulator.py"
        if bundle["evidence"]["run_manifest"].get("dataset", {}).get("version") == 6
        else "engine/agentproof/benchmark/simulator.py"
    )
    if execution.get("simulator", {}).get("sha256") != source["files"].get(
        simulator_path
    ):
        raise ValueError("simulator implementation mismatch")
    model = execution.get("model", {})
    if model.get("provider") != "offline" and (
        not model.get("model")
        or not model.get("base_url")
        or not model.get("request_timeout_seconds")
        or not isinstance(model.get("generation"), dict)
        or not model["generation"].get("max_output_tokens")
    ):
        raise ValueError("missing effective model settings")
    data = bundle["evidence"]
    if (
        value_hash(data) != bundle["evidence_sha256"]
        or value_hash(data["result"]) != bundle["result_sha256"]
    ):
        raise ValueError("result or evidence digest mismatch")
    if (
        data["result"]["trial_id"] != bundle["trial_id"]
        or bundle["execution"]["trial_id"] != bundle["trial_id"]
    ):
        raise ValueError("trial-to-manifest mismatch")
    for name, encoded in bundle["execution"]["asset_bytes"].items():
        if name not in {"dataset", "agent", "evaluator", "tool_schema", "scenario"}:
            raise ValueError("unknown asset")
        raw = base64.b64decode(encoded, validate=True)
        if (
            SECRET.search(raw)
            or digest(raw) != data["run_manifest"][name]["sha256"]
            or json.loads(raw) != data["run_manifest"][name]["content"]
        ):
            raise ValueError("altered asset or credential")
    evaluator_version = data["run_manifest"]["evaluator"]["version"]
    if evaluator_version not in (1, 2, 3, 4, 5):
        raise ValueError(
            f"recorded evaluator implementation version {evaluator_version} unavailable for offline replay"
        )
    if not all(
        name in bundle["execution"]["asset_bytes"]
        for name in ("dataset", "agent", "evaluator", "tool_schema")
    ):
        raise ValueError("missing replay asset")
    if (
        execution["dataset"]["sha256"] != data["run_manifest"]["dataset"]["sha256"]
        or execution["evaluator"]["sha256"]
        != data["run_manifest"]["evaluator"]["sha256"]
        or execution["agent"]["sha256"] != data["run_manifest"]["agent"]["sha256"]
    ):
        raise ValueError("asset reference mismatch")
    if bundle["execution"]["scenario"]["sha256"] != value_hash(data["scenario"]):
        raise ValueError("scenario digest mismatch")
    if bundle["execution"]["prompt_sha256"] != digest(
        data["run_manifest"]["agent"]["content"]["prompt"].encode()
    ):
        raise ValueError("prompt digest mismatch")
    if (
        bundle["execution"]["tool_schema"]["sha256"]
        != data["run_manifest"]["tool_schema"]["sha256"]
    ):
        raise ValueError("tool digest mismatch")
    if (
        data["result"].get("provenance")
        and data["result"]["provenance"]["attempt"].get("execution_sha256")
        != bundle["execution_sha256"]
    ):
        raise ValueError("result-to-manifest reference mismatch")
    return True


def load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("unsafe or oversized bundle")
    raw = path.read_bytes()
    if SECRET.search(raw):
        raise ValueError("bundle contains credential-shaped content")
    return json.loads(raw, object_pairs_hook=distinct)


def replay(bundle, *, root=ROOT):
    validate_bundle(bundle, require_installed=True, root=root)
    data = bundle["evidence"]
    result = data["result"]
    version = data["run_manifest"]["evaluator"]["version"]
    if version not in (1, 2, 3, 4, 5):
        raise ValueError(f"missing evaluator version {version}")
    from .benchmark import registry

    original = registry.reference
    asset = base64.b64decode(bundle["execution"]["asset_bytes"]["agent"])
    with TemporaryDirectory(prefix="agentproof-replay-") as directory:
        name = f"{data['run_manifest']['agent']['id']}.v1.json"
        (Path(directory) / name).write_bytes(asset)
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTPROOF_MODEL_CONFIG_DIR": directory,
                    "AGENTPROOF_ALLOW_MODEL_REQUESTS": "0",
                },
            ),
            patch.object(
                registry,
                "reference",
                side_effect=lambda path: (
                    {
                        "id": data["run_manifest"]["agent"]["id"],
                        "version": data["run_manifest"]["agent"]["version"],
                        "sha256": digest(asset),
                        "content": json.loads(asset),
                    }
                    if path == f"agents/{name}"
                    else original(path)
                ),
            ),
        ):
            if version == 5:
                condition = result["assessment"]["execution"]
                computed = interaction_evaluator.assess(
                    trial_id=bundle["trial_id"],
                    scenario=InteractionScenario.model_validate(data["scenario"]),
                    state=data["state"],
                    events=data["events"],
                    effects=data["effects"],
                    receipts=data["receipts"],
                    head=data["head"],
                    manifest=data["run_manifest"],
                    outcome=InteractionOutcome.model_validate(data["outcome"])
                    if data["outcome"] and condition == "completed"
                    else None,
                    attempt=result["provenance"]["attempt"],
                    latency_ms=result["latency_ms"],
                    trace_id=result["trace_id"],
                    limit_hit="execution_limit" in result["assessment"]["issues"],
                    provider_error=result.get("provider_error"),
                    execution_error=condition
                    if condition in ("infrastructure", "cancelled", "evaluator")
                    else None,
                )
            elif version == 4:
                condition = result["assessment"]["execution"]
                computed = validated_evaluator.assess(
                    trial_id=bundle["trial_id"],
                    scenario=Scenario.model_validate(data["scenario"]),
                    state=data["state"],
                    events=data["events"],
                    effects=data["effects"],
                    receipts=data["receipts"],
                    head=data["head"],
                    manifest=data["run_manifest"],
                    outcome=AgentOutcome.model_validate(data["outcome"])
                    if data["outcome"] and condition == "completed"
                    else None,
                    attempt=result["provenance"]["attempt"],
                    latency_ms=result["latency_ms"],
                    trace_id=result["trace_id"],
                    limit_hit="execution_limit" in result["assessment"]["issues"],
                    provider_error=result.get("provider_error"),
                    execution_error=condition
                    if condition in ("infrastructure", "cancelled", "evaluator")
                    else None,
                )
            else:
                if not data["outcome"]:
                    raise ValueError(
                        "recorded evaluator requires a completed agent outcome"
                    )
                implementation = {
                    1: legacy_evaluator,
                    2: audited_evaluator,
                    3: audited_evaluator_v3,
                }[version]
                computed = implementation.evaluate(
                    bundle["trial_id"],
                    Scenario.model_validate(data["scenario"]),
                    data["state"],
                    data["events"],
                    data["effects"],
                    AgentOutcome.model_validate(data["outcome"]),
                    data["run_manifest"]["evaluator"]["sha256"],
                    result["trace_id"],
                    result["latency_ms"],
                    any(e.get("limit_exceeded") for e in data["events"]),
                )
    fields = (
        "verdict",
        "failure_category",
        "assertions",
        "final_state",
        "effect_counts",
    )
    if version in (4, 5):
        fields += ("assessment", "provenance")
    for field in fields:
        if computed.model_dump()[field] != result[field]:
            raise ValueError("deterministic evaluation mismatch: " + field)
    return {
        "verdict": computed.verdict,
        "trial_id": bundle["trial_id"],
        "source_sha256": bundle["source"]["sha256"],
        "scope": "recorded evidence replay; no model generation",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    exp = actions.add_parser("export")
    exp.add_argument("trial_id")
    exp.add_argument("path", type=Path)
    for name in ("verify", "replay"):
        cmd = actions.add_parser(name)
        cmd.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.action == "export":
        bundle = export_trial(connect(), args.trial_id)
        with args.path.open("x") as output:
            json.dump(bundle, output, sort_keys=True, separators=(",", ":"))
        print("Exported trial", args.trial_id)
    else:
        bundle = load(args.path)
        print(
            json.dumps(
                replay(bundle)
                if args.action == "replay"
                else {"verified": validate_bundle(bundle)}
            )
        )


if __name__ == "__main__":
    main()
