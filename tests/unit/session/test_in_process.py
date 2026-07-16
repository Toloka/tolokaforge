"""Unit tests for :class:`tolokaforge.session.InProcessTrialSession` — M1 sub-1.

Covers:

* Producer-side ``next_seq`` monotonicity across threads.
* Broadcast fan-out: every attached participant sees every event.
* Late attach: joins mid-stream and sees only events from the current
  ``seq`` forward, never history.
* Terminal signalling: :class:`TerminalReached` closes the session and
  releases every blocked ``iter_events`` iterator.
* Intervention queueing: ``submit`` returns ``outcome="queued"``
  immediately; the intervention appears in ``drain_pending_interventions``
  in submission order.
* Detach releases the iterator without disturbing other participants.
* Attach / publish after close raise instead of silently dropping.
* Handle validation: cross-trial handle, cross-participant intervention,
  detached-participant submit all raise.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from tolokaforge.session import (
    AssistantMessage,
    InjectMessage,
    InProcessTrialSession,
    ParticipantRole,
    QueuedIntervention,
    TerminalReached,
    ToolCallEmitted,
    TrialEvent,
    TurnStarted,
)
from tolokaforge.session._status import TrialStatus

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _turn(session: InProcessTrialSession, turn_index: int) -> TurnStarted:
    return TurnStarted(
        trial_id=session.trial_id,
        seq=session.next_seq(),
        timestamp=_NOW,
        turn_index=turn_index,
    )


def _assistant(session: InProcessTrialSession, content: str = "hi") -> AssistantMessage:
    return AssistantMessage(
        trial_id=session.trial_id,
        seq=session.next_seq(),
        timestamp=_NOW,
        content_preview=content,
    )


def _terminal(session: InProcessTrialSession) -> TerminalReached:
    return TerminalReached(
        trial_id=session.trial_id,
        seq=session.next_seq(),
        timestamp=_NOW,
        status=TrialStatus.COMPLETED,
    )


class TestSeqAllocation:
    def test_next_seq_is_monotonic(self):
        session = InProcessTrialSession(trial_id="t:0")
        seqs = [session.next_seq() for _ in range(20)]
        assert seqs == list(range(20))

    def test_next_seq_is_thread_safe(self):
        session = InProcessTrialSession(trial_id="t:0")
        collected: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            local = [session.next_seq() for _ in range(50)]
            with lock:
                collected.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No duplicates, dense range regardless of interleaving
        assert sorted(collected) == list(range(8 * 50))


class TestAttachDetach:
    def test_attach_returns_confirmed_handle(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        assert handle.trial_id == "t:0"
        assert handle.role is ParticipantRole.PARTICIPANT
        assert handle.participant_id == "p1"
        assert session.attached_participant_ids == ["p1"]

    def test_double_attach_same_id_raises(self):
        session = InProcessTrialSession(trial_id="t:0")
        session.attach("p1", ParticipantRole.PARTICIPANT)
        with pytest.raises(ValueError):
            session.attach("p1", ParticipantRole.OBSERVER)

    def test_detach_is_idempotent(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        session.detach(handle)
        # Second detach is a no-op
        session.detach(handle)
        assert session.attached_participant_ids == []

    def test_attach_after_close_raises(self):
        session = InProcessTrialSession(trial_id="t:0")
        session.close()
        with pytest.raises(ValueError):
            session.attach("p1", ParticipantRole.PARTICIPANT)


class TestEventBroadcast:
    def test_all_attached_participants_receive_every_event(self):
        session = InProcessTrialSession(trial_id="t:0")
        h1 = session.attach("p1", ParticipantRole.PARTICIPANT)
        h2 = session.attach("p2", ParticipantRole.OBSERVER)

        session.publish(_turn(session, 0))
        session.publish(_assistant(session, "hello"))
        session.publish(_terminal(session))

        p1 = list(session.events().iter_events(h1))
        p2 = list(session.events().iter_events(h2))

        assert [e.seq for e in p1] == [0, 1, 2]
        assert [e.seq for e in p2] == [0, 1, 2]
        assert isinstance(p1[-1], TerminalReached)

    def test_late_attach_only_sees_events_from_attachment_forward(self):
        session = InProcessTrialSession(trial_id="t:0")

        h_early = session.attach("early", ParticipantRole.PARTICIPANT)
        session.publish(_turn(session, 0))  # seq 0
        session.publish(_assistant(session, "before"))  # seq 1

        h_late = session.attach("late", ParticipantRole.OBSERVER)
        session.publish(_assistant(session, "after"))  # seq 2
        session.publish(_terminal(session))  # seq 3

        early_seqs = [e.seq for e in session.events().iter_events(h_early)]
        late_seqs = [e.seq for e in session.events().iter_events(h_late)]

        assert early_seqs == [0, 1, 2, 3]
        assert late_seqs == [2, 3]

    def test_iter_events_blocks_until_event_arrives(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)

        collected: list[TrialEvent] = []
        ready = threading.Event()

        def consumer() -> None:
            ready.set()
            for event in session.events().iter_events(handle):
                collected.append(event)

        thread = threading.Thread(target=consumer)
        thread.start()

        ready.wait(timeout=1.0)
        # Consumer is blocked in iter_events; nothing yet.
        assert collected == []
        session.publish(_turn(session, 0))
        session.publish(_terminal(session))

        thread.join(timeout=2.0)
        assert not thread.is_alive(), "consumer did not exit after terminal"
        assert len(collected) == 2

    def test_detach_releases_blocked_iterator_without_affecting_peers(self):
        session = InProcessTrialSession(trial_id="t:0")
        h_leaving = session.attach("leaving", ParticipantRole.PARTICIPANT)
        h_staying = session.attach("staying", ParticipantRole.OBSERVER)

        leaving_events: list[TrialEvent] = []
        staying_events: list[TrialEvent] = []

        def drain(handle, into):
            for event in session.events().iter_events(handle):
                into.append(event)

        t_leave = threading.Thread(target=drain, args=(h_leaving, leaving_events))
        t_stay = threading.Thread(target=drain, args=(h_staying, staying_events))
        t_leave.start()
        t_stay.start()

        session.publish(_turn(session, 0))
        session.detach(h_leaving)
        t_leave.join(timeout=2.0)
        assert not t_leave.is_alive()
        assert len(leaving_events) == 1  # saw the turn before detach

        session.publish(_assistant(session, "after leave"))
        session.publish(_terminal(session))
        t_stay.join(timeout=2.0)
        assert not t_stay.is_alive()
        assert [e.seq for e in staying_events] == [0, 1, 2]


class TestTerminalAndClose:
    def test_terminal_reached_closes_session(self):
        session = InProcessTrialSession(trial_id="t:0")
        session.attach("p1", ParticipantRole.PARTICIPANT)
        session.publish(_terminal(session))

        assert session.is_closed
        with pytest.raises(RuntimeError):
            session.publish(_turn(session, 99))

    def test_close_without_terminal_still_releases_iterators(self):
        session = InProcessTrialSession(trial_id="t:0")
        h = session.attach("p1", ParticipantRole.PARTICIPANT)
        collected: list[TrialEvent] = []

        def consumer() -> None:
            for event in session.events().iter_events(h):
                collected.append(event)

        thread = threading.Thread(target=consumer)
        thread.start()
        session.publish(_turn(session, 0))
        session.close()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert [e.seq for e in collected] == [0]

    def test_close_is_idempotent(self):
        session = InProcessTrialSession(trial_id="t:0")
        session.close()
        session.close()
        assert session.is_closed


class TestInterventions:
    def test_submit_queues_and_returns_ack_queued(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        intervention = InjectMessage(
            trial_id="t:0",
            attach_to_seq=0,
            participant_id="p1",
            timestamp=_NOW,
            content="try /v2/auth",
        )
        ack = session.interventions().submit(handle, intervention)
        assert ack.outcome == "queued"

        pending = session.drain_pending_interventions()
        assert len(pending) == 1
        assert isinstance(pending[0], QueuedIntervention)
        assert pending[0].intervention.content == "try /v2/auth"
        assert pending[0].handle.participant_id == "p1"

    def test_drain_returns_interventions_in_submission_order(self):
        session = InProcessTrialSession(trial_id="t:0")
        h1 = session.attach("p1", ParticipantRole.PARTICIPANT)
        h2 = session.attach("p2", ParticipantRole.ADMIN)

        for i, (handle, pid) in enumerate([(h1, "p1"), (h2, "p2"), (h1, "p1")]):
            session.interventions().submit(
                handle,
                InjectMessage(
                    trial_id="t:0",
                    attach_to_seq=i,
                    participant_id=pid,
                    timestamp=_NOW,
                    content=f"msg-{i}",
                ),
            )

        drained = session.drain_pending_interventions()
        assert [q.intervention.content for q in drained] == ["msg-0", "msg-1", "msg-2"]

    def test_drain_max_batch_caps(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        for i in range(5):
            session.interventions().submit(
                handle,
                InjectMessage(
                    trial_id="t:0",
                    attach_to_seq=i,
                    participant_id="p1",
                    timestamp=_NOW,
                    content=f"m{i}",
                ),
            )
        first_batch = session.drain_pending_interventions(max_batch=2)
        rest = session.drain_pending_interventions()
        assert len(first_batch) == 2
        assert len(rest) == 3

    def test_submit_cross_trial_intervention_raises(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        with pytest.raises(ValueError):
            session.interventions().submit(
                handle,
                InjectMessage(
                    trial_id="other:0",
                    attach_to_seq=0,
                    participant_id="p1",
                    timestamp=_NOW,
                    content="wrong trial",
                ),
            )

    def test_submit_wrong_participant_id_raises(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        with pytest.raises(ValueError):
            session.interventions().submit(
                handle,
                InjectMessage(
                    trial_id="t:0",
                    attach_to_seq=0,
                    participant_id="not-p1",
                    timestamp=_NOW,
                    content="wrong pid",
                ),
            )

    def test_submit_after_detach_raises(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.PARTICIPANT)
        session.detach(handle)
        with pytest.raises(ValueError):
            session.interventions().submit(
                handle,
                InjectMessage(
                    trial_id="t:0",
                    attach_to_seq=0,
                    participant_id="p1",
                    timestamp=_NOW,
                    content="too late",
                ),
            )


class TestConcurrentProducerConsumer:
    def test_high_frequency_publish_across_multiple_consumers(self):
        session = InProcessTrialSession(trial_id="t:0")
        num_consumers = 4
        handles = [session.attach(f"p{i}", ParticipantRole.OBSERVER) for i in range(num_consumers)]

        collected: dict[str, list[int]] = {h.participant_id: [] for h in handles}

        def consume(handle) -> None:
            for event in session.events().iter_events(handle):
                collected[handle.participant_id].append(event.seq)

        threads = [threading.Thread(target=consume, args=(h,)) for h in handles]
        for t in threads:
            t.start()

        num_events = 200
        for i in range(num_events):
            session.publish(
                ToolCallEmitted(
                    trial_id="t:0",
                    seq=session.next_seq(),
                    timestamp=_NOW,
                    call_id=f"c{i}",
                    tool_name="lookup",
                    arguments_preview="{}",
                )
            )
        session.publish(_terminal(session))

        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive()

        expected = list(range(num_events + 1))  # +1 for TerminalReached
        for pid, seqs in collected.items():
            assert seqs == expected, f"consumer {pid} saw {seqs[:8]}... expected {expected[:8]}..."
