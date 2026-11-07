import asyncio
import json

import jsonschema
import psycopg
import pytest
from agentproof.contracts import EvaluationResult, Manifest, Scenario, ToolEvent
from agentproof.execution import (
    execute,
    record_infrastructure_failure,
    validate_references,
)
from agentproof.simulator import OrderSimulator
from conftest import ROOT, exporter, manifest
from pydantic import ValidationError
from pydantic_ai import models


@pytest.mark.parametrize(
    "agent,expected,count", [("safe", "passed", 1), ("duplicate", "failed", 2)]
)
async def test_final_database_state(dsn, new_run, agent, expected, count):
    m = new_run(agent)
    result = await execute(m.run_id, dsn)
    assert result["status"] == expected
    assert result["failure_kind"] == ("agent" if agent == "duplicate" else None)
    assert result["side_effect_count"] == count
    assert result["final_state"]["status"] == "cancelled"
    assert result["trace_id"] == "12345678901234567890123456789012"
    with psycopg.connect(dsn) as conn:
        events = [
            ToolEvent.model_validate(row[0])
            for row in conn.execute(
                "SELECT payload FROM tool_events WHERE run_id=%s ORDER BY sequence",
                (m.run_id,),
            )
        ]
        assert len(events) == 2
        assert events[0].outcome == "timeout_after_commit"
        assert events[0].side_effect_committed
        assert "status" not in events[0].output
        assert events[0].span_id != "0" * 16
        spans = exporter.get_finished_spans()
        assert any(span.name == "evaluation.execute" for span in spans)
        exported_ids = {f"{span.context.span_id:016x}" for span in spans}
        assert all(event.span_id in exported_ids for event in events)
        assert (
            conn.execute(
                "SELECT count(*) FROM side_effects WHERE run_id=%s", (m.run_id,)
            ).fetchone()[0]
            == count
        )
    assert await execute(m.run_id, dsn) == result
    jsonschema.validate(
        result,
        json.loads(
            (ROOT / "packages/contracts/evaluation-result.v1.schema.json").read_text()
        ),
    )


async def test_recovery_after_committed_tool(dsn, new_run, monkeypatch):
    m = new_run()
    original = OrderSimulator.call

    def crash(self, tool, order_id):
        original(self, tool, order_id)
        raise RuntimeError("worker stopped after tool commit")

    monkeypatch.setattr(OrderSimulator, "call", crash)
    with pytest.raises(RuntimeError, match="worker stopped"):
        await execute(m.run_id, dsn)
    monkeypatch.setattr(OrderSimulator, "call", original)
    result = await execute(m.run_id, dsn)
    assert result["status"] == "passed"
    assert result["side_effect_count"] == 1


async def test_concurrent_isolated_runs(dsn, new_run):
    a, b = new_run(), new_run("duplicate")
    results = await asyncio.gather(execute(a.run_id, dsn), execute(b.run_id, dsn))
    assert [r["side_effect_count"] for r in results] == [1, 2]


def test_immutable_references():
    m = manifest()
    validate_references(m)
    changed = m.model_copy(deep=True)
    changed.scenario.content["assertions"]["side_effect_count"] = 2
    with pytest.raises(ValueError, match="immutable"):
        validate_references(changed)


def test_contract_rejects_unknown_fields():
    data = manifest().model_dump()
    data["external_agent_url"] = "https://example.com"
    with pytest.raises(ValidationError):
        Manifest.model_validate(data)


def test_models_disabled():
    assert models.ALLOW_MODEL_REQUESTS is False
    with pytest.raises(RuntimeError):
        models.check_allow_model_requests()


async def test_terminal_immutability(dsn, new_run):
    m = new_run()
    await execute(m.run_id, dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute("UPDATE runs SET status='queued' WHERE id=%s", (m.run_id,))
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute(
                "UPDATE runs SET manifest='{}'::jsonb WHERE id=%s", (m.run_id,)
            )


def test_infrastructure_failure_separate(dsn, new_run, monkeypatch):
    m = new_run()
    monkeypatch.setenv("DATABASE_URL", dsn)
    record_infrastructure_failure(m.run_id)
    with psycopg.connect(dsn) as conn:
        result = conn.execute(
            "SELECT result FROM runs WHERE id=%s", (m.run_id,)
        ).fetchone()[0]
    assert result["status"] == "infrastructure_failed"
    assert result["failure_kind"] == "infrastructure"
    assert result["checks"] == []


@pytest.mark.parametrize(
    "model,name",
    [
        (Manifest, "run-manifest"),
        (EvaluationResult, "evaluation-result"),
        (ToolEvent, "tool-event"),
        (Scenario, "scenario"),
    ],
)
def test_committed_schema_matches_models(model, name):
    assert (
        json.loads((ROOT / f"packages/contracts/{name}.v1.schema.json").read_text())
        == model.model_json_schema()
    )


async def test_same_run_concurrent_recovery(dsn, new_run):
    m = new_run()
    a, b = await asyncio.wait_for(
        asyncio.gather(execute(m.run_id, dsn), execute(m.run_id, dsn)), timeout=20
    )
    assert a == b
    assert a["side_effect_count"] == 1
