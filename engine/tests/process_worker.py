"""Test-only worker. Hooks live here, never in application request handling."""

import asyncio
import os
import sys
import threading
from pathlib import Path

import psycopg
from agentproof import execution, worker
from agentproof.simulator import OrderSimulator
from agentproof.telemetry import configure
from agentproof.workflow import EvaluationWorkflow
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

mode, queue, marker = sys.argv[1:]


def pause():
    Path(marker).write_text(mode)
    threading.Event().wait()


if mode == "before_tool_commit":
    original = psycopg.Connection.execute

    def execute(self, query, *args, **kwargs):
        result = original(self, query, *args, **kwargs)
        if isinstance(query, str) and query.startswith("INSERT INTO tool_events"):
            pause()
        return result

    psycopg.Connection.execute = execute
elif mode == "after_tool_commit":
    original = OrderSimulator.call

    def call(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.sequence == 1:
            pause()
        return result

    OrderSimulator.call = call
elif mode == "after_activity_commit":
    original = worker.execute

    async def execute(*args, **kwargs):
        result = await original(*args, **kwargs)
        Path(marker).write_text(mode)
        await asyncio.Event().wait()
        return result

    worker.execute = execute
elif mode == "evaluator":

    def evaluate(*args, **kwargs):
        raise ValueError("injected evaluator defect")

    execution.evaluate = evaluate
elif mode == "infrastructure":

    async def run_agent(*args, **kwargs):
        raise ConnectionError("injected infrastructure outage")

    execution.run_agent = run_agent
elif mode == "cancel_running":

    async def run_agent(*args, **kwargs):
        Path(marker).write_text(mode)
        await asyncio.Event().wait()

    execution.run_agent = run_agent


async def main():
    configure()
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"], interceptors=[TracingInterceptor()]
    )
    async with Worker(
        client,
        task_queue=queue,
        workflows=[EvaluationWorkflow],
        activities=[worker.execute_evaluation, worker.terminal_failure, worker.failure],
    ):
        await asyncio.Event().wait()


asyncio.run(main())
