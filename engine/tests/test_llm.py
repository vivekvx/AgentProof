"""No real credentials or provider requests are used by this suite."""

import asyncio
import json

import httpx
import pytest
from agentproof.benchmark.contracts import (
    AgentConfiguration,
    ModelConfiguration,
    PublicContext,
)
from agentproof.benchmark.llm import (
    Journal,
    ProviderFailure,
    build_model,
    cost,
    run_llm,
    wire_parameters,
)
from agentproof.benchmark.registry import dataset
from agentproof.benchmark.simulator import AgentLimit, OrderSimulator
from agentproof.database import connect
from pydantic import ValidationError
from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.usage import RequestUsage


def configuration(**overrides):
    model = {
        "provider": "openai-compatible",
        "model": "test-model",
        "base_url": "https://mock.invalid/v1",
        "api_key_env": "AGENTPROOF_TEST_KEY",
        "tool_calling": True,
        "max_requests": 8,
        "max_tokens": 100000,
        "max_estimated_cost_usd": 1.0,
        "input_usd_per_million": 1.0,
        "output_usd_per_million": 2.0,
        "pricing_source": "https://example.org/pricing",
        "pricing_checked_at": "2026-10-31",
    }
    model.update(overrides)
    return AgentConfiguration(
        id="test-real",
        version=1,
        title="Mocked real model",
        policy="llm",
        model_configuration=model,
        prompt="Perform the customer request with authorized order tools.",
        prompt_version="support-prompt-v1",
        tool_schema_version="support-tools-v1",
        max_retries=0,
    )


class MemoryJournal:
    def __init__(self):
        self.rows = {}

    def reserve(self, n):
        row = self.rows.get(n)
        if row:
            if isinstance(row, str):
                raise ProviderFailure(row)
            return row
        self.rows[n] = "interrupted_unknown_usage"

    def complete(self, n, response, metadata):
        self.rows[n] = (response, metadata)

    def fail(self, n, category, metadata=None):
        self.rows[n] = category


class Client:
    async def close(self):
        pass


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    async def request(self, messages, settings, params):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if response == "interrupt":
            await asyncio.sleep(60)
        return response


def response(*parts, usage=True):
    return ModelResponse(
        parts=parts,
        usage=RequestUsage(input_tokens=20, output_tokens=10)
        if usage
        else RequestUsage(),
    )


def probe():
    return response(ToolCallPart("capability_probe", {}))


def final():
    return response(TextPart('{"resolution":"completed"}'))


def public(case=0):
    sc = dataset().scenarios[case]
    return PublicContext(
        request=sc.request.model_copy(deep=True),
        permissions=sc.permissions.model_copy(deep=True),
        limits=sc.limits.model_copy(deep=True),
    )


@pytest.mark.asyncio
async def test_real_tool_calling_replay_and_usage():
    model = FakeModel(
        [
            probe(),
            response(ToolCallPart("cancel_order", {"order_id": "order-100"})),
            final(),
        ]
    )
    journal = MemoryJournal()
    calls = []

    async def tool(name, args):
        calls.append((name, args))
        return {"ok": True}

    for _ in range(2):
        outcome = await run_llm(
            public(),
            configuration(),
            tool,
            journal,
            lambda _, model=model: (model, Client()),
        )
        assert outcome.resolution == "completed"
    assert model.calls == 3
    assert len(calls) == 2  # simulator ledger handles replayed tools
    assert journal.rows[2][1]["input_tokens"] == 20
    assert journal.rows[2][1]["reported_cost_usd"] is None
    assert not models.ALLOW_MODEL_REQUESTS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["timeout", "rate_limit", "authentication", "unsupported"]
)
async def test_provider_errors_sanitized(kind):
    request = httpx.Request("POST", "https://mock.invalid")
    error = (
        httpx.ReadTimeout("secret provider detail")
        if kind == "timeout"
        else httpx.HTTPStatusError(
            "secret provider detail",
            request=request,
            response=httpx.Response(
                {"rate_limit": 429, "authentication": 401, "unsupported": 400}[kind]
            ),
        )
    )
    # SDK exceptions expose status_code, httpx errors are otherwise generic.
    if kind != "timeout":
        error.status_code = error.response.status_code
    model = FakeModel([error])
    journal = MemoryJournal()
    with pytest.raises(ProviderFailure) as caught:
        await run_llm(
            public(),
            configuration(),
            None,
            journal,
            lambda _, model=model: (model, Client()),
        )
    assert "secret" not in str(caught.value)
    assert (
        str(caught.value)
        == {
            "timeout": "timeout",
            "rate_limit": "rate_limit",
            "authentication": "authentication",
            "unsupported": "unsupported_capability_or_configuration",
        }[kind]
    )
    assert model.calls == 1


@pytest.mark.asyncio
async def test_unsupported_tool_calling():
    with pytest.raises(ProviderFailure, match="unsupported_tool_calling"):
        await run_llm(
            public(),
            configuration(),
            None,
            MemoryJournal(),
            lambda _: (FakeModel([final()]), Client()),
        )


@pytest.mark.asyncio
async def test_budget_and_missing_usage():
    oversized = response(TextPart('{"resolution":"completed"}'))
    oversized.usage = RequestUsage(input_tokens=100000, output_tokens=100000)
    with pytest.raises(ProviderFailure, match="provider_budget_overrun"):
        await run_llm(
            public(),
            configuration(),
            None,
            MemoryJournal(),
            lambda _: (FakeModel([probe(), oversized]), Client()),
        )
    model = FakeModel([probe()])
    with pytest.raises(AgentLimit):
        await run_llm(
            public(),
            configuration(max_tokens=128),
            None,
            MemoryJournal(),
            lambda _, model=model: (model, Client()),
        )
    assert model.calls == 0
    with pytest.raises(ProviderFailure, match="incomplete_usage"):
        await run_llm(
            public(),
            configuration(),
            None,
            MemoryJournal(),
            lambda _: (
                FakeModel(
                    [response(ToolCallPart("capability_probe", {}), usage=False)]
                ),
                Client(),
            ),
        )
    with pytest.raises(AgentLimit):
        await run_llm(
            public(),
            configuration(max_requests=1),
            None,
            MemoryJournal(),
            lambda _: (FakeModel([probe()]), Client()),
        )
    with pytest.raises(AgentLimit):
        await run_llm(
            public(),
            configuration(max_estimated_cost_usd=0.000001),
            None,
            MemoryJournal(),
            lambda _: (FakeModel([]), Client()),
        )


@pytest.mark.asyncio
async def test_interrupted_calls_never_reissued():
    model = FakeModel(["interrupt"])
    journal = MemoryJournal()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await run_llm(
                public(),
                configuration(),
                None,
                journal,
                lambda _, model=model: (model, Client()),
            )
    with pytest.raises(ProviderFailure, match="interrupted_unknown_usage"):
        await run_llm(
            public(),
            configuration(),
            None,
            journal,
            lambda _, model=model: (model, Client()),
        )
    assert model.calls == 1


def test_credentials_capabilities_and_cost(monkeypatch):
    cfg = configuration().model_configuration
    monkeypatch.delenv("AGENTPROOF_ALLOW_MODEL_REQUESTS", raising=False)
    with pytest.raises(ProviderFailure, match="disabled"):
        build_model(cfg)
    monkeypatch.setenv("AGENTPROOF_ALLOW_MODEL_REQUESTS", "1")
    monkeypatch.delenv("AGENTPROOF_TEST_KEY", raising=False)
    with pytest.raises(ProviderFailure, match="missing_api_key"):
        build_model(cfg)
    with pytest.raises(ValidationError):
        configuration(tool_calling=False)
    with pytest.raises(ValidationError):
        configuration(api_key="not-a-key")
    assert cost(1000000, 1000000, cfg) == 3
    assert ModelConfiguration.model_validate(cfg).model == "test-model"


@pytest.mark.asyncio
async def test_invalid_arguments_journal_and_trial_isolation(benchmark_factory, dsn):
    _, ids, _ = benchmark_factory(ids=["cs-001"], trials=2)
    for trial in ids:
        with connect(dsn) as conn:
            simulator = OrderSimulator(conn, trial, dataset().scenarios[0])
            journal = Journal(conn, trial)
            model = FakeModel(
                [
                    probe(),
                    response(
                        ToolCallPart(
                            "refund_order",
                            {
                                "order_id": dataset().scenarios[0].request.order_id,
                                "amount_cents": "invalid",
                            },
                        )
                    ),
                    final(),
                ]
            )
            await run_llm(
                public(),
                configuration(),
                simulator.call,
                journal,
                lambda _, model=model: (model, Client()),
            )
            events = conn.execute(
                "SELECT payload FROM benchmark_events WHERE trial_id=%s", (trial,)
            ).fetchall()
            assert len(events) == 1 and not events[0][0]["arguments_valid"]
            assert (
                conn.execute(
                    "SELECT count(*) FROM benchmark_effects WHERE trial_id=%s", (trial,)
                ).fetchone()[0]
                == 0
            )
            assert len(journal.metadata()) == 3
            saved = journal.reserve(2)
            assert saved[0].parts[0].tool_name == "refund_order"
            journal.reserve(4)
            with pytest.raises(ProviderFailure, match="interrupted_unknown_usage"):
                journal.reserve(4)


@pytest.mark.asyncio
async def test_openai_compatible_http_adapter(monkeypatch):
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        parts = (
            {
                "tool_calls": [
                    {
                        "id": "probe-1",
                        "type": "function",
                        "function": {"name": "capability_probe", "arguments": "{}"},
                    }
                ]
            }
            if len(seen) == 1
            else {"content": '{"resolution":"completed"}'}
        )
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if len(seen) == 1 else "stop",
                        "message": {"role": "assistant", **parts},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    client = AsyncOpenAI(
        api_key="fixture-key",
        base_url="https://mock.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    model = OpenAIChatModel("test-model", provider=OpenAIProvider(openai_client=client))
    journal = MemoryJournal()
    await run_llm(public(), configuration(), None, journal, lambda _: (model, client))
    assert len(seen) == 2 and seen[0]["tool_choice"] == "required"
    assert seen[0]["parallel_tool_calls"] is False
    assert seen[0]["max_completion_tokens"] == 256
    assert seen[0]["temperature"] == 0.0 and seen[0]["top_p"] == 1.0
    assert journal.rows[2][1]["input_tokens"] == 20


@pytest.mark.asyncio
async def test_bounded_rate_limit_retry():
    error = httpx.HTTPStatusError(
        "do not expose",
        request=httpx.Request("POST", "https://mock.invalid"),
        response=httpx.Response(429),
    )
    error.status_code = 429
    model = FakeModel([error, probe(), final()])
    journal = MemoryJournal()
    cfg = configuration()
    cfg.max_retries = 1
    await run_llm(public(), cfg, None, journal, lambda _: (model, Client()))
    assert model.calls == 3


@pytest.mark.asyncio
async def test_actual_trial_pipeline(benchmark_factory, dsn, tmp_path, monkeypatch):
    from agentproof.benchmark import llm
    from agentproof.benchmark.execution import execute_trial, finalize

    cfg = configuration()
    (tmp_path / "test-real.v1.json").write_text(cfg.model_dump_json())
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))
    order = dataset().scenarios[0].request.order_id

    def factory(_):
        return FakeModel(
            [
                probe(),
                response(ToolCallPart("get_order", {"order_id": order})),
                response(ToolCallPart("cancel_order", {"order_id": order})),
                final(),
            ]
        ), Client()

    monkeypatch.setattr(llm, "build_model", factory)
    evaluation, ids, _ = benchmark_factory(agent="test-real", ids=["cs-001"], trials=2)
    for trial in ids:
        result = await execute_trial(trial, dsn)
        assert result["verdict"] == "PASS"
        assert result["effect_counts"]["cancellation"] == 1
        assert result["input_tokens"] == 80 and result["output_tokens"] == 40
        assert result["estimated_cost_usd"] == pytest.approx(0.00016)
        assert result["reported_cost_usd"] is None
        assert len(result["model_requests"]) == 4
    metrics = finalize(evaluation, dsn=dsn)
    assert metrics["repeated_run_reliability"]["value"] == 1
    assert metrics["model_latency_ms"]["sample_count"] == 8


@pytest.mark.asyncio
async def test_provider_failure_classification_in_database(
    benchmark_factory, dsn, tmp_path, monkeypatch
):
    from agentproof.benchmark import llm
    from agentproof.benchmark.execution import execute_trial

    cfg = configuration()
    (tmp_path / "test-real.v1.json").write_text(cfg.model_dump_json())
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))

    def missing(_):
        raise ProviderFailure("missing_api_key")

    monkeypatch.setattr(llm, "build_model", missing)
    _, ids, _ = benchmark_factory(agent="test-real", ids=["cs-001"])
    result = await execute_trial(ids[0], dsn)
    assert (
        result["verdict"] == "INDETERMINATE"
        and result["failure_category"] == "provider"
    )
    assert result["provider_error"] == "missing_api_key"


@pytest.mark.asyncio
async def test_partial_usage_and_invalid_final_output():
    partial = probe()
    partial.metadata = {"usage_fields": {"input_tokens": 20, "output_tokens": None}}
    journal = MemoryJournal()
    with pytest.raises(ProviderFailure, match="incomplete_usage"):
        await run_llm(
            public(),
            configuration(),
            None,
            journal,
            lambda _: (FakeModel([partial]), Client()),
        )
    assert journal.rows[1][1]["output_tokens"] is None
    assert journal.rows[1][1]["estimated_cost_usd"] is None
    outcome = await run_llm(
        public(),
        configuration(),
        None,
        MemoryJournal(),
        lambda _: (
            FakeModel([probe(), response(TextPart("not valid JSON"))]),
            Client(),
        ),
    )
    assert outcome.resolution == "invalid"


@pytest.mark.asyncio
async def test_model_trace_redaction(monkeypatch):
    from agentproof.benchmark import llm
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(llm, "tracer", provider.get_tracer("adapter-test"))
    await run_llm(
        public(),
        configuration(),
        None,
        MemoryJournal(),
        lambda _: (FakeModel([probe(), final()]), Client()),
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.name == "benchmark.model.request"
        assert span.attributes["gen_ai.usage.input_tokens"] == 20
        assert "customer_id" not in str(dict(span.attributes))
        assert "AGENTPROOF_TEST_KEY" not in str(dict(span.attributes))
    provider.shutdown()


@pytest.mark.asyncio
async def test_security_failure_survives_provider_failure(
    benchmark_factory, dsn, tmp_path, monkeypatch
):
    from agentproof.benchmark import llm
    from agentproof.benchmark.execution import execute_trial

    cfg = configuration()
    (tmp_path / "test-real.v1.json").write_text(cfg.model_dump_json())
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))

    def factory(_):
        return FakeModel(
            [
                probe(),
                response(
                    ToolCallPart("unapproved_tool", {"order_id": "outside-order"})
                ),
                httpx.ReadTimeout("secret"),
            ]
        ), Client()

    monkeypatch.setattr(llm, "build_model", factory)
    _, ids, _ = benchmark_factory(agent="test-real", ids=["cs-001"])
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "FAIL" and result["failure_category"] == "agent"
    assert result["provider_error"] == "timeout"
    assert result["unsafe_actions"] == 1 and result["tool_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_id,profile_limit,published_limit", [("cs-001", 1, 12), ("cs-024", 10, 1)]
)
async def test_profile_tool_limit_cannot_weaken_scenario(
    benchmark_factory,
    dsn,
    tmp_path,
    monkeypatch,
    scenario_id,
    profile_limit,
    published_limit,
):
    from agentproof.benchmark import llm
    from agentproof.benchmark.execution import execute_trial

    cfg = configuration(max_tool_calls=profile_limit)
    (tmp_path / "test-real.v1.json").write_text(cfg.model_dump_json())
    monkeypatch.setenv("AGENTPROOF_MODEL_CONFIG_DIR", str(tmp_path))
    order = dataset().scenarios[0].request.order_id

    def factory(_):
        return FakeModel(
            [
                probe(),
                response(ToolCallPart("get_order", {"order_id": order})),
                response(ToolCallPart("cancel_order", {"order_id": order})),
            ]
        ), Client()

    monkeypatch.setattr(llm, "build_model", factory)
    _, ids, manifest = benchmark_factory(agent="test-real", ids=[scenario_id])
    result = await execute_trial(ids[0], dsn)
    assert result["verdict"] == "FAIL" and result["failure_category"] == "agent"
    assert result["effect_counts"].get("cancellation", 0) == 0
    assert result["final_state"]["status"] == "active"
    assert (
        next(a for a in result["assertions"] if a["id"] == "execution_limits")[
            "verdict"
        ]
        == "FAIL"
    )
    with connect(dsn) as conn:
        events = conn.execute(
            "SELECT payload FROM benchmark_events WHERE trial_id=%s ORDER BY sequence",
            (ids[0],),
        ).fetchall()
        assert events[-1][0]["response"]["error"] == "execution_limit"
        stored = conn.execute(
            "SELECT scenario FROM benchmark_trials WHERE id=%s", (ids[0],)
        ).fetchone()[0]
    assert stored == next(
        s for s in manifest["dataset"]["content"]["scenarios"] if s["id"] == scenario_id
    )
    assert stored["limits"]["max_tool_calls"] == published_limit


@pytest.mark.asyncio
async def test_capability_probe_does_not_leak_into_task_history():
    class HistoryModel(FakeModel):
        async def request(self, messages, settings, params):
            if self.calls == 1:
                assert all(
                    not isinstance(part, ToolCallPart)
                    or part.tool_name != "capability_probe"
                    for message in messages
                    for part in message.parts
                )
                assert len(messages) == 1
                assert all(t.name != "capability_probe" for t in params.function_tools)
            return await super().request(messages, settings, params)

    model = HistoryModel([probe(), final()])
    journal = MemoryJournal()
    outcome = await run_llm(
        public(), configuration(), None, journal, lambda _: (model, Client())
    )
    assert outcome.resolution == "completed"
    assert len(journal.rows) == 2


@pytest.mark.asyncio
async def test_groq_rate_limit_headers_are_captured_without_retrying_past_deadline(
    monkeypatch,
):

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            429,
            headers={
                "retry-after": "60",
                "x-ratelimit-remaining-tokens": "0",
                "x-do-not-record": "private-fixture",
            },
            json={
                "error": {
                    "message": "fixture rate limit",
                    "type": "tokens",
                    "code": "rate_limit_exceeded",
                }
            },
        )

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    monkeypatch.setenv("AGENTPROOF_ALLOW_MODEL_REQUESTS", "1")
    monkeypatch.setenv("AGENTPROOF_TEST_KEY", "fixture-api-key")
    cfg = configuration(base_url="https://api.groq.com/openai/v1")
    cfg.max_retries = 1

    class EvidenceJournal(MemoryJournal):
        def fail(self, n, category, metadata=None):
            self.rows[n] = metadata

    journal = EvidenceJournal()
    with pytest.raises(ProviderFailure, match="rate_limit"):
        await run_llm(public(), cfg, None, journal)
    assert len(requests) == 1
    assert journal.rows[1]["provider"] == "groq"
    assert journal.rows[1]["status_code"] == 429
    assert journal.rows[1]["rate_limits"]["retry-after"] == "60"
    assert journal.rows[1]["rate_limits"]["x-ratelimit-remaining-tokens"] == "0"
    assert "x-do-not-record" not in journal.rows[1]["rate_limits"]
    assert journal.rows[1]["wire_parameters"]["tool_names"] == ["capability_probe"]
    assert journal.rows[1]["wire_parameters"]["tool_choice"] == "required"
    assert journal.rows[1]["wire_parameters"]["parallel_tool_calls"] is False
    assert journal.rows[1]["sdk_versions"]["openai"]
    assert not models.ALLOW_MODEL_REQUESTS


def test_wire_diagnostic_omits_content_and_credentials():
    request = httpx.Request(
        "POST",
        "https://mock.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer secret-key"},
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "private customer text"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "get_order", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "max_completion_tokens": 256,
        },
    )
    diagnostic = wire_parameters(request)
    assert diagnostic["tool_names"] == ["get_order"]
    assert diagnostic["tool_choice"] == "auto"
    assert diagnostic["max_completion_tokens"] == 256
    assert len(diagnostic["tool_schema_sha256"]) == 64
    assert "private customer text" not in json.dumps(diagnostic)
    assert "secret-key" not in json.dumps(diagnostic)
    too_large = httpx.Request("POST", "https://mock.invalid", content=b"x" * 65537)
    assert wire_parameters(too_large) == {
        "available": False,
        "reason": "request_too_large",
    }


@pytest.mark.asyncio
async def test_groq_tool_generation_rejection_keeps_safe_diagnostic(monkeypatch):
    def handle(_request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "private failed generation text",
                    "code": "tool_use_failed",
                    "type": "invalid_request_error",
                    "failed_generation": "private generated tool arguments",
                }
            },
        )

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    monkeypatch.setenv("AGENTPROOF_ALLOW_MODEL_REQUESTS", "1")
    monkeypatch.setenv("AGENTPROOF_TEST_KEY", "fixture-api-key")

    class EvidenceJournal(MemoryJournal):
        def fail(self, n, category, metadata=None):
            self.rows[n] = {"error": category, **(metadata or {})}

    journal = EvidenceJournal()
    with pytest.raises(ProviderFailure, match="tool_generation_rejected"):
        await run_llm(
            public(),
            configuration(
                base_url="https://api.groq.com/openai/v1",
                model="openai/gpt-oss-20b",
            ),
            None,
            journal,
        )
    saved = journal.rows[1]
    assert saved["error"] == "tool_generation_rejected"
    assert saved["provider_error_code"] == "tool_use_failed"
    assert saved["failed_generation_present"] is True
    assert saved["wire_parameters"]["tool_names"] == ["capability_probe"]
    assert "temperature" not in saved["wire_parameters"]
    assert "private" not in json.dumps(saved)
    assert "fixture-api-key" not in json.dumps(saved)


@pytest.mark.asyncio
async def test_nondefault_groq_reasoning_sampling_fails_before_request():
    for overrides in ({"temperature": 0.2}, {"top_p": 0.8}):
        model = FakeModel([probe()])
        with pytest.raises(
            ProviderFailure, match="unsupported_sampling_with_reasoning"
        ):
            await run_llm(
                public(),
                configuration(
                    base_url="https://api.groq.com/openai/v1",
                    model="openai/gpt-oss-20b",
                    **overrides,
                ),
                None,
                MemoryJournal(),
                lambda _, model=model: (model, Client()),
            )
        assert model.calls == 0


@pytest.mark.asyncio
async def test_groq_probe_prompt_does_not_advertise_unavailable_customer_tools():
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    sent = []

    def handle(request):
        body = json.loads(request.content)
        if body.get("model") == "openai/gpt-oss-120b":
            assert "temperature" not in body and "top_p" not in body
            assert body["max_completion_tokens"] == 256
        names = [t["function"]["name"] for t in body.get("tools", [])]
        if names == ["capability_probe"] and "get_order" in json.dumps(
            body["messages"]
        ):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "attempted to call tool 'get_order' which was not in request.tools",
                        "code": "tool_use_failed",
                        "type": "invalid_request_error",
                    }
                },
            )
        sent.append(body)
        if len(sent) == 1:
            assert names == ["capability_probe"]
            assert body["tool_choice"] == "required"
            assert body["parallel_tool_calls"] is False
            assert "customer-001" not in json.dumps(body["messages"])
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "probe",
                        "type": "function",
                        "function": {"name": "capability_probe", "arguments": "{}"},
                    }
                ],
            }
        elif len(sent) == 2:
            assert "get_order" in names and "capability_probe" not in names
            assert "customer-001" in json.dumps(body["messages"])
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {
                            "name": "get_order",
                            "arguments": '{"order_id":"order-001"}',
                        },
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": '{"resolution":"completed"}'}
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls"
                        if "tool_calls" in message
                        else "stop",
                        "message": message,
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    old_request = httpx.Request(
        "POST",
        "https://mock.invalid/v1/chat/completions",
        json={
            "tools": [{"type": "function", "function": {"name": "capability_probe"}}],
            "messages": [
                {"role": "user", "content": public().model_dump_json() + " get_order"}
            ],
        },
    )
    assert handle(old_request).status_code == 400
    assert not sent
    client = AsyncOpenAI(
        api_key="fixture-key",
        base_url="https://mock.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    model = OpenAIChatModel(
        "openai/gpt-oss-120b", provider=OpenAIProvider(openai_client=client)
    )
    calls = []

    async def tool(name, args):
        calls.append((name, args))
        return {"ok": True}

    outcome = await run_llm(
        public(),
        configuration(
            model="openai/gpt-oss-120b", base_url="https://api.groq.com/openai/v1"
        ),
        tool,
        MemoryJournal(),
        lambda _: (model, client),
    )
    assert outcome.resolution == "completed"
    assert calls == [("get_order", {"order_id": "order-001"})]
    assert len(sent) == 3
