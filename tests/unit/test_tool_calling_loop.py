"""Characterization tests for the generic ToolCallingLoop engine.

These pin the engine seams the agent does NOT exercise but a future read-only
judge will rely on:

* running with NO user simulator (``user_turn=None``) — the loop must never
  reference user-simulator concepts, and a no-tool-call turn just re-prompts;
* terminating on a *specific tool call* via the termination callback (how the
  judge will stop when ``submit_report`` is called);
* metrics accumulation through an arbitrary ``MetricsSink``;
* error classification through the shared ``classify_loop_error``.

We use the project's real ``ToolExecutor``-shaped fakes minimally — only a
generate seam and a tool executor are faked, no over-mocking of the loop.
"""

import time

import pytest
from litellm.exceptions import RateLimitError

from tolokaforge.core.llm.client import GenerationResult, LLMApiTimeoutError
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import (
    LoopConfig,
    MetricsSink,
    TerminationDecision,
    ToolCallingLoop,
    classify_loop_error,
)
from tolokaforge.core.models import Message, MessageRole, TerminationReason, ToolCall, TrialStatus
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.unit


class _ScriptedClient:
    """Yields a fixed sequence of GenerationResults, one per generate call.

    An entry that is an ``Exception`` instance is raised on that call instead —
    the retry-loop tests script provider failures this way.
    """

    def __init__(self, results: list[GenerationResult | Exception]) -> None:
        self._results = list(results)
        self.calls = 0

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        self.calls += 1
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _RecordingExecutor:
    """Minimal ToolExecutor-shaped fake."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict, str]] = []

    def execute(self, tool_name, arguments, *, call_id, validation_schema=None):
        self.executed.append((tool_name, arguments, call_id))
        return ToolResult(success=True, output=f"ran {tool_name}")


class _CountingSink(MetricsSink):
    def __init__(self) -> None:
        self.generations = 0
        self.tool_calls = 0
        self.prompt_tokens = 0

    def record_generation(self, result: GenerationResult) -> None:
        self.generations += 1
        self.prompt_tokens += result.usage.prompt_tokens

    def record_tool_call(self) -> None:
        self.tool_calls += 1


def _logger():
    return get_logger("loop-test", strict=False)


def _never_terminate(result, turn, messages):
    return None


def _classify_no_patterns(exc: Exception) -> TerminationDecision:
    return classify_loop_error(exc, ())


def _loop(
    client,
    *,
    should_terminate,
    user_turn=None,
    max_turns=5,
    executor=None,
    sink=None,
    config=None,
    retry_sleep=None,
):
    return ToolCallingLoop(
        llm_client=client,
        tool_executor=executor or _RecordingExecutor(),
        tool_schemas=[],
        config=config or LoopConfig(max_turns=max_turns, episode_timeout_s=10_000),
        metrics=sink or _CountingSink(),
        should_terminate=should_terminate,
        user_turn=user_turn,
        classify_error=_classify_no_patterns,
        logger=_logger(),
        retry_sleep=retry_sleep or (lambda _s: None),
    )


def test_no_user_simulator_no_tool_calls_re_prompts_until_max_turns():
    """Judge-shaped: with no user_turn, a no-tool-call turn advances to the next
    turn (re-prompt) rather than terminating, until max_turns is hit."""
    client = _ScriptedClient(
        [GenerationResult(text=f"thinking {i}", usage=Usage(prompt_tokens=1)) for i in range(3)]
    )
    messages: list[Message] = []
    outcome = _loop(client, should_terminate=_never_terminate, max_turns=3).run(
        "sys", messages, time.time()
    )

    assert client.calls == 3
    assert outcome.termination_reason == TerminationReason.MAX_TURNS
    assert outcome.status == TrialStatus.COMPLETED
    # No USER-role message ever appears without a user simulator.
    assert all(m.role != MessageRole.USER for m in messages)


def test_terminates_on_specific_tool_call():
    """Judge-shaped: terminate when a named tool is called (submit_report)."""

    def stop_on_submit(result, turn, messages):
        if any(tc.name == "submit_report" for tc in result.tool_calls):
            return TerminationDecision(
                reason=TerminationReason.AGENT_DONE,
                system_message="report submitted",
            )
        return None

    client = _ScriptedClient(
        [
            GenerationResult(
                text="look first",
                tool_calls=[ToolCall(id="t1", name="get_state", arguments={})],
                usage=Usage(prompt_tokens=2),
            ),
            GenerationResult(
                text="now report",
                tool_calls=[ToolCall(id="t2", name="submit_report", arguments={"score": 1})],
                usage=Usage(prompt_tokens=2),
            ),
        ]
    )
    executor = _RecordingExecutor()
    messages: list[Message] = []
    outcome = _loop(client, should_terminate=stop_on_submit, executor=executor, max_turns=10).run(
        "sys", messages, time.time()
    )

    assert outcome.termination_reason == TerminationReason.AGENT_DONE
    # submit_report terminates BEFORE its own tool execution; only get_state ran.
    assert executor.executed == [("get_state", {}, "t1")]
    assert messages[-1].role == MessageRole.SYSTEM
    assert messages[-1].content == "report submitted"


def test_tool_calls_executed_and_counted_then_loop_continues():
    executor = _RecordingExecutor()
    sink = _CountingSink()
    client = _ScriptedClient(
        [
            GenerationResult(
                text="call two",
                tool_calls=[
                    ToolCall(id="a", name="query", arguments={"q": 1}),
                    ToolCall(id="b", name="query", arguments={"q": 2}),
                ],
                usage=Usage(prompt_tokens=5),
            ),
            GenerationResult(text="done", usage=Usage(prompt_tokens=5)),
        ]
    )
    messages: list[Message] = []
    _loop(client, should_terminate=_never_terminate, executor=executor, sink=sink, max_turns=2).run(
        "sys", messages, time.time()
    )

    assert len(executor.executed) == 2
    assert sink.tool_calls == 2
    assert sink.generations == 2
    # Each tool call produced a TOOL message after the assistant message.
    tool_msgs = [m for m in messages if m.role == MessageRole.TOOL]
    assert len(tool_msgs) == 2


def test_each_executed_call_carries_its_own_provider_call_id():
    """The executor is handed ``ToolCall.id``, and the result message keys on the
    same id — so a call and its result join on the id, never on position. The two
    calls here differ only in that id and their arguments."""
    executor = _RecordingExecutor()
    client = _ScriptedClient(
        [
            GenerationResult(
                text="refund twice",
                tool_calls=[
                    ToolCall(id="toolu_A", name="refund", arguments={"payment_id": "PAY-1"}),
                    ToolCall(id="toolu_B", name="refund", arguments={"payment_id": "PAY-1"}),
                ],
                usage=Usage(prompt_tokens=5),
            ),
            GenerationResult(text="done", usage=Usage(prompt_tokens=5)),
        ]
    )
    messages: list[Message] = []
    _loop(client, should_terminate=_never_terminate, executor=executor, max_turns=2).run(
        "sys", messages, time.time()
    )

    assert [call_id for _, _, call_id in executor.executed] == ["toolu_A", "toolu_B"]
    assert [m.tool_call_id for m in messages if m.role == MessageRole.TOOL] == [
        "toolu_A",
        "toolu_B",
    ]


class _FailingTransportExecutor:
    """A ToolExecutor-shaped seam whose transport fails on one named call.

    In a Docker run the executor is the gRPC client, so a transport failure
    raises out of ``execute`` rather than coming back as a failed ``ToolResult``.
    A tool that fails *in band* is recorded and the loop carries on, which is why
    that case cannot stand in for this one.
    """

    def __init__(self, raise_on: str) -> None:
        self._raise_on = raise_on
        self.attempted: list[str] = []

    def execute(self, tool_name, arguments, *, call_id, validation_schema=None):
        self.attempted.append(call_id)
        if call_id == self._raise_on:
            raise RuntimeError("runner unreachable")
        return ToolResult(success=True, output=f"ran {tool_name}")


def test_a_failed_call_leaves_its_turns_remaining_calls_unexecuted_and_ends_the_episode():
    """The suffix invariant, asserted rather than assumed.

    The timeline joins a call to its result by occurrence order, which is sound
    only if the k-th declared occurrence of an id is the k-th executed one — that
    is, if the declarations that never executed are a trailing *suffix* of the
    trial rather than a gap in the middle. Two things make it one: a turn's calls
    run in declaration order and stop at the first failure, and the episode stops
    with them, so no later turn declares anything either.
    """
    executor = _FailingTransportExecutor(raise_on="b1")
    client = _ScriptedClient(
        [
            GenerationResult(
                text="",
                tool_calls=[ToolCall(id="a1", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=5),
            ),
            GenerationResult(
                text="",
                tool_calls=[
                    ToolCall(id="b1", name="query", arguments={"q": 2}),
                    ToolCall(id="b2", name="query", arguments={"q": 3}),
                    ToolCall(id="b3", name="query", arguments={"q": 4}),
                ],
                usage=Usage(prompt_tokens=5),
            ),
            GenerationResult(text="never reached", usage=Usage(prompt_tokens=5)),
        ]
    )
    messages: list[Message] = []

    outcome = _loop(client, should_terminate=_never_terminate, executor=executor, max_turns=3).run(
        "sys", messages, time.time()
    )

    assert executor.attempted == ["a1", "b1"]
    assert outcome.status == TrialStatus.ERROR
    assert outcome.termination_reason == TerminationReason.ERROR
    assert client.calls == 2, "the episode continued past the failure and declared more calls"
    declared = [call.id for message in messages for call in (message.tool_calls or [])]
    assert declared == ["a1", "b1", "b2", "b3"], (
        "the unexecuted calls must still reach the message view — they are the suffix "
        "the join relies on being a suffix"
    )


def test_episode_timeout_terminates_before_first_generation():
    client = _ScriptedClient([GenerationResult(text="never", usage=Usage())])
    loop = ToolCallingLoop(
        llm_client=client,
        tool_executor=_RecordingExecutor(),
        tool_schemas=[],
        config=LoopConfig(max_turns=5, episode_timeout_s=0),
        metrics=_CountingSink(),
        should_terminate=_never_terminate,
        classify_error=_classify_no_patterns,
        logger=_logger(),
    )
    messages: list[Message] = []
    # start_time in the past so elapsed > 0 immediately.
    outcome = loop.run("sys", messages, time.time() - 100)

    assert outcome.status == TrialStatus.TIMEOUT
    assert outcome.termination_reason == TerminationReason.TIMEOUT
    assert client.calls == 0


def test_generation_error_is_classified_via_shared_classifier():
    class _Boom:
        def generate(self, system, messages, tools, tool_choice="auto", observation=None):
            raise LLMApiTimeoutError("LLM API call timed out")

    messages: list[Message] = []
    outcome = _loop(_Boom(), should_terminate=_never_terminate).run("sys", messages, time.time())

    assert outcome.status == TrialStatus.ERROR
    assert outcome.termination_reason == TerminationReason.API_TIMEOUT
    assert messages[-1].role == MessageRole.SYSTEM


def test_empty_completion_terminates_before_appending():
    """A generation with no text and no tool calls terminates the loop with
    ``EMPTY_COMPLETION``, without ever appending the empty assistant message
    that a subsequent request would send to the provider."""
    client = _ScriptedClient(
        [
            GenerationResult(text="", tool_calls=[], usage=Usage(prompt_tokens=1)),
            GenerationResult(text="unreached", usage=Usage(prompt_tokens=1)),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(client, should_terminate=_never_terminate, max_turns=5).run(
        "sys", messages, time.time()
    )

    assert client.calls == 1
    assert outcome.termination_reason == TerminationReason.EMPTY_COMPLETION
    assert outcome.status == TrialStatus.FAILED
    assert not any(
        m.role == MessageRole.ASSISTANT and m.content == "" and not m.tool_calls for m in messages
    )
    assert messages[-1].role == MessageRole.SYSTEM
    assert "empty completion" in messages[-1].content


def test_empty_completion_still_records_generation_usage():
    """The trial paid for the empty completion, so metrics record it — only the
    assistant message is skipped."""
    client = _ScriptedClient(
        [GenerationResult(text="", tool_calls=[], usage=Usage(prompt_tokens=7))]
    )
    sink = _CountingSink()
    messages: list[Message] = []
    _loop(client, should_terminate=_never_terminate, sink=sink, max_turns=5).run(
        "sys", messages, time.time()
    )

    assert sink.generations == 1
    assert sink.prompt_tokens == 7


def test_api_error_retry_recovers_on_second_attempt():
    """Bounded retry: a transient API-error on turn 0 recovers on the retry,
    the tool call executes, the trial completes without a SYSTEM error."""
    sleeps: list[float] = []
    executor = _RecordingExecutor()
    client = _ScriptedClient(
        [
            RuntimeError("LLM API call failed: gemini rejected empty tail"),
            GenerationResult(
                text="ok",
                tool_calls=[ToolCall(id="a", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=5),
            ),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        executor=executor,
        config=LoopConfig(
            max_turns=1, episode_timeout_s=10_000, api_error_retries=1, api_error_backoff_s=0.0
        ),
        retry_sleep=lambda s: sleeps.append(s),
    ).run("sys", messages, time.time())

    assert client.calls == 2
    assert outcome.status == TrialStatus.COMPLETED
    assert executor.executed == [("query", {"q": 1}, "a")]
    assert not any(m.role == MessageRole.SYSTEM and "API error" in m.content for m in messages)
    assert sleeps == [0.0]


def test_api_error_retry_exhausts_and_fails_loud():
    """Retry budget spent: after ``api_error_retries + 1`` attempts, the trial
    terminates with the classified system message intact."""
    client = _ScriptedClient(
        [
            RuntimeError("LLM API call failed: gemini rejected empty tail"),
            RuntimeError("LLM API call failed: gemini rejected empty tail"),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        config=LoopConfig(
            max_turns=5, episode_timeout_s=10_000, api_error_retries=1, api_error_backoff_s=0.0
        ),
    ).run("sys", messages, time.time())

    assert client.calls == 2
    assert outcome.status == TrialStatus.ERROR
    assert outcome.termination_reason == TerminationReason.API_ERROR
    assert messages[-1].role == MessageRole.SYSTEM
    assert messages[-1].content == (
        "API error: LLM API call failed: gemini rejected empty tail. Dialogue terminated."
    )


def test_api_error_retry_does_not_mutate_messages_before_success():
    """Invariant lock: a raised attempt leaves ``messages`` unchanged, so the
    successful attempt's assistant message stands alone with no ghost entry
    from the failed attempt above it."""
    client = _ScriptedClient(
        [
            RuntimeError("LLM API call failed: gemini rejected empty tail"),
            GenerationResult(text="recovered", usage=Usage(prompt_tokens=5)),
        ]
    )
    messages: list[Message] = []
    _loop(
        client,
        should_terminate=_never_terminate,
        config=LoopConfig(
            max_turns=1, episode_timeout_s=10_000, api_error_retries=1, api_error_backoff_s=0.0
        ),
    ).run("sys", messages, time.time())

    assistant_messages = [m for m in messages if m.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "recovered"


def test_rate_limit_stays_one_shot():
    """Rate limits are not retried at the loop level — the client's own probe
    controller owns 429 recovery, and retrying them here would double-count
    the exclusion."""

    def _classify_with_rate_limit(exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    wrapped_rate_limit = RuntimeError(
        f"LLM API call failed: {RateLimitError(message='quota', llm_provider='openrouter', model='anthropic/claude')}"
    )
    wrapped_rate_limit.__cause__ = RateLimitError(
        message="quota", llm_provider="openrouter", model="anthropic/claude"
    )
    client = _ScriptedClient([wrapped_rate_limit])
    messages: list[Message] = []
    outcome = ToolCallingLoop(
        llm_client=client,
        tool_executor=_RecordingExecutor(),
        tool_schemas=[],
        config=LoopConfig(
            max_turns=5, episode_timeout_s=10_000, api_error_retries=5, api_error_backoff_s=0.0
        ),
        metrics=_CountingSink(),
        should_terminate=_never_terminate,
        classify_error=_classify_with_rate_limit,
        logger=_logger(),
        retry_sleep=lambda _s: None,
    ).run("sys", messages, time.time())

    assert client.calls == 1
    assert outcome.termination_reason == TerminationReason.RATE_LIMIT


def test_empty_completion_not_retried_by_api_error_budget():
    """The API-error retry budget does not cover empty completions: even with a
    generous ``api_error_retries``, an empty completion terminates on the first
    turn when ``empty_retry_count == 0``. The two retry classes are orthogonal —
    the empty-completion budget lives in a dedicated ``LoopConfig`` field."""
    client = _ScriptedClient(
        [GenerationResult(text="", tool_calls=[], usage=Usage(prompt_tokens=1))]
    )
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        config=LoopConfig(
            max_turns=5,
            episode_timeout_s=10_000,
            api_error_retries=5,
            api_error_backoff_s=0.0,
            empty_retry_count=0,
        ),
    ).run("sys", messages, time.time())

    assert client.calls == 1
    assert outcome.termination_reason == TerminationReason.EMPTY_COMPLETION
    assert outcome.status == TrialStatus.FAILED


def test_empty_completion_retries_up_to_configured_count_then_succeeds():
    """With ``empty_retry_count=1``, the first empty resamples once and the
    second sample's text lands as the assistant message. No ghost empty
    assistant entry is appended. Both generations bill the metrics sink because
    the trial paid for both calls."""
    client = _ScriptedClient(
        [
            GenerationResult(text="", tool_calls=[], usage=Usage(prompt_tokens=3)),
            GenerationResult(text="recovered", usage=Usage(prompt_tokens=5)),
        ]
    )
    sink = _CountingSink()
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        sink=sink,
        config=LoopConfig(
            max_turns=1,
            episode_timeout_s=10_000,
            empty_retry_count=1,
        ),
    ).run("sys", messages, time.time())

    assert client.calls == 2
    assert sink.generations == 2
    assert sink.prompt_tokens == 8
    assert outcome.termination_reason != TerminationReason.EMPTY_COMPLETION
    assert outcome.status == TrialStatus.COMPLETED
    assistant_messages = [m for m in messages if m.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "recovered"
    assert not any(
        m.role == MessageRole.ASSISTANT and m.content == "" and not m.tool_calls for m in messages
    )


def test_empty_completion_retry_exhausts_and_terminates():
    """With ``empty_retry_count=N``, ``N + 1`` consecutive empty completions
    exhaust the budget and terminate with ``EMPTY_COMPLETION``. Every resampled
    empty still bills the metrics sink; exactly one SYSTEM message closes out
    the loop with the empty-completion phrasing."""
    retry_count = 2
    client = _ScriptedClient(
        [
            GenerationResult(text="", tool_calls=[], usage=Usage(prompt_tokens=1))
            for _ in range(retry_count + 1)
        ]
    )
    sink = _CountingSink()
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        sink=sink,
        config=LoopConfig(
            max_turns=5,
            episode_timeout_s=10_000,
            empty_retry_count=retry_count,
        ),
    ).run("sys", messages, time.time())

    assert client.calls == retry_count + 1
    assert sink.generations == retry_count + 1
    assert outcome.termination_reason == TerminationReason.EMPTY_COMPLETION
    assert outcome.status == TrialStatus.FAILED
    assert messages[-1].role == MessageRole.SYSTEM
    assert "empty completion" in messages[-1].content
    system_messages = [m for m in messages if m.role == MessageRole.SYSTEM]
    assert len(system_messages) == 1


def test_empty_completion_retry_does_not_advance_turn_counter():
    """Resamples happen within the same outer turn. With ``max_turns=1`` and
    ``empty_retry_count=2`` the loop absorbs two empties on turn 0 and executes
    the recovered tool call also on turn 0, so a single outer iteration consumes
    three generations. A subsequent generation would live in turn 1, which does
    not fit under ``max_turns=1`` — this test locks that resamples do not
    themselves count against the outer turn budget."""
    executor = _RecordingExecutor()
    client = _ScriptedClient(
        [
            GenerationResult(text="", tool_calls=[], usage=Usage(prompt_tokens=1)),
            GenerationResult(text="", tool_calls=[], usage=Usage(prompt_tokens=1)),
            GenerationResult(
                text="ok",
                tool_calls=[ToolCall(id="a", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=5),
            ),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        executor=executor,
        config=LoopConfig(
            max_turns=1,
            episode_timeout_s=10_000,
            empty_retry_count=2,
        ),
    ).run("sys", messages, time.time())

    assert client.calls == 3
    assert executor.executed == [("query", {"q": 1}, "a")]
    assert outcome.termination_reason == TerminationReason.MAX_TURNS


def test_api_error_retry_budget_resets_per_outer_turn():
    """A successful turn 0 followed by an API-error turn 1 gets a fresh retry
    budget: the counter resets at the start of every outer iteration, so a
    future refactor that carried retry state across turns fails loud here."""
    executor = _RecordingExecutor()
    client = _ScriptedClient(
        [
            GenerationResult(
                text="first",
                tool_calls=[ToolCall(id="a", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=5),
            ),
            RuntimeError("LLM API call failed: gemini rejected empty tail"),
            GenerationResult(text="recovered", usage=Usage(prompt_tokens=5)),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        executor=executor,
        config=LoopConfig(
            max_turns=2, episode_timeout_s=10_000, api_error_retries=1, api_error_backoff_s=0.0
        ),
    ).run("sys", messages, time.time())

    assert client.calls == 3
    assert outcome.status == TrialStatus.COMPLETED
    assert executor.executed == [("query", {"q": 1}, "a")]
    assert not any(m.role == MessageRole.SYSTEM and "API error" in m.content for m in messages)


def test_effective_system_prompt_captured_from_first_generation_only():
    client = _ScriptedClient(
        [
            GenerationResult(text="a", usage=Usage(), effective_system_prompt="FIRST"),
            GenerationResult(text="b", usage=Usage(), effective_system_prompt="SECOND"),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(client, should_terminate=_never_terminate, max_turns=2).run(
        "sys", messages, time.time()
    )
    assert outcome.captured_effective_system_prompt == "FIRST"
