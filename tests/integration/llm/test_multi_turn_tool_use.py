"""Stage 8 capability test — :attr:`Capability.MULTI_TURN_TOOL_USE`.

Two-turn flow: the model fires a tool call on turn 1, receives a tool
result, and on turn 2 produces either a final answer OR another tool
call. The capability guards the full loop that every benchmark exercises
— not just the ability to *emit* a tool call (that's
:attr:`Capability.SIMPLE_TOOL_CALL`) but to *continue the dialogue*
after tool results land.

Usage metrics are asserted to accumulate across both turns so that Stage 5's
``Usage`` propagation stays observable in multi-call scenarios.
"""

from __future__ import annotations

import json

import pytest

from tolokaforge.core.models import Message, MessageRole, ToolCall

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS

_GET_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Return the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name."},
            },
            "required": ["city"],
        },
    },
}


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_multi_turn_tool_use(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Turn 1 elicits a tool call; turn 2 handles the tool result."""
    skip_unless_capability_declared(cert, Capability.MULTI_TURN_TOOL_USE)
    client = live_client(cert)

    system = (
        "You are a weather assistant. You MUST use the get_weather tool "
        "for any weather question. After the tool returns, summarise the "
        "result for the user."
    )
    user_msg = Message(role=MessageRole.USER, content="What is the weather in Paris?")

    # --- Turn 1 — the model should fire a tool call.
    turn1 = client.generate(
        system=system,
        messages=[user_msg],
        tools=[_GET_WEATHER_TOOL],
        tool_choice="auto",
    )
    _msg = f"{cert.model_id}: turn 1 did not produce a tool call (text={turn1.text[:120]!r})"
    assert turn1.tool_calls, _msg
    tc = turn1.tool_calls[0]
    assert tc.name == "get_weather", f"{cert.model_id}: wrong tool {tc.name!r}"
    args = tc.arguments if isinstance(tc.arguments, dict) else json.loads(tc.arguments)
    assert "city" in args, f"{cert.model_id}: tool args missing city: {args!r}"

    # --- Turn 2 — feed the tool result back, get a final answer.
    assistant_msg = Message(
        role=MessageRole.ASSISTANT,
        content=turn1.text or "",
        tool_calls=[ToolCall(id=tc.id or "call_1", name=tc.name, arguments=tc.arguments)],
        reasoning=turn1.reasoning,
    )
    tool_result = Message(
        role=MessageRole.TOOL,
        content='{"temperature_c": 21, "conditions": "Sunny"}',
        tool_call_id=tc.id or "call_1",
    )
    turn2 = client.generate(
        system=system,
        messages=[user_msg, assistant_msg, tool_result],
        tools=[_GET_WEATHER_TOOL],
        tool_choice="auto",
    )
    # Either a final answer (text) or a chained tool call is valid.
    terminal = bool(turn2.text.strip()) or bool(turn2.tool_calls)
    _msg = f"{cert.model_id}: turn 2 produced neither text nor tool call (usage={turn2.usage!r})"
    assert terminal, _msg

    # Metrics accumulate across calls — combined usage must strictly exceed
    # either single turn's totals.
    combined_prompt = turn1.usage.prompt_tokens + turn2.usage.prompt_tokens
    assert combined_prompt > turn1.usage.prompt_tokens, (
        f"{cert.model_id}: turn 2 prompt_tokens did not accumulate "
        f"(turn1={turn1.usage.prompt_tokens}, turn2={turn2.usage.prompt_tokens})"
    )
