"""T05: persisted source binding and profile-independent evidence replay."""

import base64
import copy
import json

import pytest
from agentproof import bundle, provenance
from agentproof.benchmark.execution import execute_trial
from agentproof.database import connect


async def test_new_result_is_bound_and_replays_without_profile(
    benchmark_factory, dsn, monkeypatch, tmp_path
):
    _, ids, _ = benchmark_factory(ids=["cs-016"], version=5)
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "PASS"
    with connect(dsn) as c:
        assert provenance.verify_binding(c, ids[0], "benchmark") is True
        data = bundle.export_trial(c, ids[0])
    assert (
        data["execution_sha256"] == result["provenance"]["attempt"]["execution_sha256"]
    )
    assert data["execution"]["git"] == provenance.git_identity()
    assert data["execution"]["lock_sha256"]["engine/uv.lock"]
    monkeypatch.delenv("AGENTPROOF_MODEL_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert bundle.validate_bundle(data, require_installed=True)
    assert bundle.replay(data)["verdict"] == "PASS"
    path = tmp_path / "run.json"
    path.write_text(json.dumps(data))
    assert bundle.load(path) == data


@pytest.mark.parametrize("failure", [None, "cancelled"])
async def test_parent_evaluation_result_has_immutable_binding(
    benchmark_factory, dsn, failure
):
    from agentproof.benchmark.execution import finalize, plan

    evaluation_id, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    assert plan(evaluation_id, dsn) == ids
    if failure is None:
        await execute_trial(ids[0], dsn)
    metrics = finalize(evaluation_id, category=failure, dsn=dsn)
    with connect(dsn) as conn:
        assert provenance.verify_binding(conn, evaluation_id, "evaluation") is True
        assert provenance.verify_binding(conn, ids[0], "benchmark") is True
        row = conn.execute(
            "SELECT sha256,manifest,result_sha256 FROM benchmark_evaluation_provenance WHERE evaluation_id=%s",
            (evaluation_id,),
        ).fetchone()
        assert row[1]["run_id"] == evaluation_id
        assert row[2] == provenance.value_hash(metrics)
        with (
            pytest.raises(Exception, match="immutable execution provenance"),
            conn.transaction(),
        ):
            conn.execute(
                "UPDATE benchmark_evaluation_provenance SET result_sha256=%s WHERE evaluation_id=%s",
                ("0" * 64, evaluation_id),
            )


@pytest.mark.parametrize(
    "change",
    ["source", "lock", "result", "manifest", "reference", "path", "asset", "version"],
)
async def test_bundle_rejects_corruption(benchmark_factory, dsn, change):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    await execute_trial(ids[0], dsn)
    with connect(dsn) as c:
        data = bundle.export_trial(c, ids[0])
    bad = copy.deepcopy(data)
    if change == "source":
        bad["source_files"]["engine/uv.lock"] = "AA=="
    elif change == "lock":
        bad["source"]["files"]["engine/uv.lock"] = "0" * 64
    elif change == "result":
        bad["evidence"]["result"]["verdict"] = "FAIL"
    elif change == "manifest":
        bad["execution_sha256"] = "0" * 64
    elif change == "reference":
        bad["evidence"]["result"]["provenance"]["attempt"]["execution_sha256"] = (
            "0" * 64
        )
        bad["evidence_sha256"] = provenance.value_hash(bad["evidence"])
        bad["result_sha256"] = provenance.value_hash(bad["evidence"]["result"])
    elif change == "path":
        bad["source"]["files"]["../escape"] = "0" * 64
        bad["source_files"]["../escape"] = "AA=="
        bad["source"]["sha256"] = provenance.value_hash(bad["source"]["files"])
        bad["execution"]["source_sha256"] = bad["source"]["sha256"]
        bad["execution_sha256"] = provenance.value_hash(bad["execution"])
    elif change == "asset":
        bad["execution"]["asset_bytes"]["agent"] = "AA=="
    else:
        bad["schema_version"] = "unknown"
    with pytest.raises((ValueError, KeyError)):
        bundle.validate_bundle(bad)


async def test_historical_result_has_explicit_missing_provenance(
    benchmark_factory, dsn
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with connect(dsn) as c:
        assert provenance.verify_binding(c, ids[0], "benchmark") is None
        with pytest.raises(ValueError, match="historical"):
            bundle.export_trial(c, ids[0])


def test_duplicate_and_sensitive_bundle_rejected(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text('{"schema_version":"x","schema_version":"y"}')
    with pytest.raises(ValueError, match="duplicate"):
        bundle.load(path)
    path.write_text("gsk_" + "A" * 20)
    with pytest.raises(ValueError, match="credential"):
        bundle.load(path)
    path.write_bytes(b"a" * (bundle.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="oversized"):
        bundle.load(path)


async def test_legacy_v1_execution_has_sidecar_without_changing_result(new_run, dsn):
    from agentproof.contracts import EvaluationResult
    from agentproof.execution import execute

    m = new_run("safe")
    result = await execute(m.run_id, dsn)
    assert EvaluationResult.model_validate(result).status == "passed"
    with connect(dsn) as c:
        assert provenance.verify_binding(c, m.run_id, "run") is True
        row = c.execute(
            "SELECT sha256,manifest,result_sha256 FROM run_execution_provenance WHERE run_id=%s",
            (m.run_id,),
        ).fetchone()
        assert row[1]["evaluator"]["sha256"] == m.evaluator.sha256
        assert row[2] == provenance.value_hash(result)


@pytest.mark.parametrize("version", [2, 3, 4])
async def test_new_archived_benchmark_replays_with_its_own_evaluator(
    benchmark_factory, dsn, version
):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=version)
    result = await execute_trial(ids[0], dsn)
    with connect(dsn) as c:
        data = bundle.export_trial(c, ids[0])
    assert data["evidence"]["run_manifest"]["evaluator"]["version"] == max(
        1, version - 1
    )
    assert bundle.replay(data)["verdict"] == result["verdict"]


def test_effective_model_settings_are_resolved_without_credentials():
    from test_llm import configuration

    cfg = configuration(model="openai/gpt-oss-20b")
    value = provenance.effective_settings(cfg)
    assert value["model"] == "openai/gpt-oss-20b"
    assert value["base_url"] == cfg.model_configuration.base_url
    assert value["request_timeout_seconds"] == cfg.model_configuration.timeout_seconds
    assert value["generation"]["parallel_tool_calls"] is False
    assert (
        "temperature" not in value["generation"] and "top_p" not in value["generation"]
    )
    assert "api_key_env" not in json.dumps(value)


def test_git_identity_is_optional_when_worker_image_has_no_git(monkeypatch):
    def missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git is not installed")

    monkeypatch.setattr(provenance.subprocess, "run", missing_git)
    assert provenance.git_identity() == {
        "commit": None,
        "dirty_or_uncommitted": True,
    }


async def test_terminal_result_without_digest_is_not_verified(
    benchmark_factory, dsn, monkeypatch
):
    from agentproof.benchmark import execution

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    with monkeypatch.context() as patched:
        patched.setattr(execution.execution_provenance, "bind_result", lambda *_: None)
        assert (await execute_trial(ids[0], dsn))["verdict"] == "PASS"
    with connect(dsn) as conn:
        assert provenance.verify_binding(conn, ids[0], "benchmark") is False
        with pytest.raises(ValueError, match="damaged provenance"):
            bundle.export_trial(conn, ids[0])


async def test_replay_requires_matching_installed_source_and_evaluator(
    benchmark_factory, dsn, monkeypatch, tmp_path
):
    from agentproof.artifacts import inventory

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    await execute_trial(ids[0], dsn)
    with connect(dsn) as c:
        data = bundle.export_trial(c, ids[0])
    bad = copy.deepcopy(data)
    bad["evidence"]["run_manifest"]["evaluator"]["content"]["implementation"] = (
        "missing-v9"
    )
    bad["evidence_sha256"] = provenance.value_hash(bad["evidence"])
    with pytest.raises(ValueError):
        bundle.validate_bundle(bad)
    source = tmp_path / "isolated"
    source.mkdir()
    for rel in inventory():
        target = source / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((provenance.ROOT / rel).read_bytes())
    (source / "engine/uv.lock").write_text("changed lock")
    with pytest.raises(ValueError, match="installed source"):
        bundle.replay(data, root=source)
    (source / "engine/uv.lock").unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        bundle.replay(data, root=source)


async def test_replay_names_missing_evaluator_version(benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    await execute_trial(ids[0], dsn)
    with connect(dsn) as conn:
        altered = bundle.export_trial(conn, ids[0])
    evaluator = altered["evidence"]["run_manifest"]["evaluator"]
    evaluator["version"] = 99
    evaluator["content"]["version"] = 99
    raw = provenance.canonical(evaluator["content"])
    evaluator["sha256"] = provenance.digest(raw)
    altered["execution"]["asset_bytes"]["evaluator"] = base64.b64encode(raw).decode()
    altered["execution"]["evaluator"] = provenance.ref(evaluator)
    altered["execution_sha256"] = provenance.value_hash(altered["execution"])
    altered["evidence_sha256"] = provenance.value_hash(altered["evidence"])
    with pytest.raises(ValueError, match="version 99 unavailable"):
        bundle.validate_bundle(altered)


async def test_interrupted_result_binding_recovers_without_duplicate_effect(
    benchmark_factory, dsn, monkeypatch
):
    from agentproof.benchmark import execution

    _, ids, _ = benchmark_factory(ids=["cs-001"], version=5)
    original = execution.execution_provenance.bind_result

    def interrupted(*args):
        raise RuntimeError("simulated interrupted persistence")

    with monkeypatch.context() as m:
        m.setattr(execution.execution_provenance, "bind_result", interrupted)
        with pytest.raises(RuntimeError, match="interrupted persistence"):
            await execute_trial(ids[0], dsn)
    with connect(dsn) as c:
        assert (
            c.execute(
                "SELECT result FROM benchmark_trials WHERE id=%s", (ids[0],)
            ).fetchone()[0]
            is None
        )
        assert (
            c.execute(
                "SELECT count(*) FROM benchmark_effects WHERE trial_id=%s", (ids[0],)
            ).fetchone()[0]
            == 1
        )
    assert execution.execution_provenance.bind_result is original
    assert (await execute_trial(ids[0], dsn))["verdict"] == "PASS"
    with connect(dsn) as c:
        assert provenance.verify_binding(c, ids[0], "benchmark") is True
        assert (
            c.execute(
                "SELECT count(*) FROM benchmark_effects WHERE trial_id=%s", (ids[0],)
            ).fetchone()[0]
            == 1
        )


def test_export_and_build_context_exclude_synthetic_secret(tmp_path):
    from agentproof.artifacts import inventory

    root = tmp_path / "source"
    root.mkdir()
    for rel in inventory():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((provenance.ROOT / rel).read_bytes())
    (root / ".env").write_text("GROQ_API_KEY=gsk_" + "A" * 20)
    assert ".env" not in inventory(root)
    assert ".env\n" in (root / ".dockerignore").read_text()
    provenance.safe_source(root)
    target = root / "engine/agentproof/agent.py"
    target.write_text("gsk_" + "B" * 20)
    with pytest.raises(ValueError, match="credential-shaped"):
        provenance.safe_source(root)
