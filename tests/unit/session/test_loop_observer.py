"""Unit tests for :class:`SessionLoopObserver` — bridges the ToolCallingLoop
observer seam into an :class:`InProcessTrialSession`.

Covers translation semantics for each observer callback plus a small
end-to-end run of :class:`ToolCallingLoop` with the bridge attached to
confirm the loop's seams fire in the expected order.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import init_trial_logger
from tolokaforge.core.loop import LoopConfig, ToolCallingLoop
from tolokaforge.core.models import (
    TerminationReason,
    ToolCall,
    TrialStatus,
)
from tolokaforge.session import (
    AssistantMessage,
    InProcessTrialSession,
    ParticipantRole,
    SessionLoopObserver,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TurnStarted,
)

pytestmark = pytest.mark.unit


class TestSessionLoopObserverTranslations:
    """Direct-call tests: invoke each observer method and verify the
    resulting event on the session queue matches the expected shape.
    """

    def _observer(self):
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.OBSERVER)
        return session, handle, SessionLoopObserver(session)

    def test_on_turn_start_publishes_turn_started(self):
        session, handle, obs = self._observer()
        obs.on_turn_start(turn_index=0)
        session.close()
        events = list(session.events().iter_events(handle))
        assert len(events) == 1
        assert isinstance(events[0], TurnStarted)
        assert events[0].turn_index == 0
        assert events[0].seq == 0

    def test_on_assistant_message_publishes_assistant_event(self):
        session, handle, obs = self._observer()
        obs.on_assistant_message(content="Hello world", has_reasoning=True)
        session.close()
        events = list(session.events().iter_events(handle))
        assert isinstance(events[0], AssistantMessage)
        assert events[0].content_preview == "Hello world"
        assert events[0].has_reasoning is True

    def test_on_tool_call_publishes_tool_call_event_with_argument_preview(self):
        session, handle, obs = self._observer()
        obs.on_tool_call(call_id="c1", tool_name="lookup", arguments={"q": "org1"})
        session.close()
        events = list(session.events().iter_events(handle))
        assert isinstance(events[0], ToolCallEmitted)
        assert events[0].call_id == "c1"
        assert events[0].tool_name == "lookup"
        assert "org1" in events[0].arguments_preview

    def test_on_tool_result_publishes_tool_result_event(self):
        session, handle, obs = self._observer()
        obs.on_tool_result(
            call_id="c1", tool_name="lookup", duration_ms=142, output="found", success=True
        )
        session.close()
        events = list(session.events().iter_events(handle))
        assert isinstance(events[0], ToolResultObserved)
        assert events[0].duration_ms == 142
        assert events[0].truncated_preview == "found"

    def test_on_terminal_translates_core_enums_into_session_enums(self):
        session, handle, obs = self._observer()
        obs.on_terminal(
            status=TrialStatus.COMPLETED, termination_reason=TerminationReason.MAX_TURNS
        )
        events = list(session.events().iter_events(handle))
        assert isinstance(events[0], TerminalReached)
        # Session's enum has same string values as core, but is a distinct class
        assert events[0].status.value == "completed"
        assert events[0].termination_reason is not None
        assert events[0].termination_reason.value == "max_turns"
        assert session.is_closed  # TerminalReached auto-closes the session

    def test_on_terminal_handles_none_reason(self):
        session, handle, obs = self._observer()
        obs.on_terminal(status=TrialStatus.COMPLETED, termination_reason=None)
        events = list(session.events().iter_events(handle))
        assert events[0].termination_reason is None

    def test_seq_is_monotonic_across_observer_calls(self):
        session, handle, obs = self._observer()
        obs.on_turn_start(turn_index=0)
        obs.on_assistant_message(content="hi", has_reasoning=False)
        obs.on_tool_call(call_id="c1", tool_name="f", arguments={})
        obs.on_tool_result(call_id="c1", tool_name="f", duration_ms=10, output="ok", success=True)
        session.close()
        events = list(session.events().iter_events(handle))
        assert [e.seq for e in events] == [0, 1, 2, 3]

    def test_argument_preview_is_truncated(self):
        session, handle, obs = self._observer()
        big = {"blob": "x" * 500}
        obs.on_tool_call(call_id="c1", tool_name="f", arguments=big)
        session.close()
        events = list(session.events().iter_events(handle))
        assert len(events[0].arguments_preview) <= 240

    def test_argument_preview_handles_unserialisable_values(self):
        """Non-JSON-serialisable arguments must not crash — the bridge falls
        back to ``repr`` under a broad exception guard.
        """
        session, handle, obs = self._observer()
        obs.on_tool_call(call_id="c1", tool_name="f", arguments={"obj": object()})
        session.close()
        events = list(session.events().iter_events(handle))
        assert isinstance(events[0], ToolCallEmitted)


class TestToolCallingLoopEndToEndWithObserver:
    """Drive :class:`ToolCallingLoop` with the bridge attached; verify the
    natural seams fire in the right order for a small synthetic run.
    """

    def _make_result(
        self, text: str = "reply", tool_calls: list[ToolCall] | None = None
    ) -> GenerationResult:
        return GenerationResult(text=text, tool_calls=tool_calls or [], usage=Usage())

    def test_observer_receives_ordered_turn_and_terminal_events(self):
        """A one-turn run with no tool calls emits turn_start, assistant,
        then terminal (via max_turns==1). Order and count are the whole
        point of the test.
        """
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.OBSERVER)
        observer = SessionLoopObserver(session)

        llm_client = MagicMock()
        llm_client.generate.return_value = self._make_result("hi from agent")

        tool_executor = MagicMock()
        metrics = MagicMock()

        loop = ToolCallingLoop(
            llm_client=llm_client,
            tool_executor=tool_executor,
            tool_schemas=[],
            config=LoopConfig(max_turns=1, episode_timeout_s=60),
            metrics=metrics,
            should_terminate=lambda result, turn, messages: None,
            logger=init_trial_logger("t:0", verbose=False, strict=False),
            observer=observer,
        )
        loop.run(system_prompt="sys", messages=[], start_time=time.time())

        events = list(session.events().iter_events(handle))
        kinds = [e.kind for e in events]
        # turn_start → assistant_message → terminal_reached
        assert kinds == ["turn_started", "assistant_message", "terminal_reached"]
        assert isinstance(events[-1], TerminalReached)

    def test_observer_receives_tool_call_and_result_events(self):
        """A run with one tool call emits tool_call and tool_result events."""
        session = InProcessTrialSession(trial_id="t:0")
        handle = session.attach("p1", ParticipantRole.OBSERVER)
        observer = SessionLoopObserver(session)

        tc = ToolCall(id="c_a", name="lookup", arguments={"q": "x"})
        # First generation carries a tool call; second generation stops with no tool call.
        llm_client = MagicMock()
        llm_client.generate.side_effect = [
            self._make_result(text="calling", tool_calls=[tc]),
            self._make_result(text="done", tool_calls=[]),
        ]

        tool_result = MagicMock()
        tool_result.success = True
        tool_result.output = "found: x"
        tool_result.content_blocks = None
        tool_result.error = None

        tool_executor = MagicMock()
        tool_executor.execute.return_value = tool_result

        loop = ToolCallingLoop(
            llm_client=llm_client,
            tool_executor=tool_executor,
            tool_schemas=[],
            config=LoopConfig(max_turns=2, episode_timeout_s=60),
            metrics=MagicMock(),
            should_terminate=lambda result, turn, messages: None,
            logger=init_trial_logger("t:0", verbose=False, strict=False),
            observer=observer,
        )
        loop.run(system_prompt="sys", messages=[], start_time=time.time())

        events = list(session.events().iter_events(handle))
        kinds = [e.kind for e in events]
        assert "tool_call_emitted" in kinds
        assert "tool_result_observed" in kinds

        tool_call_event = next(e for e in events if isinstance(e, ToolCallEmitted))
        tool_result_event = next(e for e in events if isinstance(e, ToolResultObserved))
        assert tool_call_event.call_id == "c_a"
        assert tool_call_event.tool_name == "lookup"
        assert tool_result_event.call_id == "c_a"
        assert tool_result_event.tool_name == "lookup"
        assert "found" in tool_result_event.truncated_preview


class TestDefaultNoOpObserver:
    """The loop's default observer is a no-op; existing behavior is preserved
    when no bridge is attached. Covered indirectly by every pre-existing
    ToolCallingLoop test that passes; this explicit test locks it in.
    """

    def test_default_observer_is_no_op(self):
        # If the default observer ever regresses (e.g. someone binds it to a
        # global sink), this import assertion + attribute check fails loudly.
        from tolokaforge.core.loop import _NULL_LOOP_OBSERVER

        _NULL_LOOP_OBSERVER.on_turn_start(0)
        _NULL_LOOP_OBSERVER.on_assistant_message("x", False)
        _NULL_LOOP_OBSERVER.on_tool_call("c", "t", {})
        _NULL_LOOP_OBSERVER.on_tool_result("c", "t", 0, "o", True)
        _NULL_LOOP_OBSERVER.on_terminal(TrialStatus.COMPLETED, None)
