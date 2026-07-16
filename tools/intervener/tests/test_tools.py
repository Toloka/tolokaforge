"""Tests for the interactive-tools plug-in surface."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from intervener import (
    AnalyzeTool,
    ComposedParticipant,
    ContextTool,
    InteractiveTool,
    RollingEventsSink,
    ToolContext,
    ToolRegistry,
    ToolResult,
)

from tolokaforge.session import (
    AssistantMessage,
    ParticipantRole,
    RecordedTrialSession,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TurnStarted,
)
from tolokaforge.session._status import TrialStatus

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
_TRIAL = "TEST-TOOLS:0"


def _sample_events() -> list[TrialEvent]:
    return [
        TurnStarted(trial_id=_TRIAL, seq=0, timestamp=_NOW, turn_index=0),
        AssistantMessage(trial_id=_TRIAL, seq=1, timestamp=_NOW, content_preview="thinking"),
        ToolCallEmitted(
            trial_id=_TRIAL,
            seq=2,
            timestamp=_NOW,
            call_id="c1",
            tool_name="db_query",
            arguments_preview="{}",
        ),
        ToolResultObserved(
            trial_id=_TRIAL,
            seq=3,
            timestamp=_NOW,
            call_id="c1",
            tool_name="db_query",
            duration_ms=10,
            truncated_preview="[]",
        ),
        TurnStarted(trial_id=_TRIAL, seq=4, timestamp=_NOW, turn_index=1),
        AssistantMessage(
            trial_id=_TRIAL, seq=5, timestamp=_NOW, content_preview="retrying with different args"
        ),
        ToolCallEmitted(
            trial_id=_TRIAL,
            seq=6,
            timestamp=_NOW,
            call_id="c2",
            tool_name="db_query",
            arguments_preview="{}",
        ),
        ToolResultObserved(
            trial_id=_TRIAL,
            seq=7,
            timestamp=_NOW,
            call_id="c2",
            tool_name="db_query",
            duration_ms=10,
            truncated_preview="[]",
        ),
        TurnStarted(trial_id=_TRIAL, seq=8, timestamp=_NOW, turn_index=2),
        AssistantMessage(trial_id=_TRIAL, seq=9, timestamp=_NOW, content_preview="third attempt"),
        ToolCallEmitted(
            trial_id=_TRIAL,
            seq=10,
            timestamp=_NOW,
            call_id="c3",
            tool_name="db_query",
            arguments_preview="{}",
        ),
        TerminalReached(trial_id=_TRIAL, seq=11, timestamp=_NOW, status=TrialStatus.FAILED),
    ]


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        r = ToolRegistry([ContextTool()])
        assert "context" in r
        assert r.get("context") is not None
        assert r.get("nonexistent") is None
        assert len(r) == 1

    def test_duplicate_name_raises(self) -> None:
        r = ToolRegistry([ContextTool()])
        with pytest.raises(ValueError, match="already registered"):
            r.register(ContextTool())

    def test_list_summary_preserves_order(self) -> None:
        r = ToolRegistry([ContextTool(), AnalyzeTool()])
        names = [name for name, _ in r.list_summary()]
        assert names == ["context", "analyze"]

    def test_with_discovered_finds_reference_tools(self) -> None:
        r = ToolRegistry.with_discovered()
        names = {name for name, _ in r.list_summary()}
        # entry_points in pyproject.toml register both reference tools
        assert "context" in names
        assert "analyze" in names

    def test_with_discovered_accepts_extras(self) -> None:
        class LocalTool(InteractiveTool):
            name = "local"
            description = "for testing"

            def run(self, args, ctx):
                return ToolResult(output="local")

        r = ToolRegistry.with_discovered(LocalTool())
        assert "local" in r
        assert "context" in r


class TestContextTool:
    def test_with_metadata(self) -> None:
        tool = ContextTool()
        ctx = ToolContext(
            recent_events=_sample_events(),
            task_metadata={
                "task_id": "test_ticket",
                "name": "Ticket Update",
                "description": "Update ticket status via the DB tool.",
            },
        )
        result = tool.run("", ctx)
        assert "Ticket Update" in result.output
        assert "Update ticket status" in result.output
        assert result.data is not None
        assert result.data["task"] == "Ticket Update"
        assert result.data["counters"]["turns"] == 3
        assert result.data["counters"]["tool_calls"] == 3

    def test_without_metadata_degrades_cleanly(self) -> None:
        tool = ContextTool()
        ctx = ToolContext(recent_events=_sample_events())
        result = tool.run("", ctx)
        assert "no" in result.output.lower()  # says "none supplied"
        assert result.data is not None
        assert result.data["counters"]["turns"] == 3

    def test_empty_events(self) -> None:
        tool = ContextTool()
        result = tool.run("", ToolContext())
        assert "0 turns" in result.output


class TestAnalyzeTool:
    def test_heuristic_when_llm_call_absent(self) -> None:
        tool = AnalyzeTool()
        ctx = ToolContext(recent_events=_sample_events(), llm_call=None)
        result = tool.run("3", ctx)
        assert result.data is not None
        assert result.data["source"] == "heuristic"
        # Three identical db_query calls in a row → loop detection message
        assert "loop" in result.output.lower() or "repeatedly" in result.output.lower()

    def test_llm_path_used_when_call_supplied(self) -> None:
        calls: list[tuple[str, str]] = []

        def stub(system: str, user: str) -> str:
            calls.append((system, user))
            return "LLM says: agent is stuck in db_query loop; suggest read_file."

        tool = AnalyzeTool()
        ctx = ToolContext(recent_events=_sample_events(), llm_call=stub)
        result = tool.run("3", ctx)
        assert result.data is not None
        assert result.data["source"] == "llm"
        assert "LLM says" in result.output
        assert len(calls) == 1
        system, user = calls[0]
        assert "summarise" in system.lower()
        assert "turn" in user.lower()  # transcript formatting

    def test_llm_call_failure_falls_back_to_heuristic(self) -> None:
        def stub_that_raises(system: str, user: str) -> str:
            raise RuntimeError("network down")

        tool = AnalyzeTool()
        ctx = ToolContext(recent_events=_sample_events(), llm_call=stub_that_raises)
        result = tool.run("", ctx)
        assert result.data is not None
        assert result.data["source"] == "heuristic"
        assert "network down" in result.output

    def test_llm_empty_response_falls_back_to_heuristic(self) -> None:
        tool = AnalyzeTool()
        ctx = ToolContext(recent_events=_sample_events(), llm_call=lambda s, u: "   ")
        result = tool.run("", ctx)
        assert result.data is not None
        assert result.data["source"] == "heuristic"

    def test_parses_args_default(self) -> None:
        tool = AnalyzeTool()
        ctx = ToolContext(recent_events=_sample_events())  # no llm_call
        result = tool.run("", ctx)
        assert result.data is not None
        assert result.data["turns_analyzed"] == 3

    def test_parses_args_bogus_falls_back_to_default(self) -> None:
        tool = AnalyzeTool()
        ctx = ToolContext(recent_events=_sample_events())
        result = tool.run("not-a-number", ctx)
        assert result.data is not None

    def test_empty_events_returns_useful_message(self) -> None:
        tool = AnalyzeTool()
        result = tool.run("", ToolContext(recent_events=[]))
        assert "no events" in result.output.lower()


class TestRollingEventsSink:
    def test_bounded_capacity(self) -> None:
        sink = RollingEventsSink(maxlen=3)
        events = _sample_events()
        for e in events:
            sink.on_event(e)
        assert len(sink) == 3
        # last 3 events preserved
        assert [e.seq for e in sink.events] == [events[-3].seq, events[-2].seq, events[-1].seq]

    def test_events_snapshot_is_a_copy(self) -> None:
        sink = RollingEventsSink()
        sink.on_event(_sample_events()[0])
        snapshot = sink.events
        sink.on_event(_sample_events()[1])
        # Snapshot from before does not grow
        assert len(snapshot) == 1
        assert len(sink.events) == 2

    def test_zero_maxlen_rejected(self) -> None:
        with pytest.raises(ValueError):
            RollingEventsSink(maxlen=0)


class TestKeyboardControllerToolIntegration:
    """The keyboard controller's REPL is TTY-driven so we can't unit-test the
    prompt loop cleanly. We can, however, verify the wiring: construction
    accepts a registry, rejects colliding names, and passes tools through."""

    def test_accepts_tools_and_metadata_kwargs(self) -> None:
        from intervener import KeyboardController

        r = ToolRegistry([ContextTool()])
        # Should not raise
        KeyboardController(tools=r, task_metadata={"name": "demo"})

    def test_rejects_tool_name_colliding_with_builtin(self) -> None:
        from intervener import KeyboardController

        class QuitTool(InteractiveTool):
            name = "quit"
            description = "shadows builtin"

            def run(self, args, ctx):
                return ToolResult(output="")

        r = ToolRegistry([QuitTool()])
        with pytest.raises(ValueError, match="built-in REPL command"):
            KeyboardController(tools=r)


class TestConsumerAgnosticism:
    """A tool should work identically whether called from a session-backed
    consumer or a plain script with no session."""

    def test_context_tool_no_session(self) -> None:
        # Plain-script consumer: only recent_events populated
        result = ContextTool().run("", ToolContext(recent_events=_sample_events()))
        assert "3 turns" in result.output

    def test_context_tool_with_session(self) -> None:
        # Live-session consumer: full context
        session = RecordedTrialSession.from_events(trial_id=_TRIAL, events=_sample_events())
        p = ComposedParticipant("test", role=ParticipantRole.OBSERVER, sinks=[RollingEventsSink()])
        log = p.run(session)
        # After run, the rolling sink has the events; verify via a fresh
        # ToolContext built from it
        rolling = p.sinks[0]
        assert isinstance(rolling, RollingEventsSink)
        result = ContextTool().run("", ToolContext(recent_events=rolling.events))
        assert "3 turns" in result.output
        assert log is not None
