"""Unit tests for :mod:`tolokaforge.session` — M0 surface.

Covers:

* Event and intervention discriminated-union round-tripping.
* :class:`RecordedTrialSession` attach / iterate / submit contract.
* Trajectory-YAML synthesis into a replayable event stream, with and without
  mid-flight truncation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter

from tolokaforge.session import (
    ApproveTool,
    AssistantMessage,
    InjectMessage,
    ParticipantRole,
    RecordedTrialSession,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TrialIntervention,
    TurnStarted,
)
from tolokaforge.session._status import TrialStatus

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _turn(trial_id: str, seq: int, turn_index: int) -> TurnStarted:
    return TurnStarted(trial_id=trial_id, seq=seq, timestamp=_NOW, turn_index=turn_index)


def _assistant(trial_id: str, seq: int, content: str) -> AssistantMessage:
    return AssistantMessage(
        trial_id=trial_id,
        seq=seq,
        timestamp=_NOW,
        content_preview=content,
    )


def _tool_call(trial_id: str, seq: int, name: str) -> ToolCallEmitted:
    return ToolCallEmitted(
        trial_id=trial_id,
        seq=seq,
        timestamp=_NOW,
        call_id=f"c_{seq}",
        tool_name=name,
        arguments_preview="{}",
    )


class TestEventUnion:
    def test_discriminated_roundtrip_turn_started(self):
        adapter: TypeAdapter[TrialEvent] = TypeAdapter(TrialEvent)
        event = _turn("t:0", 0, 0)
        payload = adapter.dump_python(event)
        assert payload["kind"] == "turn_started"
        parsed = adapter.validate_python(payload)
        assert isinstance(parsed, TurnStarted)
        assert parsed.turn_index == 0

    def test_discriminated_roundtrip_tool_result(self):
        adapter: TypeAdapter[TrialEvent] = TypeAdapter(TrialEvent)
        event = ToolResultObserved(
            trial_id="t:0",
            seq=3,
            timestamp=_NOW,
            call_id="c_3",
            tool_name="lookup",
            duration_ms=142,
            truncated_preview="ok",
        )
        payload = adapter.dump_python(event)
        parsed = adapter.validate_python(payload)
        assert isinstance(parsed, ToolResultObserved)
        assert parsed.duration_ms == 142

    def test_extra_fields_rejected(self):
        with pytest.raises(ValueError):
            TurnStarted(  # type: ignore[call-arg]
                trial_id="t:0",
                seq=0,
                timestamp=_NOW,
                turn_index=0,
                bogus="nope",
            )

    def test_seq_must_be_non_negative(self):
        with pytest.raises(ValueError):
            TurnStarted(trial_id="t:0", seq=-1, timestamp=_NOW, turn_index=0)


class TestInterventionUnion:
    def test_discriminated_roundtrip_inject(self):
        adapter: TypeAdapter[TrialIntervention] = TypeAdapter(TrialIntervention)
        intervention = InjectMessage(
            trial_id="t:0",
            attach_to_seq=2,
            participant_id="p_llm",
            timestamp=_NOW,
            content="try /v2/auth",
        )
        payload = adapter.dump_python(intervention)
        assert payload["kind"] == "inject_message"
        parsed = adapter.validate_python(payload)
        assert isinstance(parsed, InjectMessage)
        assert parsed.content == "try /v2/auth"

    def test_inject_content_must_be_nonempty(self):
        with pytest.raises(ValueError):
            InjectMessage(
                trial_id="t:0",
                attach_to_seq=0,
                participant_id="p",
                timestamp=_NOW,
                content="",
            )


class TestRecordedTrialSessionFromEvents:
    def _session(self) -> RecordedTrialSession:
        events: list[TrialEvent] = [
            _turn("t:0", 0, 0),
            _assistant("t:0", 1, "hello"),
            _tool_call("t:0", 2, "lookup"),
            TerminalReached(trial_id="t:0", seq=3, timestamp=_NOW, status=TrialStatus.COMPLETED),
        ]
        return RecordedTrialSession.from_events(trial_id="t:0", events=events)

    def test_attach_returns_confirmed_handle(self):
        session = self._session()
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        assert handle.trial_id == "t:0"
        assert handle.role is ParticipantRole.PARTICIPANT
        assert handle.participant_id == "p_llm"

    def test_double_attach_same_id_raises(self):
        session = self._session()
        session.attach("p_llm", ParticipantRole.PARTICIPANT)
        with pytest.raises(ValueError):
            session.attach("p_llm", ParticipantRole.OBSERVER)

    def test_iter_events_yields_in_seq_order(self):
        session = self._session()
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        seqs = [event.seq for event in session.events().iter_events(handle)]
        assert seqs == [0, 1, 2, 3]

    def test_iter_events_rejects_cross_trial_handle(self):
        session = self._session()
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        # Poison the events list with a foreign trial_id to prove the check
        session._events.events.append(_turn("other:0", 4, 1))  # type: ignore[attr-defined]
        with pytest.raises(ValueError):
            list(session.events().iter_events(handle))

    def test_submit_intervention_records_and_rejects(self):
        session = self._session()
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        intervention = InjectMessage(
            trial_id="t:0",
            attach_to_seq=1,
            participant_id="p_llm",
            timestamp=_NOW,
            content="try again",
        )
        ack = session.interventions().submit(handle, intervention)
        assert ack.outcome == "rejected"
        assert "Recorded" in (ack.reason or "")
        assert session.captured_interventions == [intervention]

    def test_submit_wrong_participant_id_raises(self):
        session = self._session()
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        with pytest.raises(ValueError):
            session.interventions().submit(
                handle,
                ApproveTool(
                    trial_id="t:0",
                    attach_to_seq=2,
                    participant_id="p_other",
                    timestamp=_NOW,
                    call_id="c_2",
                ),
            )

    def test_detach_is_idempotent(self):
        session = self._session()
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        session.detach(handle)
        session.detach(handle)


class TestRecordedTrialSessionFromTrajectoryYaml:
    def _write_trajectory(self, tmp_path: Path, terminal_status: str | None = "completed") -> Path:
        traj = {
            "task_id": "MAN-34",
            "trial_index": 0,
            "status": terminal_status,
            "termination_reason": None,
            "messages": [
                {"role": "user", "content": "Please do X."},
                {
                    "role": "assistant",
                    "content": "I'll try.",
                    "tool_calls": [
                        {"id": "call_A", "name": "lookup", "arguments": '{"q": "X"}'},
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_A",
                    "name": "lookup",
                    "content": "no results",
                },
                {
                    "role": "assistant",
                    "content": "Let me try again.",
                    "tool_calls": [
                        {"id": "call_B", "name": "lookup", "arguments": '{"q": "X refined"}'},
                    ],
                },
                {"role": "tool", "tool_call_id": "call_B", "name": "lookup", "content": "found"},
                {"role": "assistant", "content": "Done."},
            ],
        }
        path = tmp_path / "trajectory.yaml"
        with path.open("w") as f:
            yaml.safe_dump(traj, f)
        return path

    def test_full_synthesis_ends_with_terminal_event(self, tmp_path: Path):
        path = self._write_trajectory(tmp_path)
        session = RecordedTrialSession.from_trajectory_yaml(path)
        assert session.trial_id == "MAN-34:0"
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        events = list(session.events().iter_events(handle))
        assert isinstance(events[-1], TerminalReached)
        assert events[-1].status is TrialStatus.COMPLETED
        # First event opens turn 0; second is the user's assistant reply
        assert isinstance(events[0], TurnStarted)

    def test_truncation_stops_after_n_assistant_turns_and_omits_terminal(self, tmp_path: Path):
        path = self._write_trajectory(tmp_path)
        session = RecordedTrialSession.from_trajectory_yaml(path, truncate_at_turn=1)
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        events = list(session.events().iter_events(handle))
        assert not any(isinstance(e, TerminalReached) for e in events)
        # Exactly one AssistantMessage before truncation
        assistant_events = [e for e in events if isinstance(e, AssistantMessage)]
        assert len(assistant_events) == 1

    def test_tool_calls_and_results_are_paired_by_call_id(self, tmp_path: Path):
        path = self._write_trajectory(tmp_path)
        session = RecordedTrialSession.from_trajectory_yaml(path)
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        events = list(session.events().iter_events(handle))
        call_ids = [e.call_id for e in events if isinstance(e, ToolCallEmitted)]
        result_ids = [e.call_id for e in events if isinstance(e, ToolResultObserved)]
        assert call_ids == ["call_A", "call_B"]
        assert result_ids == ["call_A", "call_B"]

    def test_seq_is_monotonic_across_synthesis(self, tmp_path: Path):
        path = self._write_trajectory(tmp_path)
        session = RecordedTrialSession.from_trajectory_yaml(path)
        handle = session.attach("p_llm", ParticipantRole.PARTICIPANT)
        seqs = [e.seq for e in session.events().iter_events(handle)]
        assert seqs == sorted(seqs)
        assert seqs[0] == 0
