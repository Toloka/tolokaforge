"""Round-trip contract for the grade-time transcript wire.

:func:`encode_transcript_wire` and :func:`decode_transcript_wire` are inverses
over everything the ``GradeTrialRequest.llm_messages_json`` payload carries, so
a call and the result it produced stay joinable by id after a trip through the
wire. Locked in both directions:

* ``encode(decode(wire))`` is **byte-identical** to the wire, so a field, key or
  key order that the decoder loses or the encoder omits fails here — the guard
  an encoding with no inverse cannot have.
* ``decode(encode(trajectory))`` reproduces the ``(role, content,
  tool_calls[id, name, arguments], tool_call_id)`` of every message — asserted as
  that tuple, because ``content_blocks`` / ``reasoning`` / ``ts`` are not
  represented on the wire at all.

The fixture is the ambiguity case the id exists for: two calls to one tool with
byte-identical arguments, distinguishable only by ``id``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from tolokaforge.core.grading.transcript_wire import (
    decode_transcript_wire,
    encode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.models import Message, MessageRole, ToolCall, Trajectory

pytestmark = pytest.mark.canonical

_REFUND_ARGS = '{"payment_id": "PAY-1"}'

_WIRE_MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "You are an agent. Refund at most once."},
    {"role": "user", "content": "Refund PAY-1 twice? No — once."},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_A", "function": {"name": "refund", "arguments": _REFUND_ARGS}},
            {"id": "call_B", "function": {"name": "refund", "arguments": _REFUND_ARGS}},
        ],
    },
    {"role": "tool", "content": '{"ok": true, "refund_id": "R-1"}', "tool_call_id": "call_A"},
    {"role": "tool", "content": '{"error": "already refunded"}', "tool_call_id": "call_B"},
    {
        "role": "user",
        "content": 'here you go\n\nlook_up_policy() result: {"p": 1}',
        "tool_calls": [
            {
                "id": "call_C",
                "function": {"name": "look_up_policy", "arguments": '{"topic": "refund"}'},
            }
        ],
    },
    {"role": "assistant", "content": "Refunded once."},
]

_WIRE = json.dumps(_WIRE_MESSAGES)


def _trajectory(messages: list[Message]) -> Trajectory:
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    return Trajectory(
        task_id="refund_task",
        trial_index=0,
        start_ts=stamp,
        end_ts=stamp,
        messages=messages,
    )


def _wire_identity(messages: list[Message]) -> list[dict[str, Any]]:
    """The projection of a message list that the wire is expected to preserve."""
    return [
        {
            "role": msg.role,
            "content": msg.content,
            "tool_calls": (
                [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls]
                if msg.tool_calls
                else None
            ),
            "tool_call_id": msg.tool_call_id,
        }
        for msg in messages
    ]


def test_wire_fixture_exercises_the_shapes_the_round_trip_must_cover() -> None:
    """The round trip can only prove what the fixture contains, so the fixture's
    coverage is itself asserted: a leading policy, parallel calls identical but
    for their id, a non-assistant turn carrying tool_calls, and tool results."""
    calls_by_payload: dict[str, set[str]] = {}
    for msg in _WIRE_MESSAGES:
        for tc in msg.get("tool_calls", []):
            calls_by_payload.setdefault(json.dumps(tc["function"]), set()).add(tc["id"])

    assert _WIRE_MESSAGES[0]["role"] == "system"
    assert any(len(ids) > 1 for ids in calls_by_payload.values()), (
        "no two fixture calls share a byte-identical function payload, so the round trip "
        "would pass even if the encoder joined calls to results by position"
    )
    assert {"user", "assistant"} <= {
        msg["role"] for msg in _WIRE_MESSAGES if msg.get("tool_calls")
    }, "tool_calls must appear on both an assistant and a user turn"
    assert sum(1 for msg in _WIRE_MESSAGES if msg.get("tool_call_id")) >= 2


@pytest.mark.parametrize("lift_policy", [False, True], ids=["policy_in_messages", "policy_lifted"])
def test_encode_is_the_byte_exact_inverse_of_decode(lift_policy: bool) -> None:
    """Re-encoding a decoded payload reproduces it byte for byte, whether the
    agent policy rides as the leading ``system`` message or is lifted out and
    handed back to the encoder as the policy argument."""
    if lift_policy:
        agent_system_prompt, entries = split_leading_system_message(json.loads(_WIRE))
    else:
        agent_system_prompt, entries = "", json.loads(_WIRE)

    re_encoded = encode_transcript_wire(
        _trajectory(decode_transcript_wire(entries)), agent_system_prompt
    )

    assert re_encoded == _WIRE


def test_decode_of_encode_preserves_every_field_the_wire_carries() -> None:
    messages = [
        Message(role=MessageRole.USER, content="Refund PAY-1 once."),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="call_A", name="refund", arguments={"payment_id": "PAY-1"}),
                ToolCall(id="call_B", name="refund", arguments={"payment_id": "PAY-1"}),
            ],
        ),
        Message(role=MessageRole.TOOL, content='{"ok": true}', tool_call_id="call_A"),
        Message(role=MessageRole.TOOL, content='{"error": "dup"}', tool_call_id="call_B"),
        Message(role=MessageRole.ASSISTANT, content="Refunded once."),
    ]

    wire = encode_transcript_wire(_trajectory(messages), "You are an agent.")
    assert wire is not None
    agent_system_prompt, entries = split_leading_system_message(json.loads(wire))
    decoded = decode_transcript_wire(entries)

    assert agent_system_prompt == "You are an agent."
    assert _wire_identity(decoded) == _wire_identity(messages)


def test_wire_does_not_represent_content_blocks_reasoning_or_timestamps() -> None:
    """The stated lossiness, locked so it is a known non-guarantee rather than a
    surprise for a consumer designing against the wire: a screenshot-only turn
    crosses as ``content: ""``."""
    stamp = datetime(2020, 5, 17, 12, 30, tzinfo=UTC)
    original = Message(
        role=MessageRole.ASSISTANT,
        content="",
        content_blocks=[{"type": "image", "source": {"data": "screenshot"}}],
        reasoning=StructuredReasoning(blocks=(ReasoningBlock(type="thinking", text="deliberate"),)),
        ts=stamp,
    )

    wire = encode_transcript_wire(_trajectory([original]), "")
    assert wire is not None
    decoded = decode_transcript_wire(json.loads(wire))[0]

    assert json.loads(wire) == [{"role": "assistant", "content": ""}]
    assert decoded.content_blocks is None
    assert decoded.reasoning is None
    assert decoded.ts != stamp


def test_empty_trace_with_no_policy_encodes_to_none() -> None:
    assert encode_transcript_wire(_trajectory([]), "") is None
