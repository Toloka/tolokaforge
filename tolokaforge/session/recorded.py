"""``RecordedTrialSession`` — replay a captured trajectory as a live session.

A recorded session lets participants be developed and tested without a live
conductor. Events are pre-loaded and yielded in ``seq`` order. Interventions
are captured for post-session inspection but do not affect the underlying
recording — every submit returns an :class:`InterventionAck` with outcome
``rejected`` and a reason noting the recorded context.

Two factories:

* :meth:`RecordedTrialSession.from_events` — build directly from a list of
  :class:`TrialEvent` — the workhorse for unit tests.
* :meth:`RecordedTrialSession.from_trajectory_yaml` — synthesize events from
  a captured ``trajectory.yaml`` file, optionally truncated at a given turn.
  This is the substrate the demo uses to replay past runs into the copilot.

M0 owns the Protocols + this recorded transport. Live in-process transport
lands in M1.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from tolokaforge.session._status import TerminationReason, TrialStatus
from tolokaforge.session.events import (
    AssistantMessage,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TurnStarted,
)
from tolokaforge.session.interventions import InterventionAck, TrialIntervention
from tolokaforge.session.protocols import (
    ParticipantHandle,
    ParticipantRole,
    TrialEvents,
    TrialInterventions,
)

__all__ = ["RecordedTrialSession"]

_PREVIEW_LEN = 240


@dataclass
class _RecordedEvents:
    """Bound :class:`TrialEvents` implementation for :class:`RecordedTrialSession`."""

    events: list[TrialEvent]

    def iter_events(self, handle: ParticipantHandle) -> Iterator[TrialEvent]:
        for event in self.events:
            if event.trial_id != handle.trial_id:
                raise ValueError(
                    f"Recorded session mismatch: event trial_id={event.trial_id!r} "
                    f"but handle trial_id={handle.trial_id!r}"
                )
            yield event


@dataclass
class _RecordedInterventions:
    """Bound :class:`TrialInterventions` implementation. Records every submit
    into ``captured`` for post-session inspection and rejects with an ack that
    reflects the recorded context.
    """

    trial_id: str
    captured: list[TrialIntervention] = field(default_factory=list)

    def submit(
        self,
        handle: ParticipantHandle,
        intervention: TrialIntervention,
    ) -> InterventionAck:
        if intervention.trial_id != self.trial_id:
            raise ValueError(
                f"Intervention trial_id={intervention.trial_id!r} does not match "
                f"session trial_id={self.trial_id!r}"
            )
        if intervention.participant_id != handle.participant_id:
            raise ValueError(
                f"Intervention participant_id={intervention.participant_id!r} does not match "
                f"handle participant_id={handle.participant_id!r}"
            )
        self.captured.append(intervention)
        return InterventionAck(
            intervention_kind=intervention.kind,
            trial_id=self.trial_id,
            participant_id=handle.participant_id,
            outcome="rejected",
            reason="RecordedTrialSession — intervention captured but not applied to a live trial.",
        )


class RecordedTrialSession:
    """Read-only session backed by a pre-recorded event list.

    Participants attach exactly as they would to a live session. Events yield
    in ``seq`` order via :meth:`events`. Interventions are captured and returned
    with a ``rejected`` ack via :meth:`interventions`.
    """

    def __init__(self, trial_id: str, events: list[TrialEvent]) -> None:
        self._trial_id = trial_id
        self._events = _RecordedEvents(events)
        self._interventions = _RecordedInterventions(trial_id=trial_id)
        self._attached: dict[str, ParticipantHandle] = {}

    @property
    def trial_id(self) -> str:
        return self._trial_id

    @property
    def captured_interventions(self) -> list[TrialIntervention]:
        """Interventions submitted during the session, in submit order.

        Intended for tests and demo post-mortems — a live session would apply
        these to the running trial and record them in the trajectory trace.
        """
        return list(self._interventions.captured)

    def attach(
        self,
        participant_id: str,
        role: ParticipantRole,
    ) -> ParticipantHandle:
        if participant_id in self._attached:
            raise ValueError(f"Participant {participant_id!r} is already attached to this session.")
        handle = ParticipantHandle(
            participant_id=participant_id,
            role=role,
            trial_id=self._trial_id,
        )
        self._attached[participant_id] = handle
        return handle

    def detach(self, handle: ParticipantHandle) -> None:
        self._attached.pop(handle.participant_id, None)

    def events(self) -> TrialEvents:
        return self._events

    def interventions(self) -> TrialInterventions:
        return self._interventions

    @classmethod
    def from_events(cls, trial_id: str, events: list[TrialEvent]) -> RecordedTrialSession:
        """Direct constructor — used by tests and hand-authored fixtures."""
        return cls(trial_id=trial_id, events=events)

    @classmethod
    def from_trajectory_yaml(
        cls,
        path: Path,
        trial_id: str | None = None,
        truncate_at_turn: int | None = None,
    ) -> RecordedTrialSession:
        """Synthesize a session from a captured ``trajectory.yaml``.

        Reads the trajectory's message list and emits one event per natural
        seam (turn boundary, assistant message, tool call, tool result). When
        ``truncate_at_turn`` is set, the synthesis stops after that many
        completed agent turns and DOES NOT emit a :class:`TerminalReached` —
        this is how the demo pauses a recorded trial "mid-flight" for the
        copilot to react to.

        ``trial_id`` defaults to the trajectory's ``task_id:trial_index``.
        """
        with path.open() as f:
            raw = yaml.safe_load(f) or {}

        derived_trial_id = (
            trial_id or f"{raw.get('task_id', 'unknown')}:{raw.get('trial_index', 0)}"
        )
        events = _synthesize_events_from_messages(
            trial_id=derived_trial_id,
            messages=raw.get("messages", []),
            terminal_status=raw.get("status"),
            terminal_reason=raw.get("termination_reason"),
            truncate_at_turn=truncate_at_turn,
        )
        return cls(trial_id=derived_trial_id, events=events)


def _synthesize_events_from_messages(
    trial_id: str,
    messages: list[dict],
    terminal_status: str | None,
    terminal_reason: str | None,
    truncate_at_turn: int | None,
) -> list[TrialEvent]:
    """Walk a captured message list and emit session events at natural seams.

    Deliberately narrow: emits ``TurnStarted``, ``ToolCallEmitted``,
    ``ToolResultObserved``, ``AssistantMessage``, and (when not truncated)
    ``TerminalReached``. Budget updates require metrics that live in a
    separate file; the demo can inject them later.
    """
    events: list[TrialEvent] = []
    seq = 0
    turn_index = 0
    now = datetime.now(UTC)
    turns_completed = 0

    for msg in messages:
        role = msg.get("role")

        if role == "user":
            events.append(
                TurnStarted(
                    trial_id=trial_id,
                    seq=seq,
                    timestamp=now,
                    turn_index=turn_index,
                )
            )
            seq += 1
            turn_index += 1
            continue

        if role == "assistant":
            content = msg.get("content") or ""
            if content:
                events.append(
                    AssistantMessage(
                        trial_id=trial_id,
                        seq=seq,
                        timestamp=now,
                        content_preview=_preview(content),
                        has_reasoning=bool(msg.get("reasoning") or msg.get("thinking_blocks")),
                    )
                )
                seq += 1

            for tc in msg.get("tool_calls", []) or []:
                events.append(
                    ToolCallEmitted(
                        trial_id=trial_id,
                        seq=seq,
                        timestamp=now,
                        call_id=str(tc.get("id") or f"call_{seq}"),
                        tool_name=str(
                            tc.get("name") or tc.get("function", {}).get("name") or "unknown"
                        ),
                        arguments_preview=_preview(
                            str(
                                tc.get("arguments") or tc.get("function", {}).get("arguments") or ""
                            )
                        ),
                    )
                )
                seq += 1

            turns_completed += 1
            if truncate_at_turn is not None and turns_completed >= truncate_at_turn:
                return events
            continue

        if role == "tool":
            events.append(
                ToolResultObserved(
                    trial_id=trial_id,
                    seq=seq,
                    timestamp=now,
                    call_id=str(msg.get("tool_call_id") or f"call_{seq}"),
                    tool_name=str(msg.get("name") or "unknown"),
                    duration_ms=int(msg.get("duration_ms") or 0),
                    truncated_preview=_preview(str(msg.get("content") or "")),
                )
            )
            seq += 1
            continue

    if truncate_at_turn is None:
        events.append(
            TerminalReached(
                trial_id=trial_id,
                seq=seq,
                timestamp=now,
                status=_coerce_status(terminal_status),
                termination_reason=_coerce_reason(terminal_reason),
            )
        )

    return events


def _preview(text: str) -> str:
    if len(text) <= _PREVIEW_LEN:
        return text
    return text[: _PREVIEW_LEN - 1] + "…"


def _coerce_status(value: str | None) -> TrialStatus:
    if value is None:
        return TrialStatus.ERROR
    try:
        return TrialStatus(value)
    except ValueError:
        return TrialStatus.ERROR


def _coerce_reason(value: str | None) -> TerminationReason | None:
    if value is None:
        return None
    try:
        return TerminationReason(value)
    except ValueError:
        return None
