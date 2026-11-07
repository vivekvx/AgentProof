"""Test-only interruption hooks for the benchmark workflow."""

import asyncio
import os
import sys
import threading
from pathlib import Path

from agentproof.benchmark.activities import ACTIVITIES
from agentproof.benchmark.simulator import OrderSimulator
from agentproof.benchmark.workflow import BenchmarkWorkflow
from agentproof.telemetry import configure
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

mode, queue, marker = sys.argv[1:]
if mode == "after_commit":
    original = OrderSimulator.call

    async def call(self, *args, **kwargs):
        value = await original(self, *args, **kwargs)
        if self.sequence == 2:
            Path(marker).write_text("committed")
            threading.Event().wait()
        return value

    OrderSimulator.call = call


async def main():
    configure()
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"], interceptors=[TracingInterceptor()]
    )
    async with Worker(
        client, task_queue=queue, workflows=[BenchmarkWorkflow], activities=ACTIVITIES
    ):
        await asyncio.Event().wait()


asyncio.run(main())
