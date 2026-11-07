"""Bounded real model adapter. Only simulator capabilities are executable."""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from pydantic_ai import models
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition

from ..telemetry import tracer
from .contracts import AgentOutcome, ModelConfiguration
from .simulator import ARGUMENTS, AgentLimit

_MODEL_GATE = threading.Lock()


@asynccontextmanager
async def model_gate():
    while not _MODEL_GATE.acquire(blocking=False):
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        _MODEL_GATE.release()


class ProviderFailure(RuntimeError):
    """Sanitized provider error, never an SDK error body or request URL."""


SAFE_PROVIDER_CODES = frozenset(
    {
        "tool_use_failed",
        "invalid_api_key",
        "rate_limit_exceeded",
        "model_not_found",
        "invalid_request_error",
    }
)


def provider_code(error):
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) and code in SAFE_PROVIDER_CODES else None


def wire_parameters(request):
    """Extract only bounded, non-content fields from the actual SDK request."""
    try:
        raw = request.content
        if len(raw) > 65536:
            return {"available": False, "reason": "request_too_large"}
        body = json.loads(raw)
        if not isinstance(body, dict):
            return {"available": False, "reason": "invalid_request_shape"}
        tools = body.get("tools", [])
        if not isinstance(tools, list) or len(tools) > 32:
            return {"available": False, "reason": "invalid_tool_list"}
        names = [tool.get("function", {}).get("name") for tool in tools]
        if any(
            not isinstance(n, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", n)
            for n in names
        ):
            return {"available": False, "reason": "invalid_tool_name"}
        result = {
            "available": True,
            "model": body.get("model")
            if isinstance(body.get("model"), str) and len(body["model"]) <= 120
            else None,
            "tool_names": names,
            "tool_schema_sha256": hashlib.sha256(
                json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        if body.get("tool_choice") in {"auto", "required", "none"}:
            result["tool_choice"] = body["tool_choice"]
        for field in (
            "parallel_tool_calls",
            "reasoning_effort",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
        ):
            value = body.get(field)
            if type(value) in (str, int, float, bool) and (
                type(value) is not str or len(value) <= 32
            ):
                result[field] = value
        return result
    except (ValueError, TypeError, AttributeError, httpx.RequestNotRead):
        return {"available": False, "reason": "unreadable_request"}


def sdk_versions():
    return {
        name: importlib.metadata.version(name)
        for name in ("pydantic-ai-slim", "openai", "httpx")
    }


def classify(error):
    name = type(error).__name__
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return "authentication"
    if status == 429:
        return "rate_limit"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)) or "Timeout" in name:
        return "timeout"
    if status == 400 and provider_code(error) == "tool_use_failed":
        return "tool_generation_rejected"
    if status in (400, 404, 422):
        return "unsupported_capability_or_configuration"
    return "provider_unavailable"


def cost(input_tokens, output_tokens, config):
    return float(
        (
            Decimal(input_tokens) * Decimal(str(config.input_usd_per_million))
            + Decimal(output_tokens) * Decimal(str(config.output_usd_per_million))
        )
        / Decimal(1000000)
    )


class Journal:
    def __init__(self, conn, trial_id):
        self.conn, self.trial_id = conn, trial_id

    def reserve(self, sequence):
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT status,response,metadata FROM benchmark_model_calls WHERE trial_id=%s AND sequence=%s",
                (self.trial_id, sequence),
            ).fetchone()
            if row:
                if row[0] == "failed" and row[2].get("error") == "rate_limit":
                    return None, row[2]
                if row[0] != "completed":
                    raise ProviderFailure(
                        row[2].get("error", "interrupted_unknown_usage")
                    )
                return ModelMessagesTypeAdapter.validate_python(row[1])[0], row[2]
            self.conn.execute(
                "INSERT INTO benchmark_model_calls(trial_id,sequence,status) VALUES(%s,%s,'pending')",
                (self.trial_id, sequence),
            )
        return None

    def complete(self, sequence, response, metadata):
        self.conn.execute(
            "UPDATE benchmark_model_calls SET status='completed',response=%s,metadata=%s WHERE trial_id=%s AND sequence=%s",
            (
                Jsonb(ModelMessagesTypeAdapter.dump_python([response], mode="json")),
                Jsonb(metadata),
                self.trial_id,
                sequence,
            ),
        )

    def fail(self, sequence, category, metadata=None):
        self.conn.execute(
            "UPDATE benchmark_model_calls SET status='failed',metadata=%s WHERE trial_id=%s AND sequence=%s",
            (Jsonb({"error": category, **(metadata or {})}), self.trial_id, sequence),
        )

    def metadata(self):
        return [
            r[0] if r[1] != "pending" else {"error": "interrupted_unknown_usage"}
            for r in self.conn.execute(
                "SELECT metadata,status FROM benchmark_model_calls WHERE trial_id=%s ORDER BY sequence",
                (self.trial_id,),
            )
        ]


class UsageCheckedModel:
    def __init__(self, model, capture):
        self.model, self.capture = model, capture

    async def request(self, *args, **kwargs):
        self.capture.clear()
        try:
            response = await self.model.request(*args, **kwargs)
        except Exception as error:
            error.agentproof_response_metadata = self.capture.copy()
            raise
        response.metadata = {
            **(response.metadata or {}),
            "usage_fields": self.capture.copy(),
        }
        return response


def build_model(config):
    if os.environ.get("AGENTPROOF_ALLOW_MODEL_REQUESTS") != "1":
        raise ProviderFailure("model_requests_disabled")
    key = os.environ.get(config.api_key_env)
    if not key:
        raise ProviderFailure("missing_api_key")
    capture = {}

    async def capture_request(request):
        capture["wire_parameters"] = wire_parameters(request)

    async def capture_usage(response):
        await response.aread()
        capture["rate_limits"] = {
            name: response.headers[name][:100]
            for name in (
                "x-ratelimit-limit-requests",
                "x-ratelimit-limit-tokens",
                "x-ratelimit-remaining-requests",
                "x-ratelimit-remaining-tokens",
                "x-ratelimit-reset-requests",
                "x-ratelimit-reset-tokens",
                "retry-after",
            )
            if name in response.headers
        }
        if response.status_code == 200:
            try:
                usage = response.json().get("usage") or {}
                for source, target in [
                    ("prompt_tokens", "input_tokens"),
                    ("completion_tokens", "output_tokens"),
                ]:
                    value = usage.get(source)
                    capture[target] = (
                        value if type(value) is int and value >= 0 else None
                    )
            except (ValueError, TypeError, AttributeError):
                pass

    client = AsyncOpenAI(
        api_key=key,
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        max_retries=0,
        http_client=httpx.AsyncClient(
            timeout=config.timeout_seconds,
            follow_redirects=False,
            event_hooks={"request": [capture_request], "response": [capture_usage]},
        ),
    )
    return UsageCheckedModel(
        OpenAIChatModel(config.model, provider=OpenAIProvider(openai_client=client)),
        capture,
    ), client


async def run_llm(context, configuration, tool_call, journal, model_factory=None):
    config = ModelConfiguration.model_validate(configuration.model_configuration)
    groq_gpt_oss = urlsplit(
        config.base_url
    ).hostname == "api.groq.com" and config.model in {
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
    }
    if groq_gpt_oss and (config.temperature != 0 or config.top_p != 1):
        raise ProviderFailure("unsupported_sampling_with_reasoning")
    model_settings = {
        "max_tokens": config.max_output_tokens,
        "parallel_tool_calls": False,
    }
    if not groq_gpt_oss:
        model_settings.update(
            {"temperature": config.temperature, "top_p": config.top_p}
        )
    model, client = (model_factory or build_model)(config)
    messages = [
        ModelRequest(
            parts=[
                SystemPromptPart(
                    configuration.prompt
                    + '\nReturn final JSON only: {"resolution":"completed|rejected|blocked"}. '
                    "Use tools for order facts. A timeout can mean the mutation committed."
                ),
                UserPromptPart(context.model_dump_json()),
            ]
        )
    ]
    tools = [
        ToolDefinition(
            name=name, parameters_json_schema=ARGUMENTS[name].model_json_schema()
        )
        for name in context.permissions.tools
    ]
    used_tokens, spent, requests = 0, 0.0, 0
    probing = True
    retries = 0
    retrying_rate_limit = False
    deadline = time.perf_counter() + context.limits.wall_ms / 1000
    provider_name = (
        "groq"
        if urlsplit(config.base_url).hostname == "api.groq.com"
        else "openai-compatible"
    )
    # Reserve conservatively for UTF-8 input plus protocol overhead. Stop if usage is missing.
    try:
        while True:
            sequence = requests + 1
            replay = (
                journal.reserve(sequence) if sequence <= config.max_requests else None
            )
            if replay:
                response, metadata = replay
            else:
                input_bound = (
                    len(ModelMessagesTypeAdapter.dump_json(messages)) * 4
                    + len(json.dumps([t.parameters_json_schema for t in tools])) * 4
                    + 2048
                )
                reserve_tokens = input_bound + config.max_output_tokens
                reserve_cost = cost(input_bound, config.max_output_tokens, config)
                if (
                    requests >= config.max_requests
                    or used_tokens + reserve_tokens > config.max_tokens
                    or spent + reserve_cost > config.max_estimated_cost_usd
                ):
                    if retrying_rate_limit:
                        if sequence <= config.max_requests:
                            journal.fail(sequence, "rate_limit")
                        raise ProviderFailure("rate_limit")
                    if sequence <= config.max_requests:
                        journal.fail(sequence, "budget_exhausted")
                    raise AgentLimit("model budget exhausted")
                started = time.perf_counter()
                with tracer.start_as_current_span(
                    "benchmark.model.request",
                    attributes={
                        "gen_ai.request.model": config.model,
                        "gen_ai.provider.name": provider_name,
                        "agentproof.model.sequence": sequence,
                    },
                ) as span:
                    try:
                        # Explicit worker opt-in is checked by build_model. No global startup enable.
                        # Each trial has its own event-loop thread. Serialize the SDK's process-global override.
                        async with model_gate():
                            with models.override_allow_model_requests(True):
                                request_messages = (
                                    [
                                        ModelRequest(
                                            parts=[
                                                SystemPromptPart(
                                                    "This is only a tool-calling capability check. Do not perform a customer task."
                                                ),
                                                UserPromptPart(
                                                    "Call capability_probe exactly once with empty arguments {}."
                                                ),
                                            ]
                                        )
                                    ]
                                    if probing
                                    else messages
                                )
                                response = await model.request(
                                    request_messages,
                                    model_settings,
                                    ModelRequestParameters(
                                        function_tools=[
                                            ToolDefinition(
                                                name="capability_probe",
                                                parameters_json_schema={
                                                    "type": "object",
                                                    "properties": {},
                                                    "additionalProperties": False,
                                                },
                                            )
                                        ]
                                        if probing
                                        else tools,
                                        allow_text_output=not probing,
                                    ),
                                )
                        # OpenAI-compatible adapters must actually supply usage, not SDK default zeroes.
                        fields = (response.metadata or {}).get(
                            "usage_fields",
                            {
                                "input_tokens": response.usage.input_tokens
                                if response.usage.input_tokens
                                or response.usage.output_tokens
                                else None,
                                "output_tokens": response.usage.output_tokens
                                if response.usage.input_tokens
                                or response.usage.output_tokens
                                else None,
                            },
                        )
                        available = (
                            fields.get("input_tokens") is not None
                            and fields.get("output_tokens") is not None
                        )
                        metadata = {
                            "latency_ms": (time.perf_counter() - started) * 1000,
                            "input_tokens": fields.get("input_tokens"),
                            "output_tokens": fields.get("output_tokens"),
                            "estimated_cost_usd": cost(
                                response.usage.input_tokens,
                                response.usage.output_tokens,
                                config,
                            )
                            if available
                            else None,
                            "reported_cost_usd": None,
                            "provider": provider_name,
                            "model": config.model,
                            "rate_limits": fields.get("rate_limits", {}),
                            "wire_parameters": fields.get("wire_parameters"),
                            "sdk_versions": sdk_versions(),
                        }
                        journal.complete(sequence, response, metadata)
                        for key in ("input_tokens", "output_tokens"):
                            if metadata[key] is not None:
                                span.set_attribute("gen_ai.usage." + key, metadata[key])
                    except DatabaseError:
                        raise
                    except asyncio.CancelledError:
                        journal.fail(sequence, "interrupted_unknown_usage")
                        raise
                    except Exception as error:  # noqa: BLE001 -- sanitize every provider exception at this boundary
                        category = classify(error)
                        metadata = {
                            "error": category,
                            "latency_ms": (time.perf_counter() - started) * 1000,
                            "reserved_tokens": reserve_tokens,
                            "reserved_cost_usd": reserve_cost,
                            "provider": provider_name,
                            "model": config.model,
                            "status_code": getattr(error, "status_code", None)
                            if type(getattr(error, "status_code", None)) is int
                            and 100 <= error.status_code <= 599
                            else None,
                            "rate_limits": getattr(
                                error, "agentproof_response_metadata", {}
                            ).get("rate_limits", {}),
                            "wire_parameters": getattr(
                                error, "agentproof_response_metadata", {}
                            ).get("wire_parameters"),
                            "sdk_versions": sdk_versions(),
                        }
                        body = getattr(error, "body", None)
                        if isinstance(body, dict):
                            metadata["failed_generation_present"] = (
                                isinstance(body.get("failed_generation"), str)
                                or isinstance(body.get("error"), dict)
                                and isinstance(
                                    body["error"].get("failed_generation"), str
                                )
                            )
                        code = provider_code(error)
                        if code:
                            metadata["provider_error_code"] = code
                        journal.fail(sequence, category, metadata)
                        span.set_attribute("error.type", category)
                        if category != "rate_limit":
                            raise ProviderFailure(category) from None
                        response = None
            requests += 1
            if response is None:
                retrying_rate_limit = True
                if retries >= configuration.max_retries:
                    raise ProviderFailure("rate_limit")
                retries += 1
                used_tokens += metadata["reserved_tokens"]
                spent += metadata["reserved_cost_usd"]
                try:
                    retry_after = max(
                        0.0,
                        float(metadata.get("rate_limits", {}).get("retry-after", 0)),
                    )
                except (ValueError, TypeError):
                    retry_after = 0.0
                delay = max(min(0.1 * 2**retries, 1), retry_after)
                if delay >= deadline - time.perf_counter():
                    raise ProviderFailure("rate_limit")
                await asyncio.sleep(delay)
                continue
            retrying_rate_limit = False
            if probing:
                probes = [p for p in response.parts if isinstance(p, ToolCallPart)]
                if len(probes) != 1 or probes[0].tool_name != "capability_probe":
                    for part in probes:
                        try:
                            args = part.args_as_dict()
                        except (ValueError, TypeError, AssertionError):
                            args = {"invalid_model_arguments": str(part.args)[:1000]}
                        await tool_call(part.tool_name, args)
                    raise ProviderFailure("unsupported_tool_calling")
                # The capability probe is not part of the customer's task conversation.
                # Removed tools in history can cause strict compatible providers to reject calls.
                probing = False
                if (
                    metadata.get("input_tokens") is None
                    or metadata.get("output_tokens") is None
                ):
                    raise ProviderFailure("incomplete_usage_budget_unknown")
                used_tokens += metadata["input_tokens"] + metadata["output_tokens"]
                spent += metadata["estimated_cost_usd"]
                if (
                    used_tokens > config.max_tokens
                    or spent > config.max_estimated_cost_usd
                ):
                    raise ProviderFailure("provider_budget_overrun")
                continue
            messages.append(response)
            if (
                metadata.get("input_tokens") is not None
                and metadata.get("output_tokens") is not None
            ):
                used_tokens += metadata["input_tokens"] + metadata["output_tokens"]
                spent += metadata["estimated_cost_usd"]
            over_budget = (
                used_tokens > config.max_tokens or spent > config.max_estimated_cost_usd
            )
            # Execute every attempted tool call through the validating simulator, including unknown/invalid tools.
            returns = []
            for part in response.parts:
                if isinstance(part, ToolCallPart):
                    try:
                        args = part.args_as_dict()
                    except (ValueError, TypeError, AssertionError):
                        args = {"invalid_model_arguments": str(part.args)[:1000]}
                    if over_budget:
                        raise ProviderFailure("provider_budget_overrun")
                    answer = await tool_call(part.tool_name, args)
                    returns.append(
                        ToolReturnPart(
                            part.tool_name, answer, tool_call_id=part.tool_call_id
                        )
                    )
            if returns:
                messages.append(ModelRequest(parts=returns))
            else:
                if over_budget:
                    raise ProviderFailure("provider_budget_overrun")
                try:
                    text = "".join(
                        p.content for p in response.parts if isinstance(p, TextPart)
                    )
                    return AgentOutcome.model_validate_json(text)
                except (ValueError, TypeError, AssertionError):
                    return AgentOutcome(resolution="invalid")
            if (
                metadata.get("input_tokens") is None
                or metadata.get("output_tokens") is None
            ):
                raise ProviderFailure("incomplete_usage_budget_unknown")
            if (
                used_tokens >= config.max_tokens
                or spent >= config.max_estimated_cost_usd
            ):
                raise AgentLimit("model budget exhausted")
    finally:
        await client.close()
