"""The reference loop's tool calls travel the funnel, not a copy of it.

``InMemoryAgentLoop`` is what ``docs/RUNTIME_BACKENDS.md`` calls the worked
example to copy, so anything it writes by hand is written by hand in every loop
copied from it. Four of the funnel's obligations are optional to *read* off the
context and silent when dropped — the output cap, the observer notification, the
argument recovery and the per-tool validation schema — which is exactly the set
a hand-rolled execute path loses without failing anything.

Each is asserted through a context that supplies the seam and an episode that
must reach it. The defect paths keep their own coverage in
``tests/canonical/test_agent_loop_contract.py``: a defect switches an obligation
off on purpose, so it leaves the funnel and the hand-rolled path is what runs.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import AgentLoopContext, LoopConfig, MetricsSink, classify_loop_error
from tolokaforge.core.models import Message, MessageRole, ToolCall
from tolokaforge.core.runner import TrialToolCallRecorder
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.testing.agent_loops import InMemoryAgentLoop, LoopDefects
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.unit

_LONG_OUTPUT = "x" * 500


class _Executor:
    """Records what each call was handed, including the schema it was validated against."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        call_id: str,
        validation_schema: dict[str, Any] | None = None,
    ) -> ToolResult:
        self.calls.append(
            {
                "tool_name": tool_name,
                "arguments": dict(arguments or {}),
                "call_id": call_id,
                "validation_schema": validation_schema,
            }
        )
        return ToolResult(success=True, output=_LONG_OUTPUT)


class _Sink(MetricsSink):
    def __init__(self) -> None:
        self.generations = 0
        self.tool_calls = 0
        self.truncations: list[int] = []

    def record_generation(self, result: GenerationResult) -> None:
        self.generations += 1

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_tool_output_truncated(self, omitted_chars: int) -> None:
        self.truncations.append(omitted_chars)


class _Observer:
    """A :class:`~tolokaforge.observability.observer.LoopObserver` that counts."""

    def __init__(self) -> None:
        self.tool_calls: list[tuple[int, str]] = []

    def generation(self, **kwargs: Any) -> None:
        return None

    def tool_call(self, *, index: int, call: ToolCall, result: Any, **kwargs: Any) -> None:
        self.tool_calls.append((index, call.name))


class _OneToolCallThenDone:
    """One turn calling a tool, then a tool-call-free turn that ends the episode."""

    def __init__(self) -> None:
        self._turns = [
            GenerationResult(
                text="calling it",
                tool_calls=[ToolCall(id="c1", name="lookup", arguments={})],
                usage=Usage(prompt_tokens=1),
            ),
            GenerationResult(text="done", usage=Usage(prompt_tokens=1)),
        ]

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        return self._turns.pop(0) if self._turns else GenerationResult(text="done")


def _context(
    executor: _Executor,
    *,
    sink: _Sink,
    observer: _Observer | None = None,
    normalize: Any | None = None,
    validation_schemas: dict[str, dict[str, Any]] | None = None,
    tool_output_max_chars: int | None = None,
) -> AgentLoopContext:
    return AgentLoopContext(
        llm_client=_OneToolCallThenDone(),
        tool_executor=executor,
        tool_schemas=[],
        config=LoopConfig(
            max_turns=4, episode_timeout_s=600, tool_output_max_chars=tool_output_max_chars
        ),
        metrics=sink,
        should_terminate=lambda result, turn, messages: None,
        logger=get_logger("in-memory-reference-loop-test", strict=False),
        classify_error=lambda exc: classify_loop_error(exc, ()),
        call_ids=EpisodeUniqueCallIds(),
        recorder=TrialToolCallRecorder(),
        observer=observer,
        normalize_tool_arguments=normalize,
        validation_schemas_by_tool=validation_schemas,
    )


def _run(context: AgentLoopContext, defects: LoopDefects | None = None) -> list[Message]:
    loop = InMemoryAgentLoop(context=context, defects=defects or LoopDefects())
    messages: list[Message] = [Message(role=MessageRole.USER, content="go")]
    loop.run("system", messages, time.time())
    return messages


def _tool_messages(messages: list[Message]) -> list[Message]:
    return [message for message in messages if message.role is MessageRole.TOOL]


def test_the_tool_output_cap_reaches_the_appended_message() -> None:
    """``config.tool_output_max_chars`` bounds what the loop appends.

    A loop that ignores it lets every turn's context grow by the whole tool
    output, which is the accumulation the cap exists to bound.
    """
    executor, sink = _Executor(), _Sink()
    messages = _run(_context(executor, sink=sink, tool_output_max_chars=80))

    content = _tool_messages(messages)[0].content
    assert len(content) < len(
        _LONG_OUTPUT
    ), f"the tool message came through at {len(content)} chars, uncapped"
    assert sink.truncations == [len(_LONG_OUTPUT) - 80], (
        "the clipped characters must reach the metrics sink, so a trial can say "
        "how much of its tool output the model never saw"
    )


def test_the_observer_is_told_about_every_tool_call() -> None:
    """The live-tracing seam, and the message index the span shares with it."""
    executor, sink, observer = _Executor(), _Sink(), _Observer()
    messages = _run(_context(executor, sink=sink, observer=observer))

    assert [name for _, name in observer.tool_calls] == ["lookup"]
    index = observer.tool_calls[0][0]
    assert messages[index] is _tool_messages(messages)[0]


def test_malformed_arguments_are_recovered_before_the_tool_runs() -> None:
    """The argument-repair seam runs on the call, not after it."""
    executor, sink = _Executor(), _Sink()
    _run(
        _context(
            executor,
            sink=sink,
            normalize=lambda name, arguments, text: {"q": "recovered"},
        )
    )

    assert executor.calls[0]["arguments"] == {"q": "recovered"}


def test_the_tools_own_validation_schema_reaches_the_executor() -> None:
    """The schema the model was shown is the one the call is validated against."""
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    executor, sink = _Executor(), _Sink()
    _run(_context(executor, sink=sink, validation_schemas={"lookup": schema}))

    assert executor.calls[0]["validation_schema"] == schema


def test_a_defect_that_leaves_the_funnel_loses_exactly_those_four() -> None:
    """The control, and the reason the defect knobs still hand-roll.

    ``plain_tool_error_text`` cannot be expressed through the funnel, so the
    loop writes the path itself — and the four obligations above go with it.
    That is the cost this test names, and the cost an implementer copying a
    hand-rolled path would pay without a defect flag to explain it.
    """
    executor, sink, observer = _Executor(), _Sink(), _Observer()
    messages = _run(
        _context(executor, sink=sink, observer=observer, tool_output_max_chars=80),
        LoopDefects(plain_tool_error_text=True),
    )

    assert len(_tool_messages(messages)[0].content) == len(_LONG_OUTPUT)
    assert observer.tool_calls == []
