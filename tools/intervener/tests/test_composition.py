"""Tests for the compositional machinery — SessionBinding, sinks, controllers,
ComposedParticipant. Independent of the existing Participant-subclass tests."""

from __future__ import annotations

import io
import json
import threading
import time
from datetime import UTC, datetime

from intervener import (
    ComposedParticipant,
    CompoundSink,
    EventReactiveController,
    JsonlSink,
    PlainLineSink,
    ScriptedController,
    SessionBinding,
    SilentSink,
    TimerController,
)
from intervener.protocols import EventSink, InputController

from tolokaforge.session import (
    AssistantMessage,
    InjectMessage,
    ParticipantRole,
    RecordedTrialSession,
    TerminalReached,
    TrialEvent,
    TurnStarted,
)
from tolokaforge.session._status import TrialStatus

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
_TRIAL = "TEST-COMPOSE:0"


def _tiny_events() -> list[TrialEvent]:
    return [
        TurnStarted(trial_id=_TRIAL, seq=0, timestamp=_NOW, turn_index=0),
        AssistantMessage(trial_id=_TRIAL, seq=1, timestamp=_NOW, content_preview="hello"),
        AssistantMessage(trial_id=_TRIAL, seq=2, timestamp=_NOW, content_preview="world"),
        TerminalReached(trial_id=_TRIAL, seq=3, timestamp=_NOW, status=TrialStatus.COMPLETED),
    ]


def _tiny_session() -> RecordedTrialSession:
    return RecordedTrialSession.from_events(trial_id=_TRIAL, events=_tiny_events())


class TestSessionBinding:
    def test_attach_on_init_and_idempotent_detach(self) -> None:
        session = _tiny_session()
        b = SessionBinding(session, "test-p", ParticipantRole.OBSERVER)
        assert b.trial_id == _TRIAL
        assert b.participant_id == "test-p"
        assert b.role == ParticipantRole.OBSERVER
        b.detach()
        b.detach()  # idempotent — must not raise

    def test_submit_returns_ack(self) -> None:
        session = _tiny_session()
        b = SessionBinding(session, "test-p", ParticipantRole.PARTICIPANT)
        try:
            ack = b.submit(
                InjectMessage(
                    trial_id=_TRIAL,
                    attach_to_seq=0,
                    participant_id=b.participant_id,
                    timestamp=_NOW,
                    content="hi",
                )
            )
            assert ack.outcome in ("accepted", "queued", "superseded", "rejected")
        finally:
            b.detach()


class TestProtocolRuntimeCheckable:
    def test_sinks_are_recognised(self) -> None:
        assert isinstance(SilentSink(), EventSink)
        assert isinstance(PlainLineSink(stream=io.StringIO()), EventSink)
        assert isinstance(JsonlSink(io.StringIO()), EventSink)
        assert isinstance(CompoundSink([SilentSink()]), EventSink)

    def test_controllers_are_recognised(self) -> None:
        assert isinstance(
            ScriptedController(lines=[], line_parser=lambda _line, _event, _binding: None),
            InputController,
        )
        assert isinstance(
            EventReactiveController(callback=lambda e, b: None),
            InputController,
        )
        assert isinstance(
            TimerController(interval_seconds=1.0, callback=lambda t, b: None),
            InputController,
        )


class TestSinks:
    def test_silent_sink_swallows(self) -> None:
        sink = SilentSink()
        for event in _tiny_events():  # type: ignore[attr-defined]
            sink.on_event(event)
        sink.on_terminal()

    def test_plain_line_sink_writes_one_line_per_event(self) -> None:
        buf = io.StringIO()
        sink = PlainLineSink(stream=buf, color=False)
        events = _tiny_events()
        for event in events:
            sink.on_event(event)
        lines = buf.getvalue().strip().splitlines()
        assert len(lines) == len(events)
        assert any("hello" in line for line in lines)

    def test_jsonl_sink_writes_valid_json_per_event(self) -> None:
        buf = io.StringIO()
        sink = JsonlSink(buf)
        events = _tiny_events()
        for event in events:
            sink.on_event(event)
        records = [json.loads(line) for line in buf.getvalue().splitlines()]
        assert len(records) == len(events)
        assert records[0]["kind"] == "turn_started"
        assert records[1]["kind"] == "assistant_message"
        assert records[3]["kind"] == "terminal_reached"

    def test_compound_sink_fans_out_and_isolates_failures(self) -> None:
        good = SilentSink()
        seen: list[TrialEvent] = []

        class Recorder:
            def on_event(self, event: TrialEvent) -> None:
                seen.append(event)

            def on_terminal(self) -> None:
                pass

        class Broken:
            def on_event(self, event: TrialEvent) -> None:
                raise RuntimeError("boom")

            def on_terminal(self) -> None:
                raise RuntimeError("also boom")

        c = CompoundSink([good, Broken(), Recorder()])
        for event in _tiny_events():  # type: ignore[attr-defined]
            c.on_event(event)
        c.on_terminal()
        assert len(seen) == 4  # Recorder still received everything


class TestScriptedController:
    def test_lines_mode_consumes_one_per_seam(self) -> None:
        session = _tiny_session()
        submissions: list[str] = []

        def parser(line: str, event: TrialEvent, binding: SessionBinding):
            submissions.append(line)
            return InjectMessage(
                trial_id=binding.trial_id,
                attach_to_seq=event.seq,
                participant_id=binding.participant_id,
                timestamp=_NOW,
                content=line,
            )

        ctrl = ScriptedController(
            lines=["first", "second"],
            line_parser=parser,
            seam_predicate=lambda e: isinstance(e, AssistantMessage),
        )
        participant = ComposedParticipant(
            "test-scripted", role=ParticipantRole.PARTICIPANT, controllers=[ctrl]
        )
        participant.run(session)
        assert submissions == ["first", "second"]

    def test_timed_mode_submits_and_stops_on_terminal(self) -> None:
        session = _tiny_session()
        inj = InjectMessage(
            trial_id=_TRIAL,
            attach_to_seq=0,
            participant_id="p",
            timestamp=_NOW,
            content="timed",
        )
        ctrl = ScriptedController(timed_script=[(0.01, inj), (0.01, inj)])
        p = ComposedParticipant("test-timed", controllers=[ctrl])
        p.run(session)  # returns quickly on terminal event


class TestEventReactiveController:
    def test_callback_returning_none_does_not_submit(self) -> None:
        called: list[TrialEvent] = []

        def cb(event: TrialEvent, binding: SessionBinding):
            called.append(event)
            return None

        session = _tiny_session()
        p = ComposedParticipant(
            "test-reactive",
            controllers=[EventReactiveController(cb)],
        )
        p.run(session)
        assert len(called) == 4  # all events observed


class TestComposedParticipantLifecycle:
    def test_run_returns_log_with_one_entry_per_event(self) -> None:
        session = _tiny_session()
        p = ComposedParticipant("test-compose")
        log = p.run(session)
        assert len(log.entries) == 4
        assert log.entries[0].event_kind == "turn_started"
        assert log.entries[-1].event_kind == "terminal_reached"
        assert all(e.participant_id == "test-compose" for e in log.entries)

    def test_controller_start_and_stop_are_called(self) -> None:
        starts: list[bool] = []
        stops: list[bool] = []

        class Marker:
            def start(self, binding: SessionBinding, terminal: threading.Event) -> None:
                starts.append(True)

            def stop(self) -> None:
                stops.append(True)

        p = ComposedParticipant("test-lifecycle", controllers=[Marker()])
        p.run(_tiny_session())
        assert starts == [True]
        assert stops == [True]

    def test_sinks_receive_every_event(self) -> None:
        buf = io.StringIO()
        p = ComposedParticipant(
            "test-sinks",
            sinks=[PlainLineSink(stream=buf, color=False)],
        )
        p.run(_tiny_session())
        assert len(buf.getvalue().strip().splitlines()) == 4


class TestTimerController:
    def test_stops_when_terminal_event_fires(self) -> None:
        terminal = threading.Event()
        ticks: list[int] = []

        def cb(tick: int, binding: SessionBinding):
            ticks.append(tick)
            return None

        ctrl = TimerController(interval_seconds=0.05, callback=cb)
        # Simulate a lightweight lifecycle without going through ComposedParticipant
        session = _tiny_session()
        b = SessionBinding(session, "timer-test", ParticipantRole.OBSERVER)
        try:
            ctrl.start(b, terminal)
            time.sleep(0.16)
            terminal.set()
            time.sleep(0.1)
        finally:
            ctrl.stop()
            b.detach()
        assert len(ticks) >= 2  # ~3 ticks in 160ms with 50ms interval
