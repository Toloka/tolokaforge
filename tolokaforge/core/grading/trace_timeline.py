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

The join key is the trial's **episode-unique call id**
(:mod:`tolokaforge.core.tool_call_ids`), not the raw provider id: each view
derives its own keys from its own observation order — the message view in
declaration order, the record in ``sequence`` order — so the k-th declaration of
an id pairs with the k-th record of it. What makes that pairing unambiguous is
the suffix invariant: a turn's calls execute in declaration order and the episode
stops at the first failure, so the declarations that never executed are always a
trailing suffix rather than a gap in the middle.

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
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds, episode_unique_call_ids

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
    untruncated and carries no ``Error:`` prefix, unlike the ``role: tool``
    message — and falls back to that message on a timeline that carries no
    record view at all.
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

    Raised rather than resolved. A record naming a call the message view does not
    contain, a result answering one, or a record naming a different tool than the
    declaration it paired with is a broken harness invariant, not task data, and
    picking a winner would produce exactly the ambiguous join the key exists to
    prevent.
    """


_KIND_BY_ROLE = {
    MessageRole.ASSISTANT: TraceEventKind.ASSISTANT_MESSAGE,
    MessageRole.USER: TraceEventKind.USER_MESSAGE,
}


# The literal prefix ``core/loop.py`` writes onto a failed tool call's
# message body. Stripping it in the message-only branch of trace-timeline
# reconstruction is what keeps ``result:`` matchers reading the same on a
# bundle carrying ``tool_log.yaml`` and on the same bundle re-graded from
# messages alone (#977).
_ERROR_MESSAGE_PREFIX = "Error: "


@dataclass(frozen=True)
class _DeclaredCall:
    """One call the message view asks for, under the key the trial joins it by."""

    key: str
    call: ToolCall


def build_trial_timeline(
    messages: Sequence[Message],
    recorded_calls: Sequence[RecordedToolCall],
    termination_reason: TerminationReason | None,
) -> TrialTimeline:
    """Build the trial's timeline from its message view and its tool-call record.

    Event order follows the message view; within one message, tool calls follow
    recorded execution order, with calls that never executed after them in
    declaration order. A call and its result are joined by the episode-unique key
    each view derives from its own observation order, never by position, and every
    event carries that key as its ``call_id``.

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
        TimelineInconsistencyError: a record's key matches no declared call, a
            record names a different tool than the declaration it paired with, or
            — with no records — a tool result's key matches no declared call.
    """
    records = _records_by_episode_key(recorded_calls)
    turns = [message for message in messages if message.role in _KIND_BY_ROLE]
    declared = _declared_calls(turns)
    if turns:
        _require_records_reconcile(records, declared)
    # The record wins wherever both views describe one call, so the message-side
    # results are read only in its absence.
    message_results = {} if records else _index_message_results(messages, declared)
    builder = _TimelineBuilder(records, message_results)
    if turns:
        builder.emit_message_view(turns, declared)
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


def _records_by_episode_key(
    recorded_calls: Sequence[RecordedToolCall],
) -> dict[str, RecordedToolCall]:
    """The record view keyed by occurrence, read in ``sequence`` — execution — order."""
    in_sequence = sorted(recorded_calls, key=lambda record: record.sequence)
    keys = episode_unique_call_ids([record.call_id for record in in_sequence])
    return dict(zip(keys, in_sequence, strict=True))


def _declared_calls(turns: Sequence[Message]) -> tuple[tuple[_DeclaredCall, ...], ...]:
    """The message view's calls keyed by occurrence, one tuple per turn.

    Declaration order — message order, then position within ``tool_calls`` — is
    the order the keys are derived in, so the k-th declaration of an id carries
    the same key as the k-th record of it.
    """
    assigner = EpisodeUniqueCallIds()
    return tuple(
        tuple(
            _DeclaredCall(key=assigner.assign(call.id), call=call)
            for call in (message.tool_calls or [])
        )
        for message in turns
    )


def _declarations_by_key(declared: Sequence[Sequence[_DeclaredCall]]) -> dict[str, ToolCall]:
    return {entry.key: entry.call for turn in declared for entry in turn}


def _require_records_reconcile(
    records: dict[str, RecordedToolCall], declared: Sequence[Sequence[_DeclaredCall]]
) -> None:
    """Every record answers a declaration, and names the tool that declaration named."""
    declarations = _declarations_by_key(declared)
    _require_every_record_linkable(records, declarations)
    _require_every_record_names_its_declared_tool(records, declarations)


def _require_every_record_linkable(
    records: dict[str, RecordedToolCall], declarations: dict[str, ToolCall]
) -> None:
    unlinkable = sorted(
        ((key, record) for key, record in records.items() if key not in declarations),
        key=lambda entry: entry[1].sequence,
    )
    if not unlinkable:
        return
    key, first = unlinkable[0]
    raise TimelineInconsistencyError(
        f"recorded tool call {key!r} (sequence {first.sequence}, tool "
        f"{first.tool_name!r}) matches no tool call in the message view, so the trial's "
        f"two views of itself disagree. {len(unlinkable)} of {len(records)} "
        "recorded calls are unlinkable."
    )


def _require_every_record_names_its_declared_tool(
    records: dict[str, RecordedToolCall], declarations: dict[str, ToolCall]
) -> None:
    """The independent corroboration that the occurrence-order pairing is right.

    Both views name the tool for every call, so where they disagree the pairing
    that put them together is wrong — and a mis-pairing nothing notices is what
    an order-based join has to be defended against.
    """
    for key, record in records.items():
        declared = declarations[key]
        if record.tool_name == declared.name:
            continue
        raise TimelineInconsistencyError(
            f"recorded tool call {key!r} (sequence {record.sequence}) names tool "
            f"{record.tool_name!r}, but the message view declares that call as "
            f"{declared.name!r}. The two views disagree about what ran, so the call "
            "and the result paired with it do not describe one call."
        )


def _index_message_results(
    messages: Sequence[Message], declared: Sequence[Sequence[_DeclaredCall]]
) -> dict[str, str]:
    """The text each ``role: tool`` message carries, keyed by the call it answers.

    Read only when no record view was supplied. That text is on disk in every
    recorded bundle, so dropping it would hide a tool's own output from every
    phrase rule — and hide it in the agent's favour for a disallowed pattern.

    Keys are derived in message order, so the k-th result naming an id answers the
    k-th declaration of it.

    Raises:
        TimelineInconsistencyError: a result's key matches no declared call.
    """
    declared_keys = set(_declarations_by_key(declared))
    assigner = EpisodeUniqueCallIds()
    results: dict[str, str] = {}
    for index, message in enumerate(messages):
        if message.role is not MessageRole.TOOL:
            continue
        key = None if message.tool_call_id is None else assigner.assign(message.tool_call_id)
        if key is None or key not in declared_keys:
            raise TimelineInconsistencyError(
                f"the tool result message at index {index} answers tool-call id {key!r}, "
                "which no tool call in the trial's message view declares. Its text is the "
                "only surviving evidence of what that tool returned, so it can be neither "
                "joined to a call nor dropped."
            )
        results[key] = message.content
    return results


def _execution_order(
    declared: Sequence[_DeclaredCall], records: dict[str, RecordedToolCall]
) -> list[_DeclaredCall]:
    """One message's calls in the order they ran, then the ones that never ran.

    ``sequence`` is stamped at execution time, so it is execution order rather
    than the order the assistant declared the calls in.
    """
    executed = sorted(
        (entry for entry in declared if entry.key in records),
        key=lambda entry: records[entry.key].sequence,
    )
    return executed + [entry for entry in declared if entry.key not in records]


class _TimelineBuilder:
    """Accumulates events, assigning ``position`` and ``turn_index`` as it goes."""

    def __init__(
        self, records: dict[str, RecordedToolCall], message_results: dict[str, str]
    ) -> None:
        self._records = records
        self._message_results = message_results
        self._events: list[TraceEvent] = []
        self._generations = 0

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def emit_message_view(
        self, turns: Sequence[Message], declared: Sequence[Sequence[_DeclaredCall]]
    ) -> None:
        for message, calls in zip(turns, declared, strict=True):
            self._emit_turn(message, calls)

    def emit_records_alone(self) -> None:
        for key, record in sorted(self._records.items(), key=lambda entry: entry[1].sequence):
            self._append(
                kind=TraceEventKind.TOOL_CALL,
                call_id=key,
                tool_name=record.tool_name,
                arguments=record.arguments,
                executor=record.executor,
            )
            self._append_result(key, record)

    def _emit_turn(self, message: Message, declared: Sequence[_DeclaredCall]) -> None:
        if message.role is MessageRole.ASSISTANT:
            self._generations += 1
        self._append(kind=_KIND_BY_ROLE[message.role], text=message.content)
        for entry in _execution_order(declared, self._records):
            self._emit_call(entry)

    def _emit_call(self, declared: _DeclaredCall) -> None:
        record = self._records.get(declared.key)
        self._append(
            kind=TraceEventKind.TOOL_CALL,
            call_id=declared.key,
            tool_name=declared.call.name,
            arguments=declared.call.arguments,
            executor=record.executor if record is not None else None,
        )
        if record is not None:
            self._append_result(declared.key, record)
        elif declared.key in self._message_results:
            self._append_message_result(declared)

    def _append_result(self, key: str, record: RecordedToolCall) -> None:
        self._append(
            kind=TraceEventKind.TOOL_RESULT,
            call_id=key,
            tool_name=record.tool_name,
            executor=record.executor,
            status=record.status,
            result=record.output,
            latency_seconds=record.latency_seconds,
        )

    def _append_message_result(self, declared: _DeclaredCall) -> None:
        """The result as the message view preserved it: text, and nothing a record carries.

        Strips the ``"Error: "`` prefix ``core/loop.py:460`` writes onto the
        message body for a failed call, so ``result:`` reads the same on
        this branch as on ``_append_result`` (which pulls raw ``record.output``).
        Without this, ``result: {regex: "^insufficient funds"}`` passed on a
        bundle carrying ``tool_log.yaml`` and failed on the same bundle
        re-graded without it — the "one text on both substrates" claim (#977)
        would hold only for records, not for messages.
        """
        raw = self._message_results[declared.key]
        result = raw[len(_ERROR_MESSAGE_PREFIX) :] if raw.startswith(_ERROR_MESSAGE_PREFIX) else raw
        self._append(
            kind=TraceEventKind.TOOL_RESULT,
            call_id=declared.key,
            tool_name=declared.call.name,
            result=result,
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
