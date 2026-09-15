"""The tool-calling loop reports generations and tool results at their recorded message index."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.loop import LoopConfig, MetricsSink, ToolCallingLoop, classify_loop_error
from tolokaforge.core.models import Message, MessageRole, ToolCall
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.unit


class _ScriptedClient:
    def __init__(self, results):
        self._results = list(results)

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        return self._results.pop(0)


class _Executor:
    def execute(self, tool_name, arguments, *, call_id, validation_schema=None):
        return ToolResult(success=True, output=f"ran {tool_name}")


class _Sink(MetricsSink):
    def record_generation(self, result):
        return None

    def record_tool_call(self):
        return None

    def record_parser_errors(self, errors):
        return None


class _Recording:
    def __init__(self, fail: bool = False):
        self.generations: list[dict] = []
        self.tool_calls: list[dict] = []
        self.fail = fail

    def generation(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.generations.append(kwargs)

    def tool_call(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.tool_calls.append(kwargs)


def _terminate_on_text(result, turn, messages):
    return None


def _loop(client, observer):
    from tolokaforge.core.loop import TerminationDecision  # noqa: F401  (documented seam)

    return ToolCallingLoop(
        llm_client=client,
        tool_executor=_Executor(),
        tool_schemas=[],
        config=LoopConfig(max_turns=3, episode_timeout_s=10_000),
        metrics=_Sink(),
        should_terminate=_terminate_on_text,
        classify_error=lambda exc: classify_loop_error(exc, ()),
        logger=StructuredLogger(name="test"),
        retry_sleep=lambda _s: None,
        observer=observer,
    )


def test_indices_match_the_recorded_message_positions() -> None:
    call = ToolCall(id="c1", name="shell", arguments={"cmd": "ls"})
    client = _ScriptedClient(
        [
            GenerationResult(
                text="", tool_calls=[call], usage=Usage(prompt_tokens=1), latency_s=0.5
            ),
            GenerationResult(text="done", usage=Usage(prompt_tokens=1)),
            GenerationResult(text="more", usage=Usage(prompt_tokens=1)),
        ]
    )
    observer = _Recording()
    messages = [Message(role=MessageRole.USER, content="hi", ts=datetime.now(tz=timezone.utc))]
    _loop(client, observer).run("system", messages, time.time())

    # messages: [user, assistant(tool call), tool, assistant, assistant] -> indices 1, 2, 3, 4
    assert [g["index"] for g in observer.generations] == [1, 3, 4]
    assert [t["index"] for t in observer.tool_calls] == [2]
    for g in observer.generations:
        assert messages[g["index"]].role is MessageRole.ASSISTANT
        assert len(g["request"]) == g["index"]  # everything recorded before the turn
        assert g["started_at"] <= g["ended_at"]
    assert messages[observer.tool_calls[0]["index"]].role is MessageRole.TOOL
    assert observer.tool_calls[0]["call"] is call
    assert observer.tool_calls[0]["result"].output == "ran shell"


def test_loop_runs_without_an_observer() -> None:
    client = _ScriptedClient([GenerationResult(text="done", usage=Usage(prompt_tokens=1))] * 3)
    outcome = _loop(client, None).run(
        "system", [Message(role=MessageRole.USER, content="hi")], time.time()
    )
    assert outcome is not None
