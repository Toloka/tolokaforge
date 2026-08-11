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
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
)
from tolokaforge.core.runner import TrialRunner, TrialToolCallRecorder
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry, ToolResult
from tolokaforge.tools.user_tools import UserToolExecutor

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

    def execute(self, tool_name: str, arguments: dict[str, Any], *, call_id: str) -> ToolResult:
        self.call_ids.append(call_id)
        return super().execute(tool_name, arguments, call_id=call_id)


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
        other. Redacting an *artifact* is tracked separately (#694); redacting
        the grader's input is a correctness bug."""
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
        destroyed by construction, and order looked right only because no run
        has a live user executor (#688). The user branch is therefore driven
        directly on :class:`TrialRunner`, which is the seam #688's fix will make
        reachable, not a mock of it.
        """
        agent_registry = _registry(_Echo("agent_tool"))
        user_executor = UserToolExecutor(env_state=None, use_default_tools=False)
        user_executor.register_tool(_Echo("user_tool"))

        user_simulator = MagicMock()
        user_simulator.last_system_prompt = None
        user_simulator.reply.side_effect = [
            GenerationResult(
                text="over to you",
                tool_calls=[ToolCall(id="toolu_U", name="user_tool", arguments={"payload": "u"})],
            ),
            GenerationResult(text="###STOP###"),
        ]

        runner = TrialRunner(
            task_id="interleaving",
            trial_index=0,
            agent_client=_ScriptedClient(
                [
                    GenerationResult(
                        text="",
                        tool_calls=[
                            ToolCall(id="toolu_A", name="agent_tool", arguments={"payload": "a"})
                        ],
                        usage=Usage(prompt_tokens=1),
                    ),
                    GenerationResult(text="your turn", tool_calls=[], usage=Usage(prompt_tokens=1)),
                    GenerationResult(
                        text="",
                        tool_calls=[
                            ToolCall(id="toolu_B", name="agent_tool", arguments={"payload": "b"})
                        ],
                        usage=Usage(prompt_tokens=1),
                    ),
                    GenerationResult(text="over to you", usage=Usage(prompt_tokens=1)),
                ]
            ),
            user_simulator=user_simulator,
            tool_executor=ToolExecutor(agent_registry),
            tool_schemas=[],
            user_tool_executor=user_executor,
            max_turns=6,
        )

        trajectory = runner.run("sys", initial_user_message="go")

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

    def test_a_trial_not_found_response_carries_no_recordable_status(self) -> None:
        """There is no tool outcome to name, so the field stays ``None`` and the
        recorder resolves ``ERROR`` — the truth, stated less specifically."""
        result = self._client_for(
            pb2.ExecuteToolResponse(
                status=pb2.EXECUTION_STATUS_TRIAL_NOT_FOUND,
                error_message="Trial 't:0' not found",
            )
        ).execute_tool("t:0", "calculator", {}, call_id="toolu_A")

        assert result.status is None


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
