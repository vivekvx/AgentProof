"""Immutable execution bindings and portable source snapshots for new runs."""

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

from .artifacts import ROOT, digest
from .artifacts import manifest as source_manifest

SCHEMA = "execution-provenance-v1"
RUNNER = "agentproof-runner-v1"
SECRET = re.compile(
    rb"(?i)(?:gsk_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]{12,})"
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def value_hash(value):
    return digest(canonical(value))


def safe_source(root=ROOT):
    inventory = source_manifest(root)
    contents = {}
    for name, expected in inventory["files"].items():
        path = Path(root) / name
        if path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()):
            raise ValueError("unsafe source path")
        raw = path.read_bytes()
        if digest(raw) != expected or SECRET.search(raw):
            raise ValueError("source changed or contains credential-shaped content")
        contents[name] = base64.b64encode(raw).decode("ascii")
    return inventory, contents


def git_identity(root=ROOT):
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        commit = result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        has_changes = bool(dirty.stdout.strip())
    except OSError:
        has_changes = True
    return {
        "commit": commit if commit and re.fullmatch(r"[0-9a-f]{40}", commit) else None,
        "dirty_or_uncommitted": has_changes or not commit,
    }


def effective_settings(config):
    if config.policy != "llm":
        return {"provider": "offline", "model": None, "generation": None}
    model = config.model_configuration
    settings = {
        "max_output_tokens": model.max_output_tokens,
        "temperature": model.temperature,
        "top_p": model.top_p,
        "parallel_tool_calls": False,
        "tool_choice": "required_probe_then_auto",
    }
    # The current Pydantic AI adapter drops sampling values for GPT-OSS reasoning.
    if model.model.startswith("openai/gpt-oss-"):
        settings.pop("temperature")
        settings.pop("top_p")
    return {
        "provider": model.provider,
        "model": model.model,
        "base_url": model.base_url,
        "request_timeout_seconds": model.timeout_seconds,
        "generation": settings,
        "profile_sha256": value_hash(
            config.model_dump(exclude={"model_configuration": {"api_key_env"}})
        ),
    }


def raw_assets(run_manifest, root=ROOT):
    """Keep exact approved asset bytes, including an external non-secret profile."""
    from .benchmark.registry import reference as installed_reference

    paths = {}
    for name in ("dataset", "scenario", "agent", "evaluator", "tool_schema"):
        item = run_manifest.get(name)
        if not isinstance(item, dict):
            continue
        if name == "dataset":
            rel = f"benchmarks/support.v{item['version']}.json"
        elif name == "evaluator":
            rel = (
                f"evaluators/support.v{item['version']}.json"
                if "support-" in item["id"]
                else "evaluators/cancellation.v1.json"
            )
        elif name == "scenario":
            rel = "scenarios/cancel-order.v1.json"
        elif name == "tool_schema":
            rel = f"tools/support-tools.v{item['version']}.json"
        else:
            rel = f"agents/{item['id']}.v1.json"
        expected = installed_reference(rel)
        if expected != item:
            raise ValueError("asset differs from stored reference")
        source = Path(root) / "packages/contracts" / rel
        if not source.exists():
            directory = __import__("os").environ.get("AGENTPROOF_MODEL_CONFIG_DIR")
            if not directory:
                raise ValueError("missing model profile directory")
            source = Path(directory) / Path(rel).name
        if source.is_symlink() or not source.is_file():
            raise ValueError("unsafe asset path")
        raw = source.read_bytes()
        if digest(raw) != item["sha256"] or SECRET.search(raw):
            raise ValueError("asset changed or contains credential-shaped content")
        paths[name] = base64.b64encode(raw).decode("ascii")
    return paths


def record(conn, identity, kind, run_manifest, scenario=None, root=ROOT):
    if kind == "benchmark":
        record(conn, str(run_manifest["id"]), "evaluation", run_manifest, root=root)
    table, key = location(kind)
    old = conn.execute(
        f"SELECT sha256,manifest FROM {table} WHERE {key}=%s", (identity,)
    ).fetchone()
    if old:
        if old[1]["source_sha256"] != source_manifest(root)["sha256"]:
            raise ValueError("installed source differs from saved execution")
        return old[0]
    source = source_manifest(root)
    content = None
    if (
        conn.execute(
            "SELECT 1 FROM execution_source_snapshots WHERE sha256=%s",
            (source["sha256"],),
        ).fetchone()
        is None
    ):
        source, content = safe_source(root)
    config = run_manifest["agent"]["content"] if kind != "run" else None
    from .benchmark.contracts import AgentConfiguration
    from .benchmark.interaction_contracts import InteractionAgentConfiguration

    cfg = (
        (
            InteractionAgentConfiguration
            if config.get("schema_version") == "3.0"
            else AgentConfiguration
        ).model_validate(config)
        if config
        else None
    )
    execution_manifest = {
        "schema_version": SCHEMA,
        "runner_version": RUNNER,
        "source_sha256": source["sha256"],
        "git": git_identity(root),
        "lock_sha256": {
            k: source["files"][k]
            for k in ("go.sum", "package-lock.json", "engine/uv.lock")
        },
        "python_runtime": sys.version.split()[0],
        "go_runtime": go_version(root),
        "run_id": str(run_manifest["id"]) if kind != "run" else identity,
        "trial_id": identity if kind == "benchmark" else None,
        "evaluation_id": identity if kind == "evaluation" else None,
        "dataset": ref(run_manifest.get("dataset")),
        "scenario": {
            "id": scenario["id"],
            "version": scenario["version"],
            "sha256": value_hash(scenario),
        }
        if scenario
        else ref(run_manifest.get("scenario")),
        "fixture": {
            "version": scenario["fixture"]["version"],
            "sha256": value_hash(scenario["fixture"]),
        }
        if scenario
        else None,
        "evaluator": ref(run_manifest["evaluator"]),
        "tool_schema": ref(run_manifest.get("tool_schema")),
        "simulator": {
            "version": "source-sha256",
            "sha256": source["files"]["engine/agentproof/simulator.py"]
            if kind == "run"
            else source["files"]["engine/agentproof/benchmark/interaction_simulator.py"]
            if run_manifest.get("dataset", {}).get("version") == 6
            else source["files"]["engine/agentproof/benchmark/simulator.py"],
        },
        "agent": ref(run_manifest["agent"])
        if kind != "run"
        else {"id": run_manifest["agent"], "version": run_manifest["agent_version"]},
        "prompt_sha256": digest(cfg.prompt.encode()) if cfg else None,
        "prompt_version": cfg.prompt_version if cfg else None,
        "model": effective_settings(cfg)
        if cfg
        else {"provider": "offline", "model": None, "generation": None},
        "limits": scenario["limits"] if scenario else None,
        "fault": scenario["fault"] if scenario else None,
        "asset_bytes": raw_assets(run_manifest, root),
    }
    sha = value_hash(execution_manifest)
    with conn.transaction():
        if content is not None:
            conn.execute(
                "INSERT INTO execution_source_snapshots(sha256,inventory,content) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (source["sha256"], Jsonb(source), Jsonb(content)),
            )
        conn.execute(
            f"INSERT INTO {table}({key},sha256,manifest,source_sha256) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (identity, sha, Jsonb(execution_manifest), source["sha256"]),
        )
    return sha


def ref(value):
    if value is None:
        return None
    return {k: value[k] for k in ("id", "version", "sha256")}


def location(kind):
    try:
        return {
            "benchmark": ("benchmark_execution_provenance", "trial_id"),
            "evaluation": ("benchmark_evaluation_provenance", "evaluation_id"),
            "run": ("run_execution_provenance", "run_id"),
        }[kind]
    except KeyError as error:
        raise ValueError("unsupported execution kind") from error


def go_version(root):
    path = Path(root) / "go.version"
    if path.exists():
        return path.read_text().strip()
    try:
        return subprocess.run(
            ["go", "version"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def bind_result(conn, identity, kind, result):
    table, key = location(kind)
    digest_value = value_hash(result)
    with conn.transaction():
        row = conn.execute(
            f"SELECT result_sha256 FROM {table} WHERE {key}=%s FOR UPDATE", (identity,)
        ).fetchone()
        if row is None:
            raise ValueError("missing execution provenance")
        if row[0] is not None and row[0] != digest_value:
            raise ValueError("result provenance conflict")
        if row[0] is None:
            conn.execute(
                f"UPDATE {table} SET result_sha256=%s WHERE {key}=%s",
                (digest_value, identity),
            )


def verify_binding(conn, identity, kind):
    table, key = location(kind)
    row = conn.execute(
        f"SELECT p.sha256,p.manifest,p.source_sha256,p.result_sha256,s.inventory,s.content FROM {table} p JOIN execution_source_snapshots s ON s.sha256=p.source_sha256 WHERE p.{key}=%s",
        (identity,),
    ).fetchone()
    if not row:
        return None  # Historical result: incomplete provenance.
    sha, body, source_sha, result_sha, inventory, content = row
    if (
        body.get("schema_version") != SCHEMA
        or value_hash(body) != sha
        or body.get("source_sha256") != source_sha
        or body.get(key) != identity
        or source_sha != inventory.get("sha256")
        or inventory.get("schema_version") != "source-inventory-v1"
    ):
        return False
    files = inventory.get("files")
    if (
        not isinstance(files, dict)
        or set(files) != set(content)
        or value_hash(files) != source_sha
    ):
        return False
    try:
        if any(
            digest(base64.b64decode(content[p], validate=True)) != h
            for p, h in files.items()
        ):
            return False
    except (ValueError, TypeError):
        return False
    if any(
        body.get("lock_sha256", {}).get(k) != files[k]
        for k in ("go.sum", "package-lock.json", "engine/uv.lock")
    ):
        return False
    simulator_path = (
        "engine/agentproof/simulator.py"
        if kind == "run"
        else "engine/agentproof/benchmark/interaction_simulator.py"
        if body.get("dataset", {}).get("version") == 6
        else "engine/agentproof/benchmark/simulator.py"
    )
    if body.get("simulator", {}).get("sha256") != files[simulator_path]:
        return False
    try:
        for name, encoded in body["asset_bytes"].items():
            raw = base64.b64decode(encoded, validate=True)
            if (
                SECRET.search(raw)
                or digest(raw) != body[name]["sha256"]
                or json.loads(raw) != body[name].get("content", json.loads(raw))
            ):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    source = {
        "benchmark": "benchmark_trials",
        "evaluation": "benchmark_evaluations",
        "run": "runs",
    }[kind]
    current = conn.execute(
        f"SELECT result FROM {source} WHERE id=%s", (identity,)
    ).fetchone()
    if not current or (current[0] is None) != (result_sha is None):
        return False
    return result_sha is None or value_hash(current[0]) == result_sha
