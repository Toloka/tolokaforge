"""The grade-time transcript wire encoding and its inverse.

The host serialises a trial's message trace into
``GradeTrialRequest.llm_messages_json``; the runner reads it back. Both
directions live here, so the encoding has one definition and the inverse is
checkable: :func:`encode_transcript_wire` writes the payload,
:func:`decode_transcript_wire` reconstructs the :class:`Message` list from it,
and :func:`split_leading_system_message` lifts back out the agent policy the
encoder prepends.

The payload carries the **full interleaved** trace — assistant and user turns,
each ``tool_calls`` entry carrying the provider's call id, and every
``role: tool`` result carrying its ``tool_call_id`` — so a call and the result
it produced are joined by id rather than by position. Parallel calls to one tool
with identical arguments are distinguishable only that way.

``Message.content_blocks``, ``Message.reasoning`` and ``Message.ts`` are not
represented: a screenshot-only assistant turn crosses as ``content: ""``.
"""

from __future__ import annotations

import json
from typing import Any

from tolokaforge.core.models import Message, MessageRole, ToolCall, Trajectory

__all__ = [
    "decode_transcript_wire",
    "encode_transcript_wire",
    "split_leading_system_message",
]


def encode_transcript_wire(
    trajectory: Trajectory,
    agent_system_prompt: str,
) -> str | None:
    """Serialise the transcript + agent policy for the runner-side grading.

    The runner decides whether to actually run the rubric judge based on its
    own grading config; this always serialises the transcript when there is one
    and returns ``None`` for an empty trace with no policy.
    """
    if not trajectory.messages and not agent_system_prompt:
        return None

    messages: list[dict[str, Any]] = []
    if agent_system_prompt:
        messages.append({"role": "system", "content": agent_system_prompt})
    for msg in trajectory.messages:
        entry: dict[str, Any] = {
            "role": msg.role.value,
            "content": msg.content or "",
        }
        if msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id
        messages.append(entry)
    return json.dumps(messages)


def decode_transcript_wire(messages: list[dict[str, Any]]) -> list[Message]:
    """Reconstruct the :class:`Message` list from a decoded wire payload.

    Inverse of :func:`encode_transcript_wire` over everything the wire carries:
    role, content, ``tool_calls`` (id, name, arguments) and ``tool_call_id``.
    Every malformed shape raises, naming the offending message — a payload that
    cannot be reconstructed must not be silently degraded into an unlinkable
    trace.
    """
    return [_decode_entry(entry, index) for index, entry in enumerate(messages)]


def _decode_entry(entry: dict[str, Any], index: int) -> Message:
    for key in ("role", "content"):
        if key not in entry:
            raise ValueError(f"wire message {index} has no {key!r}; keys present: {sorted(entry)}")
    raw_calls = entry.get("tool_calls") or []
    return Message(
        role=MessageRole(entry["role"]),
        content=entry["content"],
        tool_calls=[_decode_tool_call(raw, index) for raw in raw_calls] or None,
        tool_call_id=entry.get("tool_call_id"),
    )


def _decode_tool_call(raw: dict[str, Any], message_index: int) -> ToolCall:
    if "function" not in raw:
        raise ValueError(
            f"wire message {message_index}: tool call has no 'function'; "
            f"keys present: {sorted(raw)}"
        )
    function = raw["function"]
    for key in ("name", "arguments"):
        if key not in function:
            raise ValueError(
                f"wire message {message_index}: tool call's 'function' has no {key!r}; "
                f"keys present: {sorted(function)}"
            )
    name = function["name"]
    call_id = raw.get("id")
    if not call_id:
        raise ValueError(
            f"wire message {message_index}: tool call {name!r} carries no 'id'. The engine "
            "that produced this payload predates the tool-call id on the transcript wire; "
            "a call without one cannot be joined to its result."
        )
    try:
        arguments = json.loads(function["arguments"])
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"wire message {message_index}: tool call {name!r} has unparseable "
            f"arguments JSON: {exc}"
        ) from exc
    return ToolCall(id=call_id, name=name, arguments=arguments)


def split_leading_system_message(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Split the judge wire messages into ``(agent_system_prompt, transcript)``.

    Inverse of the leading-system-message prepend in
    :func:`encode_transcript_wire`: the agent's policy is the transcript's
    leading ``system`` message, lifted out so it is injected as the judge's
    view of the agent policy rather than replayed as a conversational turn.
    Returns ``("", messages)`` unchanged when there is no leading system
    message. Shared by the runner's grading path and the offline judge replay
    so both reconstruct the judge's ``run()`` inputs identically.
    """
    if messages and str(messages[0].get("role", "")).lower() == "system":
        return str(messages[0].get("content", "") or ""), list(messages[1:])
    return "", list(messages)
