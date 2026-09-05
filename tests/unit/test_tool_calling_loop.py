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

    ``messages_history`` snapshots the wire ``messages`` argument on each call
    so a test can pin what actually reached the provider on the second call
    (a copy is taken because the loop mutates the underlying list in place).
    """

    def __init__(self, results: list[GenerationResult | Exception]) -> None:
        self._results = list(results)
        self.calls = 0
        self.messages_history: list[list[Message]] = []

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        self.calls += 1
        self.messages_history.append(list(messages))
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


def test_length_truncated_completion_not_retried_when_opt_out():
    """With ``output_length_retry_count=0`` (the default) a content-carrying
    result with ``finish_reason="length"`` lands as the assistant turn
    unchanged and the loop advances. No feedback marker is inserted — this
    locks the default-off invariant against silent drift into a global
    retry-with-feedback default (which would double reasoning spend on every
    truncation across every preset)."""
    client = _ScriptedClient(
        [
            GenerationResult(
                text="partial",
                usage=Usage(prompt_tokens=3),
                finish_reason="length",
            ),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        max_turns=1,
        config=LoopConfig(
            max_turns=1,
            episode_timeout_s=10_000,
            output_length_retry_count=0,
        ),
    ).run("sys", messages, time.time())

    assert client.calls == 1
    assistant_messages = [m for m in messages if m.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "partial"
    assert not any(
        m.role == MessageRole.USER and "truncated at max_tokens" in (m.content or "")
        for m in messages
    )
    assert outcome.termination_reason == TerminationReason.MAX_TURNS


def test_length_truncated_completion_resamples_and_recovers():
    """With ``output_length_retry_count=1`` the first truncated response is
    discarded, a ``role=user`` truncation-feedback turn is appended to both
    the recorded history and the wire history, and the second (untruncated)
    sample lands as the assistant turn. Locks the recovery shape and pins
    that the feedback actually reaches the wire on the retry call."""
    client = _ScriptedClient(
        [
            GenerationResult(
                text="partial",
                usage=Usage(prompt_tokens=3),
                finish_reason="length",
            ),
            GenerationResult(
                text="recovered",
                usage=Usage(prompt_tokens=5),
                finish_reason="stop",
            ),
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
            output_length_retry_count=1,
        ),
    ).run("sys", messages, time.time())

    assert client.calls == 2
    # (c) both generations bill the metrics sink because the trial paid.
    assert sink.generations == 2

    # (a) exactly one user-feedback turn with the truncation phrasing.
    feedback_turns = [
        m
        for m in messages
        if m.role == MessageRole.USER and "truncated at max_tokens" in (m.content or "")
    ]
    assert len(feedback_turns) == 1

    # (b) the truncated assistant message was NOT appended.
    assert not any(m.role == MessageRole.ASSISTANT and m.content == "partial" for m in messages)
    assistant_messages = [m for m in messages if m.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "recovered"

    # (d) wire-level lock: the SECOND generate call's messages list ends with
    # the truncation-feedback user turn — the retry with feedback actually
    # reaches the provider, not just the recorded trajectory.
    second_call_messages = client.messages_history[1]
    tail = second_call_messages[-1]
    assert tail.role == MessageRole.USER
    assert "truncated at max_tokens" in (tail.content or "")

    assert outcome.termination_reason == TerminationReason.MAX_TURNS


def test_length_truncated_completion_exhausts_and_accepts_last_response():
    """With ``output_length_retry_count=1``, two consecutive truncated
    responses exhaust the budget: exactly one feedback turn was inserted
    (before the second attempt) and the second truncated response lands as
    the assistant turn with its partial content preserved. The loop
    continues normally — no new terminal, no ``EMPTY_COMPLETION`` — the
    exhaustion path is strictly recoverable."""
    client = _ScriptedClient(
        [
            GenerationResult(
                text="partial-1",
                usage=Usage(prompt_tokens=3),
                finish_reason="length",
            ),
            GenerationResult(
                text="partial-2",
                usage=Usage(prompt_tokens=3),
                finish_reason="length",
            ),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(
        client,
        should_terminate=_never_terminate,
        config=LoopConfig(
            max_turns=1,
            episode_timeout_s=10_000,
            output_length_retry_count=1,
        ),
    ).run("sys", messages, time.time())

    assert client.calls == 2
    assert outcome.termination_reason == TerminationReason.MAX_TURNS
    assert outcome.status == TrialStatus.COMPLETED
    assistant_messages = [m for m in messages if m.role == MessageRole.ASSISTANT]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "partial-2"
    feedback_turns = [
        m
        for m in messages
        if m.role == MessageRole.USER and "truncated at max_tokens" in (m.content or "")
    ]
    assert len(feedback_turns) == 1


def test_length_retry_does_not_advance_turn_counter():
    """Resamples happen within the same outer turn. With ``max_turns=1`` and
    ``output_length_retry_count=2`` the loop absorbs two truncated resamples
    on turn 0 and executes the recovered tool call also on turn 0, so a
    single outer iteration consumes three generations. Mirrors the shape of
    ``test_empty_completion_retry_does_not_advance_turn_counter``."""
    executor = _RecordingExecutor()
    client = _ScriptedClient(
        [
            GenerationResult(
                text="partial-1",
                usage=Usage(prompt_tokens=1),
                finish_reason="length",
            ),
            GenerationResult(
                text="partial-2",
                usage=Usage(prompt_tokens=1),
                finish_reason="length",
            ),
            GenerationResult(
                text="ok",
                tool_calls=[ToolCall(id="a", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=5),
                finish_reason="tool_calls",
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
            output_length_retry_count=2,
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


class _ScriptedOutputExecutor:
    """Yields a fixed sequence of ``ToolResult`` payloads, one per execute call.

    The loop-layer cap tests need control over the executed output — a bare
    ``_RecordingExecutor`` returns a synthesized "ran <name>" string that is
    short by design. This fake plays back caller-supplied results without
    otherwise altering the ToolExecutor contract.
    """

    def __init__(self, results: list[ToolResult]) -> None:
        self._results = list(results)
        self.executed: list[tuple[str, dict, str]] = []

    def execute(self, tool_name, arguments, *, call_id, validation_schema=None):
        self.executed.append((tool_name, arguments, call_id))
        return self._results.pop(0)


class _RecordingRecorder:
    """Minimal ``ToolCallRecorder``-shaped fake — captures the ``output`` kwarg.

    The loop-layer cap must not touch the recorder's view of the tool call:
    the trial's ordered record and the grader inputs read the full text
    upstream of the truncation.
    """

    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(
        self,
        *,
        call_id,
        tool_name,
        arguments,
        executor,
        status,
        output,
        latency_seconds,
    ) -> None:
        self.records.append(
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "output": output,
            }
        )

    @property
    def recorded(self) -> tuple:
        return tuple(self.records)


def _one_tool_call_then_stop(name: str = "read_file") -> _ScriptedClient:
    """Two-generation script: one tool call, then a text-only reply that stops
    the loop by ``max_turns=1``-exhaustion.
    """
    return _ScriptedClient(
        [
            GenerationResult(
                text="",
                tool_calls=[ToolCall(id="t1", name=name, arguments={"path": "x"})],
                usage=Usage(prompt_tokens=1),
            ),
        ]
    )


def _run_capped_tool_output_loop(
    tool_result: ToolResult,
    cap: int | None,
    *,
    recorder: _RecordingRecorder | None = None,
) -> tuple[list[Message], _ScriptedOutputExecutor]:
    executor = _ScriptedOutputExecutor([tool_result])
    client = _one_tool_call_then_stop()
    messages: list[Message] = []
    loop = ToolCallingLoop(
        llm_client=client,
        tool_executor=executor,
        tool_schemas=[],
        config=LoopConfig(
            max_turns=1,
            episode_timeout_s=10_000,
            tool_output_max_chars=cap,
        ),
        metrics=_CountingSink(),
        should_terminate=_never_terminate,
        classify_error=_classify_no_patterns,
        logger=_logger(),
        recorder=recorder,
        retry_sleep=lambda _s: None,
    )
    loop.run("sys", messages, time.time())
    return messages, executor


def _only_tool_message(messages: list[Message]) -> Message:
    tool_messages = [m for m in messages if m.role == MessageRole.TOOL]
    assert len(tool_messages) == 1, "expected exactly one tool message"
    return tool_messages[0]


def test_tool_output_capped_at_loop_layer_when_capability_set():
    """The ``role=tool`` message content is middle-elided with the shared
    marker when ``LoopConfig.tool_output_max_chars`` is set; the recorder still
    sees the full untruncated text."""
    recorder = _RecordingRecorder()
    huge = "X" * 40_000
    messages, _ = _run_capped_tool_output_loop(
        ToolResult(success=True, output=huge),
        cap=16_000,
        recorder=recorder,
    )
    tool_message = _only_tool_message(messages)
    assert len(tool_message.content) < 40_000
    assert "\n...[24000 chars omitted]...\n" in tool_message.content
    assert tool_message.content.startswith("X" * 8_000)
    assert tool_message.content.endswith("X" * 8_000)
    assert tool_message.content_blocks is None
    assert len(recorder.records) == 1
    assert recorder.records[0]["output"] == huge


def test_tool_output_not_capped_when_capability_none():
    """Default ``tool_output_max_chars=None`` threads the tool output through
    unchanged — the pre-opt-in baseline for every preset."""
    huge = "Y" * 40_000
    messages, _ = _run_capped_tool_output_loop(
        ToolResult(success=True, output=huge),
        cap=None,
    )
    tool_message = _only_tool_message(messages)
    assert tool_message.content == huge
    assert "chars omitted" not in tool_message.content


def test_tool_output_content_blocks_left_untouched_when_capped():
    """Only ``Message.content`` is capped — ``content_blocks`` (multimodal
    payloads) pass through as-is."""
    blocks = [{"type": "image", "data": "sentinel"}]
    huge = "Z" * 40_000
    messages, _ = _run_capped_tool_output_loop(
        ToolResult(success=True, output=huge, content_blocks=blocks),
        cap=16_000,
    )
    tool_message = _only_tool_message(messages)
    assert tool_message.content_blocks == blocks
    assert len(tool_message.content) < 40_000
    assert "chars omitted" in tool_message.content


def test_tool_output_error_path_also_capped():
    """The ``Error: ...`` branch flows through the same cap — a runaway error
    string cannot silently blow past the loop's guarantee."""
    huge_error = "E" * 40_000
    messages, _ = _run_capped_tool_output_loop(
        ToolResult(success=False, output="", error=huge_error),
        cap=16_000,
    )
    tool_message = _only_tool_message(messages)
    assert tool_message.content.startswith("Error: ")
    assert len(tool_message.content) < 40_000 + len("Error: ")
    assert "chars omitted" in tool_message.content
    assert tool_message.content_blocks is None


# ---------------------------------------------------------------------------
# Context-window summarize + handoff (see tolokaforge.core.summarize_policy).
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Records every ``generate`` invocation's wire message tail.

    The summarize tests need to inspect what ``_wire_messages`` looked like at
    each call site; a scripted list of results plus a per-call snapshot of
    ``messages`` gives the assertions concrete evidence rather than mocked
    state.
    """

    def __init__(self, results: list[GenerationResult | Exception]) -> None:
        self._results = list(results)
        self.calls = 0
        self.wire_snapshots: list[list[Message]] = []

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        self.calls += 1
        self.wire_snapshots.append(list(messages))
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _ScriptedSummarizer:
    """SummarizePolicy that returns pre-scripted recaps.

    A recap value of ``""`` triggers ``SummarizerFailedError`` inside
    ``LLMSummarizer``. To keep tests small, this class emulates that same
    behaviour without an underlying ``LLMSummarizer`` — the loop consumes
    the Protocol, not the concrete class.
    """

    def __init__(self, recaps: list[str | Exception]) -> None:
        self._recaps = list(recaps)
        self.calls = 0
        self.received_messages: list[list[Message]] = []

    def summarize(self, system_prompt: str, messages: list[Message]) -> str:
        self.calls += 1
        self.received_messages.append(list(messages))
        item = self._recaps.pop(0)
        if isinstance(item, Exception):
            raise item
        if not item:
            from tolokaforge.core.summarize_policy import SummarizerFailedError

            raise SummarizerFailedError("empty recap")
        return item


class _WatermarkSink(MetricsSink):
    """Metrics sink that returns a scripted ``last_prompt_tokens``.

    Real ``_AgentMetricsSink`` derives the value from ``result.usage``; this
    fake lets a test set the exact watermark trigger without threading a
    ``GenerationResult`` through it.
    """

    def __init__(self, last_prompt_tokens_stream: list[int | None]) -> None:
        self.generations = 0
        self.tool_calls = 0
        self._stream = list(last_prompt_tokens_stream)
        self._value: int | None = None

    def record_generation(self, result: GenerationResult) -> None:
        self.generations += 1
        if self._stream:
            self._value = self._stream.pop(0)

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    @property
    def last_prompt_tokens(self) -> int | None:
        return self._value


def _summarize_config(
    *,
    max_context_tokens: int | None,
    context_watermark: int | None,
    summarize_policy,
    max_turns: int = 5,
) -> LoopConfig:
    return LoopConfig(
        max_turns=max_turns,
        episode_timeout_s=10_000,
        max_context_tokens=max_context_tokens,
        context_watermark=context_watermark,
        summarize_policy=summarize_policy,
    )


def _summarize_loop(client, *, sink, summarizer, config, user_turn=None, executor=None):
    return ToolCallingLoop(
        llm_client=client,
        tool_executor=executor or _RecordingExecutor(),
        tool_schemas=[],
        config=config,
        metrics=sink,
        should_terminate=_never_terminate,
        classify_error=_classify_no_patterns,
        logger=_logger(),
        user_turn=user_turn,
        retry_sleep=lambda _s: None,
    )


def _first_user() -> Message:
    return Message(role=MessageRole.USER, content="the original task")


def test_summarize_fires_at_watermark_before_generate():
    """Pre-turn summarize: last generation crossed the watermark, so the
    loop rewrites ``_wire_messages`` to ``[first_user, recap, marker]``
    and the caller-visible ``messages`` list still carries the full
    pre-summarize history plus the marker."""
    summarizer = _ScriptedSummarizer(["compact-recap"])
    sink = _WatermarkSink([950, 100])
    client = _RecordingClient(
        [
            GenerationResult(
                text="reach for a tool",
                tool_calls=[ToolCall(id="t1", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=950),
            ),
            GenerationResult(text="all done", usage=Usage(prompt_tokens=100)),
        ]
    )
    messages: list[Message] = [_first_user()]
    _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=1000,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=2,
        ),
    ).run("sys", messages, time.time())

    assert summarizer.calls == 1
    # Turn 1's wire messages after the summarize event: seed + recap + marker.
    assert client.calls == 2
    turn_one_wire = client.wire_snapshots[1]
    assert [(m.role, m.content) for m in turn_one_wire[:3]] == [
        (MessageRole.USER, "the original task"),
        (MessageRole.USER, "compact-recap"),
        (MessageRole.SYSTEM, turn_one_wire[2].content),
    ]
    assert "Context summarized" in turn_one_wire[2].content
    # The caller-visible list carries the full pre-summarize history.
    assert [(m.role, m.content) for m in messages if m.role != MessageRole.SYSTEM][:2] == [
        (MessageRole.USER, "the original task"),
        (MessageRole.ASSISTANT, "reach for a tool"),
    ]
    assert any(m.role is MessageRole.SYSTEM and "Context summarized" in m.content for m in messages)


def test_no_summarize_when_below_watermark():
    """Watermark check reads the LAST generation's prompt_tokens; when it
    is below the trigger, summarize never fires."""
    summarizer = _ScriptedSummarizer([])
    sink = _WatermarkSink([500, 500])
    client = _RecordingClient(
        [
            GenerationResult(
                text="",
                tool_calls=[ToolCall(id="t1", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=500),
            ),
            GenerationResult(text="done", usage=Usage(prompt_tokens=500)),
        ]
    )
    messages: list[Message] = [_first_user()]
    _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=1000,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=2,
        ),
    ).run("sys", messages, time.time())

    assert summarizer.calls == 0
    # Wire messages grew normally through the second turn.
    assert client.wire_snapshots[1][0].content == "the original task"


def test_no_summarize_when_capability_off():
    """With any of the three fields ``None``, the loop's behaviour is
    byte-for-byte the pre-opt-in path — even a prompt_tokens value that
    would otherwise cross the watermark does not fire summarize."""
    summarizer = _ScriptedSummarizer([])
    sink = _WatermarkSink([9999])
    client = _RecordingClient([GenerationResult(text="finished", usage=Usage(prompt_tokens=9999))])
    messages: list[Message] = [_first_user()]
    _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=None,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=1,
        ),
    ).run("sys", messages, time.time())

    assert summarizer.calls == 0


def test_reactive_context_window_exceeded_triggers_summarize_when_opted_in():
    """Reactive path: the first ``_generate`` raises
    ``ContextWindowExceededError``; the loop summarizes and retries once
    inline, and the trial keeps running."""
    from litellm.exceptions import ContextWindowExceededError

    summarizer = _ScriptedSummarizer(["reactive-recap"])
    sink = _WatermarkSink([5])
    client = _RecordingClient(
        [
            ContextWindowExceededError("input too large", "anthropic/claude", "anthropic"),
            GenerationResult(text="ok after summarize", usage=Usage(prompt_tokens=5)),
        ]
    )
    messages: list[Message] = [_first_user()]
    outcome = _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=1000,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=1,
        ),
    ).run("sys", messages, time.time())

    assert summarizer.calls == 1
    assert client.calls == 2
    assert outcome.termination_reason != TerminationReason.CONTEXT_WINDOW_EXCEEDED
    # Second generate received the compacted wire messages.
    second_wire = client.wire_snapshots[1]
    assert [m.content for m in second_wire[:2]] == ["the original task", "reactive-recap"]


def test_reactive_retry_after_summarize_still_context_exceeded():
    """When summarize succeeded but the post-summarize retry `_generate`
    also raises ``ContextWindowExceededError``, the loop terminates loud-fail
    with the typed reason. Bounds the retry topology — no iterated summarize."""
    from litellm.exceptions import ContextWindowExceededError

    summarizer = _ScriptedSummarizer(["recap-worked-but-still-too-big"])
    sink = _WatermarkSink([5])
    client = _RecordingClient(
        [
            ContextWindowExceededError("input too large", "anthropic/claude", "anthropic"),
            ContextWindowExceededError("still too large", "anthropic/claude", "anthropic"),
        ]
    )
    messages: list[Message] = [_first_user()]
    outcome = _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=1000,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=1,
        ),
    ).run("sys", messages, time.time())

    assert outcome.termination_reason is TerminationReason.CONTEXT_WINDOW_EXCEEDED
    assert outcome.status is TrialStatus.FAILED
    assert summarizer.calls == 1
    assert client.calls == 2


def test_reactive_context_window_exceeded_terminates_when_not_opted_in():
    """Without summarize opted in, the raise reaches the classifier's
    typed ``ContextWindowExceededError`` branch and terminates with
    that typed reason."""
    from litellm.exceptions import ContextWindowExceededError

    client = _ScriptedClient(
        [ContextWindowExceededError("input too large", "anthropic/claude", "anthropic")]
    )
    messages: list[Message] = [_first_user()]
    outcome = _loop(client, should_terminate=_never_terminate, max_turns=1).run(
        "sys", messages, time.time()
    )

    assert outcome.termination_reason is TerminationReason.CONTEXT_WINDOW_EXCEEDED
    assert outcome.status is TrialStatus.FAILED
    assert any(
        m.role is MessageRole.SYSTEM and "Context window exceeded" in m.content for m in messages
    )


def test_summarize_empty_result_terminates_context_window_exceeded():
    """An empty recap means summarize could not recover; the loop
    terminates with the typed reason and the system message names the
    empty-recap branch."""
    summarizer = _ScriptedSummarizer([""])
    sink = _WatermarkSink([950])
    client = _RecordingClient(
        [
            GenerationResult(
                text="",
                tool_calls=[ToolCall(id="t1", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=950),
            ),
            GenerationResult(text="unreached", usage=Usage(prompt_tokens=1)),
        ]
    )
    messages: list[Message] = [_first_user()]
    outcome = _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=1000,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=3,
        ),
    ).run("sys", messages, time.time())

    assert outcome.termination_reason is TerminationReason.CONTEXT_WINDOW_EXCEEDED
    assert outcome.status is TrialStatus.FAILED
    assert any(m.role is MessageRole.SYSTEM and "produced no recap" in m.content for m in messages)


def test_summarize_own_call_context_exceeded_terminates():
    """The summarize ``.summarize`` call raising
    ``ContextWindowExceededError`` (the pre-summarize history alone
    already exceeds the window) terminates with the typed reason.
    Distinct wording from the empty-recap branch so post-run analysis
    can tell them apart."""
    from litellm.exceptions import ContextWindowExceededError

    summarizer = _ScriptedSummarizer(
        [ContextWindowExceededError("summarize prompt too large", "anthropic/claude", "anthropic")]
    )
    sink = _WatermarkSink([950])
    client = _RecordingClient(
        [
            GenerationResult(
                text="",
                tool_calls=[ToolCall(id="t1", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=950),
            ),
            GenerationResult(text="unreached", usage=Usage(prompt_tokens=1)),
        ]
    )
    messages: list[Message] = [_first_user()]
    outcome = _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=1000,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=3,
        ),
    ).run("sys", messages, time.time())

    assert outcome.termination_reason is TerminationReason.CONTEXT_WINDOW_EXCEEDED
    assert outcome.status is TrialStatus.FAILED
    assert any(
        m.role is MessageRole.SYSTEM
        and "Summarize call itself exceeded the context window" in m.content
        for m in messages
    )
    assert summarizer.calls == 1  # no iterated summarize


def test_summarize_records_full_history_in_trajectory_messages():
    """Grading invariant: after summarize fires mid-trial, the
    caller-visible ``messages`` list (== ``Trajectory.messages``) carries
    the pre-summarize turns, the summarize system marker, and the
    post-summarize turns end-to-end."""
    summarizer = _ScriptedSummarizer(["mid-trial recap"])
    sink = _WatermarkSink([950, 20])
    client = _RecordingClient(
        [
            GenerationResult(
                text="turn0 text",
                tool_calls=[ToolCall(id="t1", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=950),
            ),
            GenerationResult(text="turn1 done", usage=Usage(prompt_tokens=20)),
        ]
    )
    messages: list[Message] = [_first_user()]
    _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=1000,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=2,
        ),
    ).run("sys", messages, time.time())

    kinds = [(m.role, m.content) for m in messages]
    # Pre-summarize: user, assistant, tool. Summarize marker (SYSTEM), then
    # post-summarize assistant.
    assert kinds[0] == (MessageRole.USER, "the original task")
    assert kinds[1] == (MessageRole.ASSISTANT, "turn0 text")
    assert kinds[2][0] == MessageRole.TOOL
    summarize_idx = next(
        i
        for i, k in enumerate(kinds)
        if k[0] == MessageRole.SYSTEM and "Context summarized" in k[1]
    )
    assistant_texts = [k[1] for k in kinds if k[0] == MessageRole.ASSISTANT]
    assert assistant_texts == ["turn0 text", "turn1 done"]
    # Post-summarize assistant lives after the marker.
    assert kinds[summarize_idx + 1] == (MessageRole.ASSISTANT, "turn1 done")


def test_classify_loop_error_typed_context_window_exceeded():
    """The classifier's typed branch matches
    ``ContextWindowExceededError`` before the ``"API" in error_str``
    substring fallback and routes to
    ``TerminationReason.CONTEXT_WINDOW_EXCEEDED`` / ``TrialStatus.FAILED``."""
    from litellm.exceptions import ContextWindowExceededError

    exc = ContextWindowExceededError("input too large", "anthropic/claude", "anthropic")
    decision = classify_loop_error(exc, ())
    assert decision.reason is TerminationReason.CONTEXT_WINDOW_EXCEEDED
    assert decision.status is TrialStatus.FAILED
    assert "Context window exceeded" in decision.system_message


class _OnceUserTurn:
    """UserTurn seam that returns a scripted user message on the first call."""

    def __init__(self, message: Message) -> None:
        self._message = message
        self.calls = 0

    def __call__(self, messages: list[Message]):
        from tolokaforge.core.loop import UserTurnResult

        self.calls += 1
        return UserTurnResult(message=self._message)


def test_user_turn_reply_appears_on_wire_after_summarize():
    """A user simulator reply, appended after a text-only assistant turn,
    must land on the wire. This test drives a summarize event, then a
    no-tool-call assistant turn (which triggers ``_advance_user_turn``),
    and asserts the third ``_generate`` call's wire tail is the user
    reply. Guards the append site inside ``_advance_user_turn``."""
    summarizer = _ScriptedSummarizer(["recap-before-user-turn"])
    sink = _WatermarkSink([950, 50, 60])
    user_turn = _OnceUserTurn(Message(role=MessageRole.USER, content="follow-up"))
    client = _RecordingClient(
        [
            GenerationResult(
                text="turn0 text",
                tool_calls=[ToolCall(id="t1", name="query", arguments={"q": 1})],
                usage=Usage(prompt_tokens=950),
            ),
            GenerationResult(text="turn1 text-only", usage=Usage(prompt_tokens=50)),
            GenerationResult(text="turn2 wrap up", usage=Usage(prompt_tokens=60)),
        ]
    )
    messages: list[Message] = [_first_user()]
    _summarize_loop(
        client,
        sink=sink,
        summarizer=summarizer,
        config=_summarize_config(
            max_context_tokens=1000,
            context_watermark=100,
            summarize_policy=summarizer,
            max_turns=3,
        ),
        user_turn=user_turn,
    ).run("sys", messages, time.time())

    assert client.calls == 3
    third_wire = client.wire_snapshots[2]
    assert third_wire[-1].role is MessageRole.USER
    assert third_wire[-1].content == "follow-up"


def test_summarize_call_is_billed_through_metrics():
    """LLMSummarizer bills its own generation through the injected metrics
    sink so the trial's total cost includes the summarize call."""
    from tolokaforge.core.summarize_policy import LLMSummarizer

    class _MinimalClient:
        def generate(self, system, messages, tools, tool_choice="none"):
            return GenerationResult(
                text="cheap recap",
                usage=Usage(prompt_tokens=100_000, completion_tokens=500),
                cost_usd=0.42,
            )

    class _Billing:
        def __init__(self) -> None:
            self.generations: list[GenerationResult] = []

        def record_generation(self, result: GenerationResult) -> None:
            self.generations.append(result)

    billing = _Billing()
    summarizer = LLMSummarizer(_MinimalClient(), billing)
    recap = summarizer.summarize("system prompt", [_first_user()])

    assert recap == "cheap recap"
    assert len(billing.generations) == 1
    assert billing.generations[0].usage.prompt_tokens == 100_000
    assert billing.generations[0].cost_usd == 0.42
