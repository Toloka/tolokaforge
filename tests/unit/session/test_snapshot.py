"""Unit tests for :meth:`InProcessTrialSession.snapshot` — M1 sub-4b.

Covers the durable event/intervention history the ``open_agent_loop``
trajectory-trace section persists to disk.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tolokaforge.session import (
    AssistantMessage,
    InjectMessage,
    InProcessTrialSession,
    ParticipantRole,
    TerminalReached,
    ToolCallEmitted,
    TurnStarted,
)
from tolokaforge.session._status import TrialStatus

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _make_session_with_activity() -> InProcessTrialSession:
    session = InProcessTrialSession(trial_id="MAN-34:0")
    session.publish(
        TurnStarted(trial_id="MAN-34:0", seq=session.next_seq(), timestamp=_NOW, turn_index=0)
    )
    session.publish(
        AssistantMessage(
            trial_id="MAN-34:0",
            seq=session.next_seq(),
            timestamp=_NOW,
            content_preview="hi",
        )
    )
    session.publish(
        ToolCallEmitted(
            trial_id="MAN-34:0",
            seq=session.next_seq(),
            timestamp=_NOW,
            call_id="c1",
            tool_name="lookup",
            arguments_preview='{"q":"x"}',
        )
    )
    session.publish(
        TerminalReached(
            trial_id="MAN-34:0",
            seq=session.next_seq(),
            timestamp=_NOW,
            status=TrialStatus.COMPLETED,
        )
    )
    return session


class TestEventHistory:
    def test_snapshot_records_every_published_event_even_without_participants(self):
        """The trace must be faithful even when no participant is attached —
        events go into per-participant queues *and* the durable history.
        """
        session = _make_session_with_activity()
        snap = session.snapshot()

        assert snap["trial_id"] == "MAN-34:0"
        assert snap["closed"] is True
        assert len(snap["events"]) == 4
        assert snap["events"][0]["kind"] == "turn_started"
        assert snap["events"][-1]["kind"] == "terminal_reached"

    def test_snapshot_events_preserve_seq_order(self):
        session = _make_session_with_activity()
        snap = session.snapshot()
        seqs = [e["seq"] for e in snap["events"]]
        assert seqs == sorted(seqs)
        assert seqs[0] == 0

    def test_snapshot_events_pass_through_pydantic_json_serialization(self):
        session = _make_session_with_activity()
        snap = session.snapshot()
        # timestamp is ISO-formatted (mode="json"), status is a string
        term = snap["events"][-1]
        assert isinstance(term["timestamp"], str)
        assert term["status"] == "completed"


class TestInterventionHistory:
    def _session_with_intervention(self) -> tuple[InProcessTrialSession, InjectMessage]:
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        intervention = InjectMessage(
            trial_id="t:0",
            attach_to_seq=0,
            participant_id="p1",
            timestamp=_NOW,
            content="try /v2/auth",
        )
        session.interventions().submit(handle, intervention)
        return session, intervention

    def test_snapshot_records_submitted_interventions(self):
        session, intervention = self._session_with_intervention()
        snap = session.snapshot()
        assert len(snap["interventions"]) == 1
        rec = snap["interventions"][0]
        assert rec["intervention"]["kind"] == "inject_message"
        assert rec["intervention"]["content"] == "try /v2/auth"

    def test_snapshot_records_ack_outcome(self):
        session, _ = self._session_with_intervention()
        snap = session.snapshot()
        rec = snap["interventions"][0]
        # In-process transport returns "queued" synchronously
        assert rec["ack_outcome"] == "queued"

    def test_snapshot_intervention_survives_queue_drain(self):
        """The pause-pump-side queue drain must not empty the durable trace
        history — that's the whole point of separating _intervention_history
        from _intervention_queue.
        """
        session, _ = self._session_with_intervention()
        drained = session.drain_pending_interventions()
        assert len(drained) == 1
        # History persists after drain
        snap = session.snapshot()
        assert len(snap["interventions"]) == 1

    def test_snapshot_records_multiple_interventions_in_order(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        for i, content in enumerate(["a", "b", "c"]):
            session.interventions().submit(
                handle,
                InjectMessage(
                    trial_id="t:0",
                    attach_to_seq=i,
                    participant_id="p1",
                    timestamp=_NOW,
                    content=content,
                ),
            )
        snap = session.snapshot()
        assert [rec["intervention"]["content"] for rec in snap["interventions"]] == ["a", "b", "c"]


class TestSnapshotSerialisability:
    def test_snapshot_is_yaml_serialisable(self):
        """The snapshot must land on disk cleanly. Verify with an in-memory
        yaml.safe_dump round-trip.
        """
        import yaml

        session = _make_session_with_activity()
        snap = session.snapshot()
        dumped = yaml.safe_dump(snap, sort_keys=False)
        parsed = yaml.safe_load(dumped)

        assert parsed["trial_id"] == "MAN-34:0"
        assert len(parsed["events"]) == 4
        assert parsed["events"][0]["kind"] == "turn_started"

    def test_empty_session_snapshot_is_stable(self):
        session = InProcessTrialSession(trial_id="t:0")
        snap = session.snapshot()
        assert snap == {
            "trial_id": "t:0",
            "closed": False,
            "events": [],
            "interventions": [],
        }
