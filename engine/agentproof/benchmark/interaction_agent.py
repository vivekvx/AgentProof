"""Deterministic reference agent for published interaction cases.

This agent receives public task text, approved tool names, and trusted replies.
It has no scenario, fixture, obligation, assertion, or fault access.
"""

import re

from .interaction_contracts import InteractionOutcome


async def run_reference(public, tool_call, ask_user, receive_user_reply):
    text = public["user_message"]
    order_id = public["order_id"]
    tools = set(public["tools"])

    async def call(name, **arguments):
        if name not in tools:
            return {"ok": False, "error": "permission_denied"}
        return await tool_call(name, {"order_id": order_id, **arguments})

    if "have not said how much" in text:
        await ask_user("amount_cents")
        reply = await receive_user_reply()
        if reply is None or reply.source != "harness":
            return InteractionOutcome(resolution="clarification_required")
        match = re.fullmatch(r"Refund (\d+) cents\.", reply.text)
        if match is None:
            return InteractionOutcome(resolution="clarification_required")
        amount = int(match.group(1))
        read = await call("get_order")
        if (
            not read.get("ok")
            or amount > read["order"]["total_cents"] - read["order"]["refunded_cents"]
        ):
            return InteractionOutcome(resolution="blocked")
        response = await call("refund_order", amount_cents=amount)
        return InteractionOutcome(
            resolution="completed" if response.get("ok") else "blocked"
        )

    amounts = re.search(
        r"Refund (\d+) cents for item A and (\d+) cents for item B", text
    )
    if amounts:
        read = await call("get_order")
        if not read.get("ok"):
            return InteractionOutcome(resolution="blocked")
        for amount in (int(amounts.group(1)), int(amounts.group(2))):
            response = await call("refund_order", amount_cents=amount)
            if not response.get("ok"):
                return InteractionOutcome(resolution="blocked")
        return InteractionOutcome(resolution="completed")

    if "Do not issue a refund" in text:
        read = await call("get_order")
        if not read.get("ok") or read["order"]["status"] != "active":
            return InteractionOutcome(resolution="blocked")
        response = await call("cancel_order")
        return InteractionOutcome(
            resolution="completed" if response.get("ok") else "blocked"
        )

    if "Change my address" in text:
        approval = await call("request_approval")
        if not approval.get("approved"):
            return InteractionOutcome(resolution="blocked")
        address = text.split("Change my address to ", 1)[1].split(". ", 1)[0]
        response = await call("update_address", address=address)
        return InteractionOutcome(
            resolution="completed" if response.get("ok") else "blocked"
        )

    if "If my order shipped" in text:
        read = await call("get_order")
        if not read.get("ok"):
            return InteractionOutcome(resolution="blocked")
        if read["order"]["status"] == "shipped":
            amount = int(re.search(r"refund (\d+) cents", text).group(1))
            response = await call("refund_order", amount_cents=amount)
        else:
            response = await call("cancel_order")
        return InteractionOutcome(
            resolution="completed" if response.get("ok") else "blocked"
        )

    return InteractionOutcome(resolution="rejected")
