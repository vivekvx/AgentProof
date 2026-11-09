import hashlib
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from agentproof.benchmark.registry import reference
from agentproof.contracts import Manifest
from agentproof.database import migrate
from agentproof.telemetry import configure
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg.types.json import Jsonb
from pydantic_ai import models

models.ALLOW_MODEL_REQUESTS = False
ROOT = Path(__file__).resolve().parents[2]
provider = configure()

exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))


@pytest.fixture
def dsn():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated test database")
    migrate(value)
    return value


def manifest(agent="safe"):
    def ref(path):
        raw = (ROOT / "packages/contracts" / path).read_bytes()
        content = json.loads(raw)
        return {
            "id": content["id"],
            "version": content["version"],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": content,
        }

    return Manifest(
        run_id=str(uuid.uuid4()),
        agent=agent,
        agent_version="reference-v1",
        scenario=ref("scenarios/cancel-order.v1.json"),
        evaluator=ref("evaluators/cancellation.v1.json"),
        traceparent="00-12345678901234567890123456789012-1234567890123456-01",
    )


@pytest.fixture
def new_run(dsn):
    ids = []

    def create(agent="safe"):
        m = manifest(agent)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO runs(id,manifest) VALUES (%s,%s)",
                (m.run_id, Jsonb(m.model_dump())),
            )
        ids.append(m.run_id)
        return m

    yield create
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('agentproof.test_cleanup','on',false)")
        for run_id in ids:
            conn.execute(
                "DELETE FROM run_execution_provenance WHERE run_id=%s", (run_id,)
            )
            for table in [
                "run_requests",
                "run_attempts",
                "run_failures",
                "side_effects",
                "tool_events",
                "orders",
            ]:
                conn.execute(f"DELETE FROM {table} WHERE run_id=%s", (run_id,))
            conn.execute("DELETE FROM runs WHERE id=%s", (run_id,))


@pytest.fixture
def benchmark_factory(dsn):
    created = []

    def create(agent="support-safe", ids=None, trials=1, version=4, agent_config=None):
        evaluation_id = str(uuid.uuid4())
        ds = reference(f"benchmarks/support.v{version}.json")
        cfg = (
            reference(f"agents/{agent}.v1.json")
            if agent_config is None
            else {
                "id": agent_config["id"],
                "version": agent_config["version"],
                "sha256": hashlib.sha256(
                    json.dumps(
                        agent_config, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
                "content": agent_config,
            }
        )
        cases = [s for s in ds["content"]["scenarios"] if ids is None or s["id"] in ids]
        manifest = {
            "schema_version": "3.0" if version == 6 else "2.0",
            "id": evaluation_id,
            "dataset": ds,
            "agent": cfg,
            "evaluator": reference(f"evaluators/support.v{max(1, version - 1)}.json"),
            "tool_schema_version": "support-tools-v2"
            if version == 6
            else "support-tools-v1",
            "tool_schema": reference(
                "tools/support-tools.v2.json"
                if version == 6
                else "tools/support-tools.v1.json"
            ),
            "split": "all",
            "trials": trials,
            "scenario_ids": [s["id"] for s in cases],
            "traceparent": "00-12345678901234567890123456789012-1234567890123456-01",
        }
        trial_ids = []
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO benchmark_evaluations(id,manifest) VALUES(%s,%s)",
                (evaluation_id, Jsonb(manifest)),
            )
            for sc in cases:
                for n in range(1, trials + 1):
                    trial_id = str(
                        uuid.uuid5(uuid.UUID(evaluation_id), f"{sc['id']}:{n}")
                    )
                    conn.execute(
                        "INSERT INTO benchmark_trials(id,evaluation_id,scenario_id,trial_index,scenario,state) VALUES(%s,%s,%s,%s,%s,%s)",
                        (
                            trial_id,
                            evaluation_id,
                            sc["id"],
                            n,
                            Jsonb(sc),
                            Jsonb(sc["fixture"]["order"]),
                        ),
                    )
                    if version >= 5:
                        conn.execute(
                            "INSERT INTO benchmark_evidence_heads(trial_id) VALUES(%s)",
                            (trial_id,),
                        )
                    trial_ids.append(trial_id)
        created.append(evaluation_id)
        return evaluation_id, trial_ids, manifest

    yield create
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('agentproof.test_cleanup','on',false)")
        for evaluation_id in created:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM benchmark_trials WHERE evaluation_id=%s",
                    (evaluation_id,),
                )
            ]
            for trial_id in ids:
                conn.execute(
                    "DELETE FROM benchmark_external_sessions WHERE trial_id=%s",
                    (trial_id,),
                )
                conn.execute(
                    "DELETE FROM benchmark_execution_provenance WHERE trial_id=%s",
                    (trial_id,),
                )
                for table in [
                    "benchmark_broker_rejections",
                    "benchmark_receipts",
                    "benchmark_evidence_heads",
                    "benchmark_model_calls",
                    "benchmark_attempts",
                    "benchmark_effects",
                    "benchmark_events",
                ]:
                    conn.execute(f"DELETE FROM {table} WHERE trial_id=%s", (trial_id,))
            conn.execute(
                "DELETE FROM benchmark_trials WHERE evaluation_id=%s", (evaluation_id,)
            )
            conn.execute(
                "DELETE FROM benchmark_requests WHERE evaluation_id=%s",
                (evaluation_id,),
            )
            conn.execute(
                "DELETE FROM benchmark_evaluation_provenance WHERE evaluation_id=%s",
                (evaluation_id,),
            )
            conn.execute(
                "DELETE FROM benchmark_evaluations WHERE id=%s", (evaluation_id,)
            )
