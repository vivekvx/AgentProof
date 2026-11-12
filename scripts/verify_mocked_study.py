"""Run the eight-slot study through the real local API, worker, and mock provider."""

import hashlib
import json
import os
import re
import secrets
import signal
import socket
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
from agentproof.database import migrate

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".local/verification/mock-study-config"
CONFIG.mkdir(parents=True, exist_ok=True)
PORT = 18080
API_PORT = 18081
FAULT = os.getenv("AGENTPROOF_STUDY_MOCK_FAULT", "none")
TELEMETRY_FAULT = os.getenv("AGENTPROOF_STUDY_MOCK_TELEMETRY_FAULT", "none")
if TELEMETRY_FAULT not in {"none", "dead_endpoint", "collector_outage"}:
    raise SystemExit("Unsupported local telemetry fault")
if FAULT not in {
    "none",
    "restart_api_worker",
    "provider_rate_limit",
    "provider_timeout",
    "cancel",
    "cancel_after_one",
    "deadline",
    "api_temporal",
    "postgres",
    "restart_inflight",
}:
    raise SystemExit("Unsupported local study fault")
DB_NAME = os.getenv("AGENTPROOF_STUDY_TEST_DB", "agentproof_study_test")
if not re.fullmatch(
    r"agentproof_study_test_[a-z0-9]{1,24}|agentproof_study_test", DB_NAME
):
    raise SystemExit("Use a dedicated AgentProof study test database name")
ADMIN = "postgresql://agentproof:agentproof@127.0.0.1:55433/postgres"
DSN = f"postgresql://agentproof:agentproof@127.0.0.1:55433/{DB_NAME}"


def write_json(path, value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


with psycopg.connect(ADMIN, autocommit=True) as connection:
    if connection.execute(
        "SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,)
    ).fetchone():
        raise SystemExit(
            "Dedicated mocked-study database already exists; preserve its evidence"
        )
    connection.execute(f"CREATE DATABASE {DB_NAME}")
migrate(DSN)
with psycopg.connect(DSN) as connection:
    database_bytes_before = connection.execute(
        "SELECT pg_database_size(current_database())"
    ).fetchone()[0]

plan = json.loads(
    (ROOT / "packages/contracts/experiments/groq-gpt-oss-matched.v1.json").read_text()
)
plan["id"] = "agentproof-mocked-t16"
prompt = (
    "Complete the customer cancellation with approved tools. "
    "After an uncertain mutation, read the order state before deciding."
)
plan["prompt_version"] = "mocked-t16-v1"
plan["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
plan["effective_settings"]["base_url"] = f"http://127.0.0.1:{PORT}/v1"
for index, label in enumerate(("20b", "120b")):
    profile_id = f"mocked-t16-{label}"
    model = f"mocked-{label}"
    profile = {
        "schema_version": "2.0",
        "id": profile_id,
        "version": 1,
        "title": f"Synthetic {label} provider fixture",
        "policy": "llm",
        "model_configuration": {
            "schema_version": "2.0",
            "provider": "openai-compatible",
            "model": model,
            "base_url": f"http://127.0.0.1:{PORT}/v1",
            "api_key_env": "AGENTPROOF_TEST_KEY",
            "tool_calling": True,
            "timeout_seconds": 3.0,
            "temperature": 0.0,
            "top_p": 1.0,
            "max_output_tokens": 1024,
            "max_requests": 6,
            "max_tool_calls": 10,
            "max_tokens": 64000,
            "max_estimated_cost_usd": 0.1,
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 2.0,
            "pricing_source": "https://example.com/synthetic-pricing",
            "pricing_checked_at": "2026-11-11",
        },
        "prompt": prompt,
        "prompt_version": plan["prompt_version"],
        "tool_schema_version": "support-tools-v1",
        "max_retries": 0,
    }
    digest = write_json(CONFIG / f"{profile_id}.v1.json", profile)
    plan["profiles"][index] = {
        "id": profile_id,
        "version": 1,
        "model": model,
        "sha256": digest,
    }
write_json(CONFIG / "mocked-plan.v1.json", plan)

requests = []
requests_lock = threading.Lock()
dispatched_at_interruption = None
provider_fault_delivered = False
admission_observed = False
fault_started_at = None
inflight_seen = threading.Event()
inflight_release = threading.Event()
compose = [
    "docker",
    "compose",
    "--env-file",
    "infra/verification.env",
    "-p",
    "agentproof-verification",
]
service_stopped = None


class MockProvider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        global provider_fault_delivered, admission_observed, fault_started_at
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > 65536:
            self.send_error(400)
            return
        body = json.loads(self.rfile.read(size))
        names = [x["function"]["name"] for x in body.get("tools", [])]
        messages = body.get("messages", [])
        with requests_lock:
            requests.append({"model": body.get("model"), "tools": names})
            number = len(requests)
            deliver_fault = (
                FAULT in {"provider_rate_limit", "provider_timeout", "restart_inflight"}
                and "capability_probe" not in names
                and not provider_fault_delivered
            )
            if deliver_fault:
                provider_fault_delivered = True
                fault_started_at = time.monotonic()
        if number == 1:
            with psycopg.connect(DSN) as connection:
                admission_observed = connection.execute(
                    "SELECT count(*) FROM benchmark_study_trials"
                ).fetchone() == (8,)
            if not admission_observed:
                self.send_error(503)
                return
        if deliver_fault and FAULT == "provider_rate_limit":
            response = (
                b'{"error":{"message":"synthetic quota","type":"rate_limit_error"}}'
            )
            self.send_response(429)
            self.send_header("Retry-After", "60")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        if deliver_fault and FAULT == "provider_timeout":
            time.sleep(4)
            return
        if deliver_fault and FAULT == "restart_inflight":
            inflight_seen.set()
            if not inflight_release.wait(30):
                return
        if FAULT in {
            "restart_api_worker",
            "cancel",
            "cancel_after_one",
            "deadline",
            "api_temporal",
            "postgres",
        }:
            time.sleep(0.08)
        if "capability_probe" in names:
            tool = "capability_probe"
        else:
            last_tool = next(
                (m for m in reversed(messages) if m.get("role") == "tool"), None
            )
            if last_tool is None:
                tool = "get_order"
            else:
                content = str(last_tool.get("content", ""))
                previous = next(
                    (m for m in reversed(messages) if m.get("role") == "assistant"),
                    {},
                )
                previous_tools = previous.get("tool_calls", [])
                previous_name = (
                    previous_tools[0]["function"]["name"] if previous_tools else ""
                )
                if previous_name == "get_order" and "cancelled" in content:
                    tool = None
                elif previous_name == "get_order":
                    tool = "cancel_order"
                else:
                    tool = "get_order"
        if tool:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"mock-{number}",
                        "type": "function",
                        "function": {
                            "name": tool,
                            "arguments": "{}"
                            if tool == "capability_probe"
                            else '{"order_id":"order-001"}',
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": '{"resolution":"completed"}'}
            finish = "stop"
        response = json.dumps(
            {
                "id": f"mock-{number}",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        try:
            self.wfile.write(response)
        except BrokenPipeError:
            pass  # The worker was deliberately killed after provider dispatch.


server = ThreadingHTTPServer(("127.0.0.1", PORT), MockProvider)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    API_PORT = sock.getsockname()[1]
token = secrets.token_urlsafe(36)
trace_id = uuid.uuid4().hex
queue = "agentproof-study-" + uuid.uuid4().hex
env = os.environ.copy()
env.update(
    DATABASE_URL=DSN,
    TEMPORAL_ADDRESS="127.0.0.1:7234",
    AGENTPROOF_TASK_QUEUE=queue,
    AGENTPROOF_MODEL_CONFIG_DIR=str(CONFIG),
    AGENTPROOF_STUDY_PLAN_PATH=str(CONFIG / "mocked-plan.v1.json"),
    AGENTPROOF_ENABLE_STUDY_DISPATCH="1",
    AGENTPROOF_API_TOKEN=token,
    AGENTPROOF_ALLOW_MODEL_REQUESTS="1",
    AGENTPROOF_TEST_KEY="synthetic-local-only",
    OTEL_EXPORTER_OTLP_ENDPOINT=(
        "http://127.0.0.1:1"
        if TELEMETRY_FAULT == "dead_endpoint"
        else os.getenv("AGENTPROOF_STUDY_OTLP_ENDPOINT", "http://127.0.0.1:4319")
    ),
    API_ADDRESS=f"127.0.0.1:{API_PORT}",
)
log_stack = ExitStack()
worker_log = log_stack.enter_context(
    Path("/tmp/agentproof-t16-mock-worker.log").open("w")  # noqa: SIM115
)
api_log = log_stack.enter_context(Path("/tmp/agentproof-t16-mock-api.log").open("w"))  # noqa: SIM115
api_build_dir = tempfile.TemporaryDirectory(prefix="agentproof-t16-api-")
api_binary = Path(api_build_dir.name) / "api"
subprocess.run(
    ["go", "build", "-o", str(api_binary), "./apps/api/cmd/server"],
    cwd=ROOT,
    check=True,
)
worker = subprocess.Popen(
    [
        "uv",
        "run",
        "--project",
        "engine",
        "--frozen",
        "python",
        "-m",
        "agentproof.worker",
    ],
    cwd=ROOT,
    env=env,
    stdout=worker_log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
api = subprocess.Popen(
    [str(api_binary)],
    cwd=ROOT,
    env=env,
    stdout=api_log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)


def stop_process_group(process, sig):
    if process.poll() is None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


samples = []
sampling_stop = threading.Event()


def process_tree(root, processes):
    active = {root}
    while True:
        expanded = active | {
            pid for pid, values in processes.items() if values[0] in active
        }
        if expanded == active:
            break
        active = expanded
    return (
        sum(processes[pid][1] for pid in active if pid in processes) / 1024,
        sum(processes[pid][2] for pid in active if pid in processes),
    )


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def quantiles(values):
    known = [float(value) for value in values if value is not None]
    if not known:
        return None
    return [round(statistics.median(known), 2), round(percentile(known, 0.95), 2)]


def sample_resources():
    while not sampling_stop.is_set():
        try:
            lines = subprocess.check_output(
                ["ps", "-A", "-o", "pid=,ppid=,rss=,%cpu="], text=True
            ).splitlines()
            processes = {}
            for line in lines:
                pid, parent, rss, cpu = line.split()
                processes[int(pid)] = (int(parent), int(rss), float(cpu))

            api_rss, api_cpu = process_tree(api.pid, processes)
            worker_rss, worker_cpu = process_tree(worker.pid, processes)
            with psycopg.connect(DSN) as connection:
                db_connections = connection.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
                ).fetchone()[0]
            samples.append((api_rss, api_cpu, worker_rss, worker_cpu, db_connections))
        except (OSError, ValueError, psycopg.Error):
            pass  # A selected outage can make one resource sample unavailable.
        sampling_stop.wait(0.1)


sampling_thread = threading.Thread(target=sample_resources, daemon=True)
sampling_thread.start()


def http(path, method="GET", payload=None, key=None):
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "traceparent": f"00-{trace_id}-1234567890abcdef-01",
    }
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        f"http://127.0.0.1:{API_PORT}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code < 500:
            raise
        raise AssertionError(
            f"synthetic API {path} returned {error.code}: {error.read(512)!r}"
        ) from error


try:
    for _ in range(100):
        try:
            http("/healthz")
            break
        except (urllib.error.URLError, TimeoutError):
            if api.poll() is not None:
                raise RuntimeError("study API stopped before health")
            time.sleep(0.1)
    else:
        raise RuntimeError("study API did not become healthy")
    key = "mock-study-" + uuid.uuid4().hex
    study_started = time.monotonic()
    request = {"schema_version": "study-create-v1", "plan_id": plan["id"]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        creates = list(
            pool.map(lambda _: http("/api/v2/studies", "POST", request, key), range(4))
        )
    first = creates[0]
    assert all(item["id"] == first["id"] for item in creates)
    try:
        http("/api/v2/studies", "POST", request, key + "-different")
        raise AssertionError("same frozen plan admitted as another study")
    except urllib.error.HTTPError as error:
        assert error.code == 409
    with psycopg.connect(DSN) as connection:
        assert connection.execute(
            "SELECT count(*) FROM benchmark_study_trials WHERE study_id=%s",
            (first["id"],),
        ).fetchone() == (8,)
    if FAULT in {
        "restart_api_worker",
        "cancel",
        "cancel_after_one",
        "deadline",
        "api_temporal",
        "postgres",
        "restart_inflight",
    }:
        for _ in range(200):
            with requests_lock:
                dispatched = len(requests)
            if FAULT == "cancel_after_one":
                with psycopg.connect(DSN) as connection:
                    completed_before_cancel = connection.execute(
                        "SELECT count(*) FROM benchmark_trials t JOIN benchmark_study_trials b ON b.trial_id=t.id WHERE b.study_id=%s AND t.result->>'verdict'='PASS'",
                        (first["id"],),
                    ).fetchone()[0]
                if completed_before_cancel >= 1:
                    break
            elif (
                inflight_seen.is_set()
                if FAULT == "restart_inflight"
                else dispatched >= 5
            ):
                break
            time.sleep(0.02)
        assert dispatched >= (2 if FAULT == "restart_inflight" else 5), (
            "worker did not dispatch before interruption"
        )
        assert dispatched < 40, "study finished before worker interruption"
        dispatched_at_interruption = dispatched
        fault_started_at = time.monotonic()
        if FAULT in {"cancel", "cancel_after_one"}:
            cancelled = http("/api/v2/studies/" + first["id"] + "/cancel", "POST")
            assert cancelled["status"] == "cancelled"
        elif FAULT == "deadline":
            with psycopg.connect(DSN) as connection:
                connection.execute(
                    "UPDATE benchmark_study_budgets SET created_at=clock_timestamp()-interval '181 seconds' WHERE study_id=%s",
                    (first["id"],),
                )
        elif FAULT in {"api_temporal", "postgres"}:
            if FAULT == "api_temporal":
                stop_process_group(api, signal.SIGKILL)
                service_stopped = "temporal"
            else:
                service_stopped = "postgres"
            subprocess.run(
                compose + ["stop", service_stopped],
                check=True,
                stdout=subprocess.DEVNULL,
                env=env,
            )
            time.sleep(1)
            subprocess.run(
                compose + ["up", "-d", "--wait", service_stopped],
                check=True,
                stdout=subprocess.DEVNULL,
                env=env,
            )
            service_stopped = None
            if api.poll() is not None:
                api = subprocess.Popen(
                    [str(api_binary)],
                    cwd=ROOT,
                    env=env,
                    stdout=api_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            if worker.poll() is not None:
                worker = subprocess.Popen(
                    [
                        "uv",
                        "run",
                        "--project",
                        "engine",
                        "--frozen",
                        "python",
                        "-m",
                        "agentproof.worker",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        elif FAULT == "restart_inflight":
            assert inflight_seen.is_set(), "mock provider did not hold a request"
            stop_process_group(worker, signal.SIGKILL)
            inflight_release.set()
            worker = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "--project",
                    "engine",
                    "--frozen",
                    "python",
                    "-m",
                    "agentproof.worker",
                ],
                cwd=ROOT,
                env=env,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        else:
            stop_process_group(api, signal.SIGKILL)
            stop_process_group(worker, signal.SIGKILL)
            api = subprocess.Popen(
                [str(api_binary)],
                cwd=ROOT,
                env=env,
                stdout=api_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            worker = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "--project",
                    "engine",
                    "--frozen",
                    "python",
                    "-m",
                    "agentproof.worker",
                ],
                cwd=ROOT,
                env=env,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    if FAULT in {"restart_api_worker", "api_temporal"}:
        for _ in range(100):
            try:
                retry = http("/api/v2/studies", "POST", request, key)
                break
            except (urllib.error.URLError, TimeoutError):
                if api.poll() is not None:
                    raise RuntimeError("restarted study API stopped before retry")
                time.sleep(0.1)
        else:
            raise RuntimeError("restarted study API did not accept retry")
        assert retry["id"] == first["id"]
        assert retry["evaluation_ids"] == first["evaluation_ids"]
    for _ in range(600):
        try:
            result = http("/api/v2/studies/" + first["id"])
        except (urllib.error.URLError, TimeoutError):
            if api.poll() is not None:
                raise RuntimeError("study API stopped during recovery") from None
            time.sleep(0.1)
            continue
        if result["completed_trials"] == 8 and result["pending_evaluations"] == 0:
            break
        time.sleep(0.1)
    assert result["status"] == (
        "cancelled" if FAULT in {"cancel", "cancel_after_one"} else "completed"
    ), result
    assert result["planned_slots"] == result["completed_trials"] == 8
    retried = http("/api/v2/studies", "POST", request, key)
    assert retried["id"] == first["id"] and retried["status"] == result["status"]
    assert set(retried["evaluation_ids"]) == set(first["evaluation_ids"])
    if FAULT not in {"cancel", "cancel_after_one"}:
        try:
            http("/api/v2/studies/" + first["id"] + "/cancel", "POST")
            raise AssertionError("completed study accepted cancellation")
        except urllib.error.HTTPError as error:
            assert error.code == 409
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{API_PORT}/api/v2/studies/{first['id']}"
        )
        raise AssertionError("unauthenticated study read was accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 401
    with psycopg.connect(DSN) as connection:
        rows = connection.execute(
            "SELECT state,count(*) FROM benchmark_study_requests r JOIN benchmark_study_trials t ON t.trial_id=r.trial_id WHERE t.study_id=%s GROUP BY state",
            (first["id"],),
        ).fetchall()
        verdicts = connection.execute(
            "SELECT result->>'verdict',count(*) FROM benchmark_trials t JOIN benchmark_study_trials b ON b.trial_id=t.id WHERE b.study_id=%s GROUP BY 1",
            (first["id"],),
        ).fetchall()
        categories = connection.execute(
            "SELECT result->>'failure_category',count(*) FROM benchmark_trials t JOIN benchmark_study_trials b ON b.trial_id=t.id WHERE b.study_id=%s GROUP BY 1",
            (first["id"],),
        ).fetchall()
        effects = connection.execute(
            "SELECT trial_id,count(*) FROM benchmark_effects e JOIN benchmark_study_trials t USING (trial_id) WHERE t.study_id=%s GROUP BY trial_id",
            (first["id"],),
        ).fetchall()
        usage = connection.execute(
            "SELECT count(*),sum(observed_tokens),sum(estimated_cost_usd),count(*) FILTER (WHERE state='unknown') FROM benchmark_study_requests r JOIN benchmark_study_trials t USING (trial_id) WHERE t.study_id=%s",
            (first["id"],),
        ).fetchone()
        durations = connection.execute(
            "SELECT extract(epoch FROM (t.started_at-e.created_at))*1000,extract(epoch FROM (t.completed_at-t.started_at))*1000,extract(epoch FROM (t.completed_at-e.created_at))*1000 FROM benchmark_trials t JOIN benchmark_evaluations e ON e.id=t.evaluation_id JOIN benchmark_study_trials b ON b.trial_id=t.id WHERE b.study_id=%s",
            (first["id"],),
        ).fetchall()
        database_bytes_after = connection.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
        assert all(state != "reserved" for state, _ in rows)
        cooldown = connection.execute(
            "SELECT provider_not_before > clock_timestamp() FROM benchmark_study_budgets WHERE study_id=%s",
            (first["id"],),
        ).fetchone()[0]
        assert len(effects) <= 8 and all(count == 1 for _, count in effects)
        assert usage[0] <= 48 and usage[1] <= 512000 and usage[2] <= 0.8
        assert sum(count for _, count in verdicts) == 8
        if FAULT == "none":
            assert result["pass"] == 8 and len(effects) == 8
        elif FAULT in {"restart_api_worker", "api_temporal"}:
            assert result["fail"] == 0
            assert all(
                category in {None, "infrastructure"} for category, _ in categories
            )
        elif FAULT == "postgres":
            assert result["fail"] == 0 and result["indeterminate"] >= 1
            assert len(effects) == 8
            assert all(
                category in {None, "infrastructure"} for category, _ in categories
            )
        elif FAULT == "restart_inflight":
            assert result["fail"] == 0 and result["indeterminate"] >= 1
            assert usage[3] >= 1 and len(requests) < 40
            assert all(
                category in {None, "infrastructure"} for category, _ in categories
            )
        elif FAULT in {"cancel", "cancel_after_one", "deadline"}:
            assert result["indeterminate"] >= 1 and result["fail"] == 0
            if FAULT == "cancel_after_one":
                assert result["pass"] >= 1 and len(effects) >= 1
        else:
            assert provider_fault_delivered and result["indeterminate"] >= 1
            assert usage[3] >= 1 and result["pass"] < 8
        if FAULT == "provider_rate_limit":
            assert cooldown is True
        assert connection.execute(
            "SELECT count(*) FROM benchmark_study_trials WHERE study_id=%s",
            (first["id"],),
        ).fetchone() == (8,)
    trace_services = set()
    trace_spans = []
    assert admission_observed, "provider dispatch preceded eight-slot admission"
    if TELEMETRY_FAULT == "none":
        for _ in range(50):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:16687/api/traces/{trace_id}", timeout=2
                ) as response:
                    traces = json.load(response).get("data", [])
                if traces:
                    processes = traces[0].get("processes", {})
                    trace_spans = traces[0].get("spans", [])
                    trace_services = {
                        processes.get(span.get("processID"), {}).get("serviceName")
                        for span in trace_spans
                    }
                    if {
                        "agentproof-api",
                        "agentproof-worker",
                    } <= trace_services and any(
                        span.get("operationName") == "benchmark.trial"
                        for span in trace_spans
                    ):
                        break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.2)
        assert {"agentproof-api", "agentproof-worker"} <= trace_services
        trial_spans = [
            span
            for span in trace_spans
            if span.get("operationName") == "benchmark.trial"
        ]
        assert trial_spans, "trace has no worker trial span"
        trial_tags = {
            tag["key"]: tag.get("value") for tag in trial_spans[0].get("tags", [])
        }
        assert trial_tags.get("agentproof.run_id") in first["evaluation_ids"]
        assert trial_tags.get("agentproof.scenario.id") in plan["scenario_ids"]
        assert isinstance(trial_tags.get("temporal.activity.attempt"), int)
        assert trial_tags["temporal.activity.attempt"] >= 1
    print(
        json.dumps(
            {
                "study_id": first["id"],
                "trace_id": trace_id,
                "trace_services": sorted(trace_services),
                "telemetry_degraded": TELEMETRY_FAULT != "none",
                "planned_slots": 8,
                "status": result["status"],
                "pass": result["pass"],
                "fail": result["fail"],
                "indeterminate": result["indeterminate"],
                "provider_dispatches": len(requests),
                "dispatches_at_interruption": dispatched_at_interruption,
                "fault": FAULT,
                "usage_tokens": int(usage[1] or 0),
                "estimated_cost_usd": str(usage[2]),
                "unknown_request_count": usage[3],
                "shared_cooldown_active": cooldown,
                "effect_trials": len(effects),
                "request_states": rows,
                "verdicts": verdicts,
                "failure_categories": categories,
                "resource_samples": len(samples),
                "sampled_peak_api_rss_mib": round(
                    max((v[0] for v in samples), default=0), 2
                ),
                "sampled_peak_api_cpu_percent": round(
                    max((v[1] for v in samples), default=0), 2
                ),
                "sampled_peak_worker_rss_mib": round(
                    max((v[2] for v in samples), default=0), 2
                ),
                "sampled_peak_worker_cpu_percent": round(
                    max((v[3] for v in samples), default=0), 2
                ),
                "sampled_peak_db_connections": max((v[4] for v in samples), default=0),
                "sampled_api_rss_mib_p50_p95": quantiles(v[0] for v in samples),
                "sampled_api_cpu_percent_p50_p95": quantiles(v[1] for v in samples),
                "sampled_worker_rss_mib_p50_p95": quantiles(v[2] for v in samples),
                "sampled_worker_cpu_percent_p50_p95": quantiles(v[3] for v in samples),
                "sampled_db_connections_p50_p95": quantiles(v[4] for v in samples),
                "sampled_median_worker_rss_mib": round(
                    statistics.median(v[2] for v in samples), 2
                )
                if samples
                else None,
                "queue_wait_ms_p50_p95": quantiles(row[0] for row in durations),
                "active_trial_ms_p50_p95": quantiles(row[1] for row in durations),
                "end_to_end_trial_ms_p50_p95": quantiles(row[2] for row in durations),
                "study_wall_seconds": round(time.monotonic() - study_started, 3),
                "recovery_seconds": round(time.monotonic() - fault_started_at, 3)
                if fault_started_at is not None
                else None,
                "database_growth_bytes": database_bytes_after - database_bytes_before,
            },
            sort_keys=True,
        )
    )
finally:
    sampling_stop.set()
    sampling_thread.join(timeout=2)
    if service_stopped is not None:
        subprocess.run(
            compose + ["up", "-d", "--wait", service_stopped],
            check=True,
            stdout=subprocess.DEVNULL,
            env=env,
        )
    stop_process_group(api, signal.SIGTERM)
    stop_process_group(worker, signal.SIGTERM)
    inflight_release.set()
    log_stack.close()
    server.shutdown()
    api_build_dir.cleanup()
