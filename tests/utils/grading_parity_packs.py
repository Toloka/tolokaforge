"""Decoder for a ``grading_parity`` pack's authored ``trial.yaml``.

One trial, read once, handed to each substrate in the input shape that substrate
really takes: the core engine's :class:`Trajectory`, the runner's wire-shaped
``llm_messages_json`` turns and its :class:`TrialTimeline`. The canonical
differential and the docker gate suite both read a pack through here, so "the two
substrates agree" is a statement about the same bytes rather than about two
hand-written readings of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.grading.trace_timeline import TrialTimeline, build_trial_timeline
from tolokaforge.core.models import (
    Message,
    RecordedToolCall,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
)

# The one shape a ``trial.yaml`` case may take. Every key is read; anything else
# is rejected, so a pack cannot carry a field the loader silently drops.
_CASE_KEYS = frozenset({"messages", "state"})
_MESSAGE_KEYS = frozenset({"role", "content", "tool_calls"})
_CALL_KEYS = frozenset({"tool_name", "executor", "status", "arguments", "output"})

FIXTURE_TIMESTAMP = "2026-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class TrialCase:
    """One satisfying-or-violating trial, in each substrate's own input shape."""

    core_trajectory: Trajectory
    runner_messages: list[dict[str, Any]]
    runner_timeline: TrialTimeline
    state: dict[str, Any]


def _reject_unknown(authored: dict[str, Any], allowed: frozenset[str], *, what: str) -> None:
    """A fixture key nothing reads expresses less than its author wrote — so it is an error."""
    unknown = sorted(set(authored) - allowed)
    assert not unknown, f"{what} declares {unknown}; the loader reads only {sorted(allowed)}"


def _authored_call(raw_call: dict[str, Any], *, sequence: int) -> tuple[ToolCall, RecordedToolCall]:
    """One authored call, as the message view declares it and as the record kept it.

    ``latency_seconds`` is not authorable and is pinned at ``0.0``: wall time is
    not compared across substrates, so a fixture varying it would pin a number no
    parity claim reads.
    """
    call_id = f"call_{sequence}"
    return (
        ToolCall(id=call_id, name=raw_call["tool_name"], arguments=raw_call["arguments"]),
        RecordedToolCall(
            call_id=call_id,
            sequence=sequence,
            tool_name=raw_call["tool_name"],
            arguments=raw_call["arguments"],
            executor=ToolExecutorIdentity(raw_call["executor"]),
            output=raw_call.get("output", ""),
            status=ToolExecutionStatus(raw_call["status"]),
            latency_seconds=0.0,
            timestamp=FIXTURE_TIMESTAMP,
        ),
    )


def wire_message(message: Message) -> dict[str, Any]:
    """One turn as the runner receives it, in ``llm_messages_json``'s OpenAI shape."""
    wire: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    return wire


def load_case(pack_dir: Path, case: str) -> TrialCase:
    """One authored trial, in the input shape each substrate really takes.

    A call is authored inside the message that requested it, so a fixture places
    its calls across turns and the timeline's ``turn_index`` and event order follow
    what the author wrote.
    """
    fixture = yaml.safe_load((pack_dir / "trial.yaml").read_text())[case]
    where = f"{pack_dir.name}/trial.yaml case {case!r}"
    _reject_unknown(fixture, _CASE_KEYS, what=where)

    # One recorded-tool-call list feeds both substrates: the core engine holds it
    # on the Trajectory, the runner's evaluators read its dump. A per-substrate
    # fixture could disagree with itself, which is the divergence this suite exists
    # to catch. The message view declares every one of them, or the trial's two
    # views disagree and neither substrate will grade it.
    view: list[Message] = []
    recorded: list[RecordedToolCall] = []
    for index, raw in enumerate(fixture["messages"]):
        _reject_unknown(raw, _MESSAGE_KEYS, what=f"{where} message {index}")
        declared: list[ToolCall] = []
        for raw_call in raw.get("tool_calls", ()):
            _reject_unknown(raw_call, _CALL_KEYS, what=f"{where} message {index} tool call")
            call, record = _authored_call(raw_call, sequence=len(recorded))
            declared.append(call)
            recorded.append(record)
        view.append(Message(role=raw["role"], content=raw["content"], tool_calls=declared or None))

    trajectory = Trajectory(
        task_id=pack_dir.name,
        trial_index=0,
        start_ts=FIXTURE_TIMESTAMP,
        end_ts=FIXTURE_TIMESTAMP,
        messages=view,
        tool_log=recorded,
    )
    return TrialCase(
        core_trajectory=trajectory,
        runner_messages=[wire_message(message) for message in view],
        runner_timeline=build_trial_timeline(view, recorded, None),
        state=fixture["state"],
    )
