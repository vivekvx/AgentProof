from pydantic_ai import Agent, models
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from .simulator import OrderSimulator

models.ALLOW_MODEL_REQUESTS = False


async def run_agent(simulator: OrderSimulator, mode: str):
    """Use the real Pydantic AI tool loop with an offline, deterministic model."""

    def respond(messages, info):
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "cancel_order", {"order_id": simulator.scenario.order_id}
                    )
                ]
            )
        if len(returns) == 1:
            tool = "get_order" if mode == "safe" else "cancel_order"
            return ModelResponse(
                parts=[ToolCallPart(tool, {"order_id": simulator.scenario.order_id})]
            )
        return ModelResponse(parts=[TextPart("The order is cancelled.")])

    # Keep the synchronous database connection on this activity's event-loop thread.
    agent = Agent(FunctionModel(respond), capabilities=[Instrumentation()])

    @agent.tool_plain
    async def cancel_order(order_id: str) -> dict:
        """Cancel an order. A timeout leaves the result unknown."""
        return simulator.call("cancel_order", order_id)

    @agent.tool_plain
    async def get_order(order_id: str) -> dict:
        """Read the current order state."""
        return simulator.call("get_order", order_id)

    return await agent.run("Cancel " + simulator.scenario.order_id)
