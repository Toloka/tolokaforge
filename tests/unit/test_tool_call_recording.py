"""One recorded-tool-call type, recorded once, in true order.

The trial owns a single :class:`TrialToolCallRecorder`; every executor records
into it, so ``sequence`` is execution order across executors rather than
position within one of them. The executors keep no history of their own, which
is what puts every early-return rejection into the record and lets the
recording caller — not the tool — supply the clock.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.trace_timeline import TraceEventKind, build_trial_timeline
from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import (
    LoopConfig,
    MetricsSink,
    TerminationDecision,
    ToolCallingLoop,
    classify_loop_error,
)
from tolokaforge.core.models import (
    Message,
    MessageRole,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
)
from tolokaforge.core.runner import TrialRunner, TrialToolCallRecorder
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.protocol import TrialNotRegisteredError
from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry, ToolResult

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Real tools, real registry — no mock stands between a branch and its status
# ---------------------------------------------------------------------------


class _Echo(Tool):
    """Echoes ``payload`` back. Its schema requires a string, so a non-string
    argument reaches the executor's real jsonschema validation."""

    def __init__(self, name: str = "echo") -> None:
        super().__init__(name, "Echo the payload back")

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"payload": {"type": "string"}},
                    "required": ["payload"],
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output=str(kwargs.get("payload")))


class _Slow(Tool):
    """Takes measurable wall time and reports no duration of its own — the shape
    every real tool has, which is why latency must come from the caller."""

    DURATION_S = 0.05

    def __init__(self) -> None:
        super().__init__("slow", "Take a measurable amount of time")

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        time.sleep(self.DURATION_S)
        return ToolResult(success=True, output="slept")


class _Boom(Tool):
    def __init__(self) -> None:
        super().__init__("boom", "Raise")

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("kaboom")


class _Silent(Tool):
    """Fails without saying why: ``error`` is left unset, not merely empty."""

    def __init__(self) -> None:
        super().__init__("silent", "Fail without saying why")

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=False, output="")


class _Secretive(Tool):
    """Takes an argument whose name the deleted redaction rewrote."""

    def __init__(self) -> None:
        super().__init__("authorize", "Authorize with a token")

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"token": {"type": "string"}},
                    "required": ["token"],
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output="authorized")


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


class _CallIdWatchingExecutor(ToolExecutor):
    """The real executor, plus the ``call_id`` every call arrived with.

    A second observation, not a restatement of the recorder's: the id the
    executor receives is the id the gRPC client puts on the wire, and therefore
    the one the runner's own trial-context record keys on.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)
        self.call_ids: list[str] = []

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        call_id: str,
        validation_schema: dict[str, Any] | None = None,
    ) -> ToolResult:
        self.call_ids.append(call_id)
        return super().execute(
            tool_name, arguments, call_id=call_id, validation_schema=validation_schema
        )


class _ScriptedClient:
    """Yields a fixed sequence of GenerationResults, one per generate call."""

    def __init__(self, results: list[GenerationResult]) -> None:
        self._results = list(results)
        self.calls = 0

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        self.calls += 1
        return self._results.pop(0)

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict] | None:
        """Return ``None`` so the loop takes its no-override branch: arguments are
        validated against each tool's own declared schema, which is what these
        recording-shape tests fix as the reference behaviour.
        """
        return None


class _CountingSink(MetricsSink):
    def record_generation(self, result: GenerationResult) -> None:
        pass

    def record_tool_call(self) -> None:
        pass


def _classify_no_patterns(exc: Exception) -> TerminationDecision:
    return classify_loop_error(exc, ())


def _drive_loop(
    tool_calls: list[list[ToolCall]],
    executor: ToolExecutor,
    recorder: TrialToolCallRecorder | None,
) -> list[Message]:
    """Run the real loop over ``tool_calls``, one assistant turn per element,
    and return the transcript it built."""
    results = [
        GenerationResult(text="", tool_calls=calls, usage=Usage(prompt_tokens=1))
        for calls in tool_calls
    ]
    messages: list[Message] = []
    ToolCallingLoop(
        llm_client=_ScriptedClient(results),
        tool_executor=executor,
        tool_schemas=[],
        config=LoopConfig(max_turns=len(results), episode_timeout_s=10_000),
        metrics=_CountingSink(),
        should_terminate=lambda result, turn, messages: None,
        classify_error=_classify_no_patterns,
        logger=get_logger("recording-test", strict=False),
        recorder=recorder,
    ).run("sys", messages, time.time())
    return messages


def _record_one(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    registry: ToolRegistry,
) -> Any:
    """Drive one real tool call through the real loop and return its record."""
    recorder = TrialToolCallRecorder()
    _drive_loop(
        [[ToolCall(id="toolu_A", name=tool_name, arguments=arguments)]],
        ToolExecutor(registry),
        recorder,
    )
    assert len(recorder.recorded) == 1, (
        f"the call to {tool_name!r} produced {len(recorder.recorded)} records; every call "
        "the loop makes must produce exactly one"
    )
    return recorder.recorded[0]


def _message_and_record(tool: Tool) -> tuple[Message, Any]:
    """One call to ``tool``: the ``role: tool`` message the agent reads, and the
    record a matcher reads."""
    recorder = TrialToolCallRecorder()
    messages = _drive_loop(
        [[ToolCall(id="toolu_A", name=tool.name, arguments={})]],
        ToolExecutor(_registry(tool)),
        recorder,
    )
    message = next(message for message in messages if message.tool_call_id == "toolu_A")
    return message, recorder.recorded[0]


def _run_two_actor_trial(
    agent_results: list[GenerationResult],
    simulator_replies: list[GenerationResult],
    *,
    agent_registry: ToolRegistry,
    user_registry: ToolRegistry,
    initial_user_message: str = "go",
) -> Trajectory:
    """Run one real trial in which both actors can execute tools.

    The user branch is driven on :class:`TrialRunner` — the seam the conductor
    hands a user-side executor to — over a real :class:`ToolExecutor`, not a
    mock of one. An empty ``initial_user_message`` routes turn 0 through the
    simulator, so the first reply in ``simulator_replies`` is the opening turn.
    """
    simulator = MagicMock()
    simulator.last_system_prompt = None
    simulator.reply.side_effect = simulator_replies

    return TrialRunner(
        task_id="two-actors",
        trial_index=0,
        agent_client=_ScriptedClient(agent_results),
        user_simulator=simulator,
        tool_executor=ToolExecutor(agent_registry),
        tool_schemas=[],
        user_tool_executor=ToolExecutor(user_registry),
        max_turns=6,
    ).run("sys", initial_user_message=initial_user_message)


def _agent_turn(text: str, *calls: ToolCall) -> GenerationResult:
    return GenerationResult(text=text, tool_calls=list(calls), usage=Usage(prompt_tokens=1))


# ---------------------------------------------------------------------------
# Every branch of the in-process executor records, with its own status
# ---------------------------------------------------------------------------


class TestEveryBranchRecordsItsStatus:
    """The executor's four distinct outcomes each reach the record with the
    status that names them — driven through the real registry and real tools,
    with no inspection of ``error`` text anywhere.

    Three of these branches ``return`` before the executor's old log append, so
    an unknown tool and a schema-violating argument recorded *nothing* while the
    loop still appended a ``role: tool`` error. Recording at the caller makes
    bypassing it impossible by construction.
    """

    def test_a_clean_call_records_success(self) -> None:
        record = _record_one("echo", {"payload": "hi"}, registry=_registry(_Echo()))
        assert record.status is ToolExecutionStatus.SUCCESS
        assert record.output == "hi"

    def test_an_unregistered_tool_records_tool_not_found(self) -> None:
        record = _record_one("no_such_tool", {}, registry=_registry(_Echo()))
        assert record.status is ToolExecutionStatus.TOOL_NOT_FOUND

    def test_schema_violating_arguments_record_invalid_arguments(self) -> None:
        record = _record_one("echo", {"payload": 17}, registry=_registry(_Echo()))
        assert record.status is ToolExecutionStatus.INVALID_ARGUMENTS

    def test_a_tool_that_raises_records_error(self) -> None:
        record = _record_one("boom", {}, registry=_registry(_Boom()))
        assert record.status is ToolExecutionStatus.ERROR

    def test_a_result_with_no_status_and_no_success_records_error(self) -> None:
        """A ``ToolResult`` built anywhere in the tree carries ``status=None``;
        the resolution is coarse but accurate, never a guess from ``error``."""

        class _InBandFailure(Tool):
            def __init__(self) -> None:
                super().__init__("in_band", "Report failure in band")

            def get_schema(self) -> dict[str, Any]:
                return {
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "description": self.description,
                        "parameters": {"type": "object", "properties": {}},
                    },
                }

            def execute(self, **kwargs: Any) -> ToolResult:
                return ToolResult(success=False, output="", error="the account is closed")

        record = _record_one("in_band", {}, registry=_registry(_InBandFailure()))
        assert record.status is ToolExecutionStatus.ERROR
        assert record.output == "the account is closed"


# ---------------------------------------------------------------------------
# Latency comes from the caller's clock
# ---------------------------------------------------------------------------


class TestLatencyIsMeasuredByTheCaller:
    def test_a_call_that_takes_real_time_records_real_time(self) -> None:
        """``ToolResult.duration_s`` defaults to ``0.0`` and no tool sets it, so
        resolving latency from it would record ``0.0`` for every successful call
        — and tool wall-time budgets would grade against a constant."""
        record = _record_one("slow", {}, registry=_registry(_Slow()))

        assert record.latency_seconds >= _Slow.DURATION_S, (
            f"a tool that slept {_Slow.DURATION_S}s recorded "
            f"latency_seconds={record.latency_seconds}"
        )

    def test_the_tools_own_duration_field_is_not_the_source(self) -> None:
        """Proof the assertion above discriminates: the field a naive
        implementation would have read is still ``0.0`` for this same call."""
        result = ToolExecutor(_registry(_Slow())).execute("slow", {}, call_id="toolu_A")

        assert result.duration_s == 0.0


# ---------------------------------------------------------------------------
# Arguments are recorded verbatim
# ---------------------------------------------------------------------------


class TestArgumentsAreRecordedVerbatim:
    def test_a_token_named_argument_reaches_the_record_unredacted(self) -> None:
        """The grader's input must not be edited. The runner substrate never
        redacted, so a core-side rewrite of ``token`` / ``password`` / ``secret``
        / ``api_key`` made the two substrates disagree on the same call — and a
        matcher over arguments would pass on one substrate and fail on the
        other. Redacting an *artifact* is a separate concern, handled by the
        write policy in ``OutputWriter``; redacting the grader's input is a
        correctness bug."""
        record = _record_one(
            "authorize", {"token": "sk-live-abc123"}, registry=_registry(_Secretive())
        )

        assert record.arguments == {"token": "sk-live-abc123"}


# ---------------------------------------------------------------------------
# One recorder, one order, across executors
# ---------------------------------------------------------------------------


class TestOneOrderedRecordPerTrial:
    def test_the_call_id_and_sequence_distinguish_two_identical_calls(self) -> None:
        recorder = TrialToolCallRecorder()
        _drive_loop(
            [
                [ToolCall(id="toolu_A", name="echo", arguments={"payload": "hi"})],
                [ToolCall(id="toolu_B", name="echo", arguments={"payload": "hi"})],
            ],
            ToolExecutor(_registry(_Echo())),
            recorder,
        )

        assert [(call.call_id, call.sequence) for call in recorder.recorded] == [
            ("toolu_A", 0),
            ("toolu_B", 1),
        ]

    def test_interleaved_agent_and_user_calls_keep_execution_order(self) -> None:
        """agent, user, agent — in that order, with the right executor on each.

        The replaced implementation read one list per executor and concatenated
        them, so this trial recorded agent, agent, user: interleaved order was
        destroyed by construction. The user branch is therefore driven directly
        on :class:`TrialRunner`, which is the seam the conductor hands a
        user-side executor to, not a mock of it.
        """
        trajectory = _run_two_actor_trial(
            [
                _agent_turn(
                    "", ToolCall(id="toolu_A", name="agent_tool", arguments={"payload": "a"})
                ),
                _agent_turn("your turn"),
                _agent_turn(
                    "", ToolCall(id="toolu_B", name="agent_tool", arguments={"payload": "b"})
                ),
                _agent_turn("over to you"),
            ],
            [
                GenerationResult(
                    text="over to you",
                    tool_calls=[
                        ToolCall(id="toolu_U", name="user_tool", arguments={"payload": "u"})
                    ],
                ),
                GenerationResult(text="###STOP###"),
            ],
            agent_registry=_registry(_Echo("agent_tool")),
            user_registry=_registry(_Echo("user_tool")),
        )

        assert [(call.sequence, call.call_id, call.executor) for call in trajectory.tool_log] == [
            (0, "toolu_A", ToolExecutorIdentity.AGENT),
            (1, "toolu_U", ToolExecutorIdentity.USER),
            (2, "toolu_B", ToolExecutorIdentity.AGENT),
        ]

    def test_the_agent_only_view_excludes_user_calls(self) -> None:
        """Stuck detection reads the agent's own repetition, so a user-side call
        must not enter its last-N window."""
        recorder = TrialToolCallRecorder()
        for index, executor in enumerate(
            (
                ToolExecutorIdentity.AGENT,
                ToolExecutorIdentity.USER,
                ToolExecutorIdentity.AGENT,
            )
        ):
            recorder.record(
                call_id=f"toolu_{index}",
                tool_name="echo",
                arguments={},
                executor=executor,
                status=ToolExecutionStatus.SUCCESS,
                output="",
                latency_seconds=0.0,
            )

        agent_only = recorder.recorded_for(ToolExecutorIdentity.AGENT)

        assert [call.sequence for call in agent_only] == [0, 2]
        assert [call.sequence for call in recorder.recorded] == [0, 1, 2]

    def test_a_recorderless_loop_does_not_grow_the_trials_record(self) -> None:
        """The rubric judge runs this same loop over its own read-only tools and
        passes no recorder, so a grading-time tool call can never enter the
        trial's record.

        Driven in two halves against the *same* executor so the assertion
        discriminates: the first half proves the recorder does record through
        this path, and the second proves a recorderless run over the same
        executor leaves that record untouched. Asserting only that a
        never-injected recorder is empty would pass however the loop behaved.
        """
        executor = ToolExecutor(_registry(_Echo()))
        trial_recorder = TrialToolCallRecorder()

        _drive_loop(
            [[ToolCall(id="toolu_A", name="echo", arguments={"payload": "agent"})]],
            executor,
            trial_recorder,
        )
        assert [call.call_id for call in trial_recorder.recorded] == ["toolu_A"], (
            "the recorder does not record through this path at all, so the "
            "recorderless assertion below would prove nothing"
        )

        _drive_loop(
            [[ToolCall(id="toolu_J", name="echo", arguments={"payload": "judge"})]],
            executor,
            None,
        )

        assert [call.call_id for call in trial_recorder.recorded] == ["toolu_A"]


# ---------------------------------------------------------------------------
# The loop assigns the episode-unique id before anything downstream reads it
# ---------------------------------------------------------------------------


class TestTheLoopAssignsAnEpisodeUniqueCallId:
    """A provider that numbers its tool calls within a turn reuses an id across
    turns. The loop assigns the trial's episode-unique id at ingestion, so all
    four consumers — the executor (hence the runner's own record), the core
    recorder, the assistant message and the ``role: tool`` message — carry one
    unambiguous id per call rather than one id for two calls.
    """

    @staticmethod
    def _drive_reused_id(executor: ToolExecutor, recorder: TrialToolCallRecorder) -> list[Message]:
        """Two turns whose provider minted the same id, with different arguments
        so a mis-pairing shows up in the output rather than only in the id."""
        return _drive_loop(
            [
                [ToolCall(id="echo:0", name="echo", arguments={"payload": "first"})],
                [ToolCall(id="echo:0", name="echo", arguments={"payload": "second"})],
            ],
            executor,
            recorder,
        )

    def test_the_executor_is_called_with_two_distinct_ids(self) -> None:
        executor = _CallIdWatchingExecutor(_registry(_Echo()))

        self._drive_reused_id(executor, TrialToolCallRecorder())

        assert executor.call_ids == ["echo:0", "echo:0#2"]

    def test_the_recorder_holds_two_records_with_distinct_ids(self) -> None:
        recorder = TrialToolCallRecorder()

        self._drive_reused_id(ToolExecutor(_registry(_Echo())), recorder)

        assert [(call.call_id, call.output) for call in recorder.recorded] == [
            ("echo:0", "first"),
            ("echo:0#2", "second"),
        ]

    def test_each_tool_message_answers_the_assistant_entry_it_names(self) -> None:
        """The two sides of the conversation the id is echoed into must agree, or
        the provider is handed a transcript naming a call it cannot resolve."""
        messages = self._drive_reused_id(ToolExecutor(_registry(_Echo())), TrialToolCallRecorder())

        declared = [call.id for message in messages for call in (message.tool_calls or [])]
        answered = [
            message.tool_call_id for message in messages if message.tool_call_id is not None
        ]
        assert declared == ["echo:0", "echo:0#2"]
        assert answered == declared

    def test_a_provider_that_minted_unique_ids_records_exactly_those_ids(self) -> None:
        """The no-movement guarantee at the runtime end: every Anthropic / OpenAI
        trial records the provider's own ids, so no bundle changes shape."""
        executor = _CallIdWatchingExecutor(_registry(_Echo()))
        recorder = TrialToolCallRecorder()

        messages = _drive_loop(
            [
                [
                    ToolCall(id="toolu_01", name="echo", arguments={"payload": "a"}),
                    ToolCall(id="toolu_02", name="echo", arguments={"payload": "b"}),
                ],
                [ToolCall(id="toolu_03", name="echo", arguments={"payload": "c"})],
            ],
            executor,
            recorder,
        )

        expected = ["toolu_01", "toolu_02", "toolu_03"]
        assert executor.call_ids == expected
        assert [call.call_id for call in recorder.recorded] == expected
        assert [call.id for message in messages for call in (message.tool_calls or [])] == expected
        assert [
            message.tool_call_id for message in messages if message.tool_call_id is not None
        ] == expected


# ---------------------------------------------------------------------------
# One id sequence per trial, drawn from by both actors
# ---------------------------------------------------------------------------


class TestBothActorsDrawFromOneIdSequence:
    """The agent's loop and the user's turn are two ingestion points into one
    record, so an id sequence owned by the loop alone lets the second actor
    record a key the first already issued — and the record's ``call_id`` is what
    joins a call to the result it produced.
    """

    def test_two_actors_emitting_one_raw_id_record_two_distinct_keys(self) -> None:
        """Both actors' providers mint ``call_1``; the trial records ``call_1``
        and ``call_1#2``, and the timeline joins each call to its own result."""
        trajectory = _run_two_actor_trial(
            [
                _agent_turn(
                    "", ToolCall(id="call_1", name="agent_tool", arguments={"payload": "a"})
                ),
                _agent_turn("your turn"),
                _agent_turn("bye"),
            ],
            [
                GenerationResult(
                    text="over to you",
                    tool_calls=[
                        ToolCall(id="call_1", name="user_tool", arguments={"payload": "u"})
                    ],
                ),
                GenerationResult(text="###STOP###"),
            ],
            agent_registry=_registry(_Echo("agent_tool")),
            user_registry=_registry(_Echo("user_tool")),
        )

        assert [(call.call_id, call.executor) for call in trajectory.tool_log] == [
            ("call_1", ToolExecutorIdentity.AGENT),
            ("call_1#2", ToolExecutorIdentity.USER),
        ]

        timeline = build_trial_timeline(
            trajectory.messages, trajectory.tool_log, trajectory.termination_reason
        )
        results = [
            (event.call_id, event.tool_name, event.executor, event.result)
            for event in timeline.events
            if event.kind is TraceEventKind.TOOL_RESULT
        ]
        assert results == [
            ("call_1", "agent_tool", ToolExecutorIdentity.AGENT, "a"),
            ("call_1#2", "user_tool", ToolExecutorIdentity.USER, "u"),
        ]

    def test_an_opening_tool_call_executes_and_rides_the_first_user_message(self) -> None:
        """The simulator's opening reply is a user turn like any other: its calls
        run, record under ``executor=user`` at sequence 0, and reach the message
        the transcript rules read. The opening turn used to return its text
        alone, so the call was executed by nobody and declared by nothing."""
        trajectory = _run_two_actor_trial(
            [_agent_turn("bye")],
            [
                GenerationResult(
                    text="my meter reads low",
                    tool_calls=[
                        ToolCall(id="call_open", name="user_tool", arguments={"payload": "o"})
                    ],
                ),
                GenerationResult(text="###STOP###"),
            ],
            agent_registry=_registry(_Echo("agent_tool")),
            user_registry=_registry(_Echo("user_tool")),
            initial_user_message="",
        )

        assert [
            (call.sequence, call.call_id, call.tool_name, call.executor)
            for call in trajectory.tool_log
        ] == [(0, "call_open", "user_tool", ToolExecutorIdentity.USER)]

        opening = trajectory.messages[0]
        assert opening.role is MessageRole.USER
        assert [(call.id, call.name) for call in (opening.tool_calls or [])] == [
            ("call_open", "user_tool")
        ]
        assert opening.content == "my meter reads low\n\nuser_tool() result: o"


# ---------------------------------------------------------------------------
# The trial's metrics count the agent's tool use
# ---------------------------------------------------------------------------


class TestMetricsCountTheAgentsCalls:
    """``metrics.tool_calls`` and ``tool_success_rate`` describe the agent — the
    same scoping stuck detection and ``tool_expectations`` apply. The trajectory's
    ``tool_log`` keeps every executor's calls, so nothing is lost by the scoping.
    """

    @staticmethod
    def _user_reply() -> GenerationResult:
        """The user calls a tool that raises, so the two candidate sources
        disagree about the rate as well as the count: over every executor these
        trials are 2 of 3 and 0 of 1, over the agent's own 2 of 2 and none."""
        return GenerationResult(
            text="over to you",
            tool_calls=[ToolCall(id="toolu_U", name="boom", arguments={})],
        )

    def test_two_agent_calls_beside_one_user_call_report_two(self) -> None:
        trajectory = _run_two_actor_trial(
            [
                _agent_turn(
                    "", ToolCall(id="toolu_A", name="agent_tool", arguments={"payload": "a"})
                ),
                _agent_turn("your turn"),
                _agent_turn(
                    "", ToolCall(id="toolu_B", name="agent_tool", arguments={"payload": "b"})
                ),
                _agent_turn("bye"),
            ],
            [self._user_reply(), GenerationResult(text="###STOP###")],
            agent_registry=_registry(_Echo("agent_tool")),
            user_registry=_registry(_Boom()),
        )

        assert trajectory.metrics.tool_calls == 2
        assert trajectory.metrics.tool_success_rate == 1.0
        assert len(trajectory.tool_log) == 3

    def test_a_trial_whose_only_call_was_the_users_reports_no_agent_calls(self) -> None:
        """The agent's true count is zero, and computing a success rate over an
        empty agent slice would divide by zero rather than report it."""
        trajectory = _run_two_actor_trial(
            [_agent_turn("your turn"), _agent_turn("bye")],
            [self._user_reply(), GenerationResult(text="###STOP###")],
            agent_registry=_registry(_Echo("agent_tool")),
            user_registry=_registry(_Boom()),
        )

        assert trajectory.metrics.tool_calls == 0
        assert trajectory.metrics.tool_success_rate == 0.0
        assert [call.executor for call in trajectory.tool_log] == [ToolExecutorIdentity.USER]


# ---------------------------------------------------------------------------
# The docker path keeps the wire's fine-grained status
# ---------------------------------------------------------------------------


class TestTheWireStatusSurvivesTheClient:
    """``GrpcRunnerClient`` collapsed the proto status to a bool one line before
    it would have been recorded, so ``TIMEOUT`` was unrecordable on the
    production path even though the runner reported it."""

    class _Stub:
        def __init__(self, response) -> None:
            self._response = response

        def ExecuteTool(self, request):
            return self._response

    def _client_for(self, response) -> GrpcRunnerClient:
        client = GrpcRunnerClient.__new__(GrpcRunnerClient)
        client.stub = self._Stub(response)
        return client

    def test_a_timeout_response_becomes_a_timeout_status(self) -> None:
        result = self._client_for(
            pb2.ExecuteToolResponse(
                status=pb2.EXECUTION_STATUS_TIMEOUT,
                output="",
                error_message="Tool execution timed out after 30.0s",
            )
        ).execute_tool("t:0", "slow", {}, call_id="toolu_A")

        assert result.status is ToolExecutionStatus.TIMEOUT

    def test_a_success_response_becomes_a_success_status(self) -> None:
        result = self._client_for(
            pb2.ExecuteToolResponse(status=pb2.EXECUTION_STATUS_SUCCESS, output="2.0")
        ).execute_tool("t:0", "calculator", {}, call_id="toolu_A")

        assert result.status is ToolExecutionStatus.SUCCESS

    def test_a_trial_not_found_response_refuses_instead_of_returning_a_result(self) -> None:
        """The runner holds no registration, so the call reached no tool and has
        no outcome to record. A ``ToolResult`` here is a failure the agent reads
        as its own, so the client raises and names what was lost instead."""
        with pytest.raises(TrialNotRegisteredError) as raised:
            self._client_for(
                pb2.ExecuteToolResponse(
                    status=pb2.EXECUTION_STATUS_TRIAL_NOT_FOUND,
                    error_message="Trial 't:0' not found",
                )
            ).execute_tool("t:0", "calculator", {}, call_id="toolu_A")

        assert raised.value.trial_id == "t:0"
        assert raised.value.tool_name == "calculator"
        assert "t:0" in str(raised.value)

    def test_a_status_no_trial_records_refuses_rather_than_degrading_to_error(self) -> None:
        """The translation is total. ``UNSPECIFIED`` names no outcome, and the
        next status added to the proto will name none either until it is mapped
        — neither may arrive at a recorder as an ordinary tool failure."""
        with pytest.raises(ValueError, match="no recordable ToolExecutionStatus"):
            self._client_for(
                pb2.ExecuteToolResponse(status=pb2.EXECUTION_STATUS_UNSPECIFIED)
            ).execute_tool("t:0", "calculator", {}, call_id="toolu_A")


def test_messages_are_unaffected_by_recording() -> None:
    """Recording moved; what the transcript carries did not. The ``role: tool``
    message still holds the loop's own ``Error: `` prefixed text, which is not
    the text the record holds — the timeline resolves that by taking ``result``
    from the record."""
    message, record = _message_and_record(_Boom())

    assert message.content == "Error: kaboom"
    assert record.output == "kaboom"


def test_a_failure_with_no_message_of_its_own_states_the_shared_sentence() -> None:
    """A tool that fails without saying why is recorded as a sentence rather
    than as nothing. An empty text would reach the host as the gRPC client's own
    wording instead, so the agent and a matcher would read different failures."""
    message, record = _message_and_record(_Silent())

    assert record.output == "Tool returned failure with no error message"
    assert message.content == "Error: Tool returned failure with no error message"
