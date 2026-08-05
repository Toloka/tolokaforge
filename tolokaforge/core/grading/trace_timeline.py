"""The trial's event timeline: one ordered view of what happened, for both substrates.

A trial leaves two records of itself. The **message view** — the assistant and
user turns, each tool call carried on the message that requested it — says what
the agent asked for. The **tool-call record** — one
:class:`~tolokaforge.runner.models.RecordedToolCall` per call, in execution order
— says what happened when it ran. Neither alone is gradeable: the message view
has no status, latency or executor identity, and the record has no conversation.

:func:`build_trial_timeline` joins them into a single ordered tuple of
:class:`TraceEvent`. It is a pure function — no services, no I/O — over inputs
both grading substrates already hold, so a check written against the timeline
evaluates identically whichever substrate grades the trial.

The guarantees the timeline makes, and the states it declares rather than
papers over, are written out in ``docs/GRADING.md`` § "Trial event timeline".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Declared in the leaf ``trace_event_kind`` and re-exported here so the timeline
# and the matcher vocabulary that selects on it name one enum. This module cannot
# own it: ``runner.models`` declares ``TraceMatcher`` and reaching in here for the
# kind would close a cycle through ``core.models``.
from tolokaforge.core.grading.trace_event_kind import TraceEventKind as TraceEventKind
from tolokaforge.core.models import (
    Message,
    MessageRole,
    RecordedToolCall,
    TerminationReason,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
)

__all__ = [
    "AttemptedCall",
    "TimelineInconsistencyError",
    "TraceEvent",
    "TraceEventKind",
    "TrialTimeline",
    "assistant_texts",
    "attempted_calls",
    "build_trial_timeline",
]


@dataclass(frozen=True)
class TraceEvent:
    """One event in a trial, in one flat shape whatever its kind.

    ``None`` means the field is either inapplicable to the kind or unrecorded, so a
    predicate over it is unmatched — never vacuously true. See ``docs/GRADING.md``
    G4 and G6b for when a field that does apply is nonetheless ``None``.

    ``executor``, ``status`` and ``latency_seconds`` come from the trial's
    tool-call record alone, so a call the trial never recorded carries ``None``
    for all three. ``result`` prefers the record — the record's failure text is
    the executing layer's own and is untruncated, while the ``role: tool``
    message words the same failure differently — and falls back to that message
    on a timeline that carries no record view at all.
    """

    position: int
    turn_index: int
    kind: TraceEventKind
    text: str | None
    call_id: str | None
    tool_name: str | None
    executor: ToolExecutorIdentity | None
    arguments: dict[str, Any] | None
    status: ToolExecutionStatus | None
    result: str | None
    latency_seconds: float | None

    __hash__ = None
    """Unhashable, uniformly. ``arguments`` is a dict on every ``TOOL_CALL`` — even
    an empty one — so the frozen dataclass's generated hash raises for that kind and
    succeeds for the others. ``set()`` / ``Counter()`` over results would work while
    the same code over calls raised, which is the worst shape for the checks written
    against this contract. Use ``position`` or ``call_id`` as the key instead."""


@dataclass(frozen=True)
class TrialTimeline:
    """A trial's events plus what the two views of it actually contained.

    ``message_view_present`` and ``records_present`` are both degenerate-input
    flags, and each is a normal state: hash-only grading supplies no messages,
    and a timeline rebuilt from ``trajectory.yaml`` alone has no records — the
    trial's record is the bundle's ``tool_log.yaml`` sidecar, and a bundle written
    before that artifact existed carries none. ``records_present`` says a
    record view was supplied, not that results exist — a records-less timeline
    still carries whatever its ``role: tool`` messages preserved. A constraint
    that reads a field only the missing view supplies must become a named failing
    sub-check — never a silent pass.
    """

    events: tuple[TraceEvent, ...]
    termination_reason: TerminationReason | None
    message_view_present: bool
    records_present: bool


@dataclass(frozen=True)
class AttemptedCall:
    """One tool call the agent asked for, folded together with its outcome.

    ``status`` and ``executor`` are ``None`` when nothing recorded the call — it
    either never ran or the timeline carries no records at all. A check that
    requires a successful call therefore fails on absent evidence instead of
    passing on it.
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    executor: ToolExecutorIdentity | None
    status: ToolExecutionStatus | None


class TimelineInconsistencyError(Exception):
    """The trial's two views cannot be joined into one timeline.

    Raised rather than resolved. A duplicate ``call_id`` or a record naming a
    call the message view does not contain is a broken harness invariant, not
    task data, and picking a winner would produce exactly the ambiguous join the
    ``call_id`` exists to prevent.
    """


_KIND_BY_ROLE = {
    MessageRole.ASSISTANT: TraceEventKind.ASSISTANT_MESSAGE,
    MessageRole.USER: TraceEventKind.USER_MESSAGE,
}


def build_trial_timeline(
    messages: Sequence[Message],
    recorded_calls: Sequence[RecordedToolCall],
    termination_reason: TerminationReason | None,
) -> TrialTimeline:
    """Build the trial's timeline from its message view and its tool-call record.

    Event order follows the message view; within one message, tool calls follow
    recorded execution order, with calls that never executed after them in
    declaration order. A call and its result are joined by ``call_id``, never by
    position.

    A ``TOOL_RESULT``'s text comes from the record. With no record view supplied at
    all — a trial re-graded from its bundle — it comes instead from the
    ``role: tool`` message answering the call, joined by ``tool_call_id``, and the
    fields only a record carries stay ``None``.

    ``messages`` carrying no assistant or user turn is a records-only trial: the
    events come from the record alone, all at ``turn_index`` 0, and
    ``message_view_present`` is ``False``. ``role: system`` messages are harness
    annotations and are never events, so a payload of nothing but harness text is
    not a message view.

    Raises:
        TimelineInconsistencyError: a ``call_id`` occurs twice, a record names a
            call the message view does not contain, or — with no records — a tool
            result message names one.
    """
    records = _index_by_call_id(recorded_calls)
    turns = [message for message in messages if message.role in _KIND_BY_ROLE]
    # The record wins wherever both views describe one call, so the message-side
    # results are read only in its absence.
    message_results = {} if records else _index_message_results(messages, turns)
    builder = _TimelineBuilder(records, message_results)
    if turns:
        builder.emit_message_view(turns)
    else:
        builder.emit_records_alone()
    return TrialTimeline(
        events=builder.events,
        termination_reason=termination_reason,
        message_view_present=bool(turns),
        records_present=bool(recorded_calls),
    )


def assistant_texts(timeline: TrialTimeline) -> tuple[str, ...]:
    """One entry per assistant generation, in order — its text, ``""`` when it had none.

    The length is the trial's assistant turn count, so a turn that produced only
    tool calls still counts as a turn.
    """
    return tuple(
        event.text or ""
        for event in timeline.events
        if event.kind is TraceEventKind.ASSISTANT_MESSAGE
    )


def attempted_calls(timeline: TrialTimeline) -> tuple[AttemptedCall, ...]:
    """Every tool call on the timeline, each joined to its result by ``call_id``."""
    statuses = {
        event.call_id: event.status
        for event in timeline.events
        if event.kind is TraceEventKind.TOOL_RESULT
    }
    return tuple(
        AttemptedCall(
            call_id=event.call_id or "",
            tool_name=event.tool_name or "",
            arguments=event.arguments or {},
            executor=event.executor,
            status=statuses.get(event.call_id),
        )
        for event in timeline.events
        if event.kind is TraceEventKind.TOOL_CALL
    )


def _index_by_call_id(recorded_calls: Sequence[RecordedToolCall]) -> dict[str, RecordedToolCall]:
    index: dict[str, RecordedToolCall] = {}
    for record in recorded_calls:
        seen = index.get(record.call_id)
        if seen is not None:
            raise TimelineInconsistencyError(
                f"tool-call id {record.call_id!r} was recorded twice, at sequences "
                f"{seen.sequence} and {record.sequence}. The id is the only key that joins "
                "a call to the result it produced, so a duplicate leaves the join ambiguous."
            )
        index[record.call_id] = record
    return index


def _declared_call_ids(turns: Sequence[Message]) -> set[str]:
    """Every call id the message view asks for — the only ids a result may name."""
    return {call.id for message in turns for call in (message.tool_calls or [])}


def _index_message_results(messages: Sequence[Message], turns: Sequence[Message]) -> dict[str, str]:
    """The text each ``role: tool`` message carries, keyed by the call it answers.

    Read only when no record view was supplied. That text is on disk in every
    recorded bundle, so dropping it would hide a tool's own output from every
    phrase rule — and hide it in the agent's favour for a disallowed pattern.

    Raises:
        TimelineInconsistencyError: a result names a call the message view does
            not declare, or two results name the same call.
    """
    declared = _declared_call_ids(turns)
    results: dict[str, str] = {}
    for index, message in enumerate(messages):
        if message.role is not MessageRole.TOOL:
            continue
        call_id = message.tool_call_id
        if call_id is None or call_id not in declared:
            raise TimelineInconsistencyError(
                f"the tool result message at index {index} answers tool-call id {call_id!r}, "
                "which no tool call in the trial's message view declares. Its text is the "
                "only surviving evidence of what that tool returned, so it can be neither "
                "joined to a call nor dropped."
            )
        if call_id in results:
            raise TimelineInconsistencyError(
                f"tool-call id {call_id!r} is answered twice, the second time by the tool "
                f"result message at index {index}. The id is the only key that joins a call "
                "to the result it produced, so a duplicate leaves the join ambiguous."
            )
        results[call_id] = message.content
    return results


def _execution_order(
    calls: Sequence[ToolCall], records: dict[str, RecordedToolCall]
) -> list[ToolCall]:
    """One message's calls in the order they ran, then the ones that never ran.

    ``sequence`` is stamped at execution time, so it is execution order rather
    than the order the assistant declared the calls in.
    """
    executed = sorted(
        (call for call in calls if call.id in records), key=lambda call: records[call.id].sequence
    )
    return executed + [call for call in calls if call.id not in records]


class _TimelineBuilder:
    """Accumulates events, assigning ``position`` and ``turn_index`` as it goes."""

    def __init__(
        self, records: dict[str, RecordedToolCall], message_results: dict[str, str]
    ) -> None:
        self._records = records
        self._message_results = message_results
        self._events: list[TraceEvent] = []
        self._call_positions: dict[str, int] = {}
        self._generations = 0

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def emit_message_view(self, turns: Sequence[Message]) -> None:
        self._require_every_record_linkable(turns)
        for message in turns:
            self._emit_turn(message)

    def emit_records_alone(self) -> None:
        for record in sorted(self._records.values(), key=lambda record: record.sequence):
            self._append_call(
                call_id=record.call_id,
                tool_name=record.tool_name,
                arguments=record.arguments,
                executor=record.executor,
            )
            self._append_result(record)

    def _emit_turn(self, message: Message) -> None:
        if message.role is MessageRole.ASSISTANT:
            self._generations += 1
        self._append(kind=_KIND_BY_ROLE[message.role], text=message.content)
        for call in _execution_order(message.tool_calls or [], self._records):
            self._emit_call(call)

    def _emit_call(self, call: ToolCall) -> None:
        record = self._records.get(call.id)
        self._append_call(
            call_id=call.id,
            tool_name=call.name,
            arguments=call.arguments,
            executor=record.executor if record is not None else None,
        )
        if record is not None:
            self._append_result(record)
        elif call.id in self._message_results:
            self._append_message_result(call)

    def _require_every_record_linkable(self, turns: Sequence[Message]) -> None:
        declared = _declared_call_ids(turns)
        unlinkable = sorted(
            (record for record in self._records.values() if record.call_id not in declared),
            key=lambda record: record.sequence,
        )
        if not unlinkable:
            return
        first = unlinkable[0]
        raise TimelineInconsistencyError(
            f"recorded tool call {first.call_id!r} (sequence {first.sequence}, tool "
            f"{first.tool_name!r}) matches no tool call in the message view, so the trial's "
            f"two views of itself disagree. {len(unlinkable)} of {len(self._records)} "
            "recorded calls are unlinkable."
        )

    def _append_call(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: ToolExecutorIdentity | None,
    ) -> None:
        first = self._call_positions.get(call_id)
        if first is not None:
            raise TimelineInconsistencyError(
                f"tool-call id {call_id!r} occurs twice in the trial, at positions {first} "
                f"and {len(self._events)}. The id is the only key that joins a call to the "
                "result it produced, so a duplicate leaves the join ambiguous."
            )
        self._call_positions[call_id] = len(self._events)
        self._append(
            kind=TraceEventKind.TOOL_CALL,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=executor,
        )

    def _append_result(self, record: RecordedToolCall) -> None:
        self._append(
            kind=TraceEventKind.TOOL_RESULT,
            call_id=record.call_id,
            tool_name=record.tool_name,
            executor=record.executor,
            status=record.status,
            result=record.output,
            latency_seconds=record.latency_seconds,
        )

    def _append_message_result(self, call: ToolCall) -> None:
        """The result as the message view preserved it: text, and nothing a record carries."""
        self._append(
            kind=TraceEventKind.TOOL_RESULT,
            call_id=call.id,
            tool_name=call.name,
            result=self._message_results[call.id],
        )

    def _append(
        self,
        *,
        kind: TraceEventKind,
        text: str | None = None,
        call_id: str | None = None,
        tool_name: str | None = None,
        executor: ToolExecutorIdentity | None = None,
        arguments: dict[str, Any] | None = None,
        status: ToolExecutionStatus | None = None,
        result: str | None = None,
        latency_seconds: float | None = None,
    ) -> None:
        self._events.append(
            TraceEvent(
                position=len(self._events),
                turn_index=max(self._generations - 1, 0),
                kind=kind,
                text=text,
                call_id=call_id,
                tool_name=tool_name,
                executor=executor,
                arguments=arguments,
                status=status,
                result=result,
                latency_seconds=latency_seconds,
            )
        )
