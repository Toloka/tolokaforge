"""The runner's post-conditions on what an agent loop returns.

Three obligations of the ``tolokaforge.agent_loops`` seam are stated in the
:class:`~tolokaforge.core.loop.AgentLoop` docstring and enforced by nothing at
write time. Each one broken by a third-party loop produces a plausible
trajectory carrying a wrong number rather than an error:

1. **Declared and recorded tool-call ids reconcile.** A record answering no
   declaration makes the trial ungradeable end to end, and grading is where it
   surfaces — long after the loop that caused it stopped being the subject.
2. **A denominator-excluding termination reason is earned by typed evidence.**
   The reasons in
   :data:`~tolokaforge.core.failure_attribution.EXCLUDED_TYPED_REASONS` remove
   a trial from the measured denominator *and* produce no grade, so a loop free
   to emit one from nothing can delete its own failures from the results.
3. **Assistant turns come with recorded model calls.** A loop that never feeds
   the metrics sink leaves ``cost_usd`` at zero however much it spent, so the
   run's budget cap can never fire.

Each case here drives a stub loop that breaks one obligation and a stub that
keeps it, through the real :class:`~tolokaforge.core.runner.TrialRunner`, and
reads the findings off the trial logger's own record.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Any

import pytest

from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import (
    AgentLoopContext,
    LoopOutcome,
    TerminationDecision,
    classify_loop_error,
)
from tolokaforge.core.models import (
    Message,
    MessageRole,
    ModelConfig,
    TerminationReason,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.plugin_registry import AGENT_LOOPS_GROUP, _clear_discovery_cache
from tolokaforge.core.runner import BUILT_IN_AGENT_LOOP, TrialRunner
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

pytestmark = pytest.mark.canonical


class _NeverCalledAgent:
    """Agent generate seam — the stub loops here never generate."""

    def __init__(self) -> None:
        self.capabilities = ModelCapabilities()
        self.config = ModelConfig(provider="openai", name="gpt-4")

    def generate(self, *args: Any, **kwargs: Any) -> GenerationResult:
        raise AssertionError("the stub loop, not the built-in loop, drives these trials")

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


@dataclass
class _ScriptedLoop:
    """A third-party loop shape whose whole behaviour is the case's script.

    ``declare`` / ``record`` name the ids the two views carry, so a case sets
    them equal to keep the reconciliation obligation and unequal to break it.
    ``record_generation`` decides whether the metrics sink is fed at all.
    """

    context: AgentLoopContext
    termination_reason: TerminationReason = TerminationReason.AGENT_DONE
    declare: str | None = None
    record: str | None = None
    tool_name: str = "read_file"
    record_generation: bool = False
    evidence: object | None = None
    append_assistant_turn: bool = True
    clear_messages: bool = False

    def run(self, system_prompt: str, messages: list[Message], start_time: float) -> LoopOutcome:
        if self.clear_messages:
            messages.clear()
        if self.record_generation:
            self.context.metrics.record_generation(
                GenerationResult(
                    text="",
                    usage=Usage(prompt_tokens=11, completion_tokens=3),
                    cost_usd=0.002,
                )
            )
        if self.append_assistant_turn:
            messages.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content="working on it",
                    tool_calls=(
                        [ToolCall(id=self.declare, name=self.tool_name, arguments={})]
                        if self.declare is not None
                        else None
                    ),
                )
            )
        if self.record is not None:
            self.context.recorder.record(
                call_id=self.record,
                tool_name=self.tool_name,
                arguments={},
                executor=ToolExecutorIdentity.AGENT,
                status=ToolExecutionStatus.SUCCESS,
                output="ok",
                latency_seconds=0.01,
            )
        return LoopOutcome(
            status=TrialStatus.COMPLETED,
            termination_reason=self.termination_reason,
            excluding_reason_evidence=self.evidence,
        )


class _EntryPointStub:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the discovery scan."""

    def __init__(self, name: str, value: Any) -> None:
        self.name = name
        self.value = value

        class _Dist:
            name = "tests-fixture"

        self.dist = _Dist()

    def load(self) -> Any:
        return self.value


_LOOP_NAME = "postcondition_stub_loop"


@pytest.fixture
def register_loop(monkeypatch: pytest.MonkeyPatch):
    """Register a scripted loop, optionally *as* the built-in registration.

    Registering under :data:`BUILT_IN_AGENT_LOOP` is how the built-in
    exemption is driven: the exemption is keyed on the name the runner
    resolved, so a stub answering to that name reaches exactly the branch a
    real ``engine-loop`` trial does.
    """

    def register(*, name: str = _LOOP_NAME, **script: Any) -> None:
        def factory(context: AgentLoopContext) -> _ScriptedLoop:
            return _ScriptedLoop(context=context, **script)

        real_entry_points = importlib.metadata.entry_points
        shipped = [ep for ep in real_entry_points(group=AGENT_LOOPS_GROUP) if ep.name != name]
        injected = _EntryPointStub(name, factory)

        def fake_entry_points(*, group: str) -> list[Any]:
            if group == AGENT_LOOPS_GROUP:
                return [*shipped, injected]
            return list(real_entry_points(group=group))

        monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
        _clear_discovery_cache()

    _clear_discovery_cache()
    yield register
    _clear_discovery_cache()


_TRIAL_COUNTER = iter(range(10_000))


def _drive(agent_loop: str = _LOOP_NAME) -> tuple[Trajectory, list[dict[str, Any]]]:
    """Run one trial and return its trajectory plus the ERROR records it logged.

    Each call takes a fresh ``task_id`` because :func:`get_logger` caches by
    name and the in-memory record list is per logger instance.
    """
    runner = TrialRunner(
        task_id=f"loop-postconditions-{next(_TRIAL_COUNTER)}",
        trial_index=0,
        agent_client=_NeverCalledAgent(),
        user_simulator=None,
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=5,
        episode_timeout_s=1200,
        interaction_mode="agent_only",
        agent_loop=agent_loop,
    )
    trajectory = runner.run("You are an agent.", "Do the task.")
    errors = [entry for entry in runner.logger.logs if entry["level"] == "ERROR"]
    return trajectory, errors


def _messages(errors: list[dict[str, Any]]) -> str:
    return " || ".join(entry["message"] for entry in errors)


class TestDeclaredAndRecordedCallIdsReconcile:
    """The two views of one trial's tool calls must describe the same calls."""

    def test_a_record_answering_no_declaration_is_named(self, register_loop) -> None:
        register_loop(declare="call-1", record="call-2", record_generation=True)

        trajectory, errors = _drive()

        assert len(errors) == 1, _messages(errors)
        finding = errors[0]
        assert "do not" in finding["message"] and "reconcile" in finding["message"]
        assert finding["context"]["agent_loop"] == _LOOP_NAME
        assert finding["context"]["declared_tool_calls"] == 1
        assert finding["context"]["recorded_tool_calls"] == 1
        detail = finding["context"]["detail"]
        assert "call-2" in detail, "the finding must name the unlinkable record"
        assert trajectory.status is TrialStatus.COMPLETED, (
            "a trial whose agent work is finished is kept and classified by "
            "failure attribution, not discarded by this check"
        )

    def test_agreeing_views_are_silent(self, register_loop) -> None:
        register_loop(declare="call-1", record="call-1", record_generation=True)

        _, errors = _drive()

        assert errors == [], _messages(errors)

    def test_records_with_no_conversation_turn_say_the_check_could_not_run(
        self, register_loop
    ) -> None:
        """The reconciliation's own blind spot is reported, never passed over.

        ``build_trial_timeline`` reconciles the two views only when the message
        view carries a turn; records alone build a records-only timeline that
        nothing cross-checks. A loop owns the ``messages`` list it is handed and
        may rewrite it, so "records but no turn to reconcile them against" is
        reachable — and it is answered here rather than passed over.
        """
        register_loop(record="call-1", append_assistant_turn=False, clear_messages=True)

        _, errors = _drive()

        assert len(errors) == 1, _messages(errors)
        assert "could not run" in errors[0]["message"]
        assert errors[0]["context"]["recorded_tool_calls"] == 1


class TestExclusionIsEarnedByTypedEvidence:
    """A reason that deletes a trial from the denominator needs evidence."""

    @pytest.mark.parametrize(
        "reason",
        [
            TerminationReason.RATE_LIMIT,
            TerminationReason.API_TIMEOUT,
            TerminationReason.EMPTY_COMPLETION,
            TerminationReason.PROVISION_ERROR,
        ],
    )
    def test_an_unevidenced_excluding_reason_is_downgraded(
        self, register_loop, reason: TerminationReason
    ) -> None:
        register_loop(termination_reason=reason, record_generation=True)

        trajectory, errors = _drive()

        assert trajectory.termination_reason is TerminationReason.ERROR, (
            f"{reason.value} from a third-party loop with no typed evidence must "
            "not remove the trial from the measured denominator"
        )
        assert len(errors) == 1, _messages(errors)
        assert errors[0]["context"]["claimed_termination_reason"] == reason.value
        assert errors[0]["context"]["counted_as"] == TerminationReason.ERROR.value

    def test_typed_evidence_keeps_the_exclusion(self, register_loop) -> None:
        register_loop(
            termination_reason=TerminationReason.RATE_LIMIT,
            evidence=RuntimeError("429 from the provider"),
            record_generation=True,
        )

        trajectory, errors = _drive()

        assert trajectory.termination_reason is TerminationReason.RATE_LIMIT
        assert errors == [], _messages(errors)

    def test_a_counted_reason_is_untouched(self, register_loop) -> None:
        register_loop(termination_reason=TerminationReason.MAX_TURNS, record_generation=True)

        trajectory, errors = _drive()

        assert trajectory.termination_reason is TerminationReason.MAX_TURNS
        assert errors == [], _messages(errors)

    def test_the_built_in_loop_is_exempt(self, register_loop) -> None:
        """The built-in loop reaches these reasons only through its classifier.

        Its excluding reasons are produced from an exception type inside
        ``classify_loop_error``, so the evidence is the classifier's own input
        and the outcome carries none. Downgrading them would change the
        behaviour of every real run.
        """
        register_loop(
            name=BUILT_IN_AGENT_LOOP,
            termination_reason=TerminationReason.RATE_LIMIT,
            record_generation=True,
        )

        trajectory, errors = _drive(agent_loop=BUILT_IN_AGENT_LOOP)

        assert trajectory.termination_reason is TerminationReason.RATE_LIMIT
        assert errors == [], _messages(errors)


class TestMetricsSinkLiveness:
    """A loop that generates without reporting it leaves the run uncapped."""

    def test_assistant_turns_with_no_recorded_call_are_named(self, register_loop) -> None:
        register_loop(record_generation=False)

        trajectory, errors = _drive()

        assert len(errors) == 1, _messages(errors)
        finding = errors[0]
        assert finding["context"]["assistant_turns"] == 1
        assert finding["context"]["api_calls"] == 0
        assert trajectory.metrics.cost_usd is None, (
            "the condition the finding describes: nothing priced this trial, so "
            "it charges nothing against the run's budget cap"
        )

    def test_a_fed_sink_is_silent(self, register_loop) -> None:
        register_loop(record_generation=True)

        trajectory, errors = _drive()

        assert errors == [], _messages(errors)
        assert trajectory.metrics.api_calls == 1

    def test_a_loop_that_produced_no_assistant_turn_is_silent(self, register_loop) -> None:
        """No turns means no generation to have gone unreported."""
        register_loop(record_generation=False, append_assistant_turn=False)

        _, errors = _drive()

        assert errors == [], _messages(errors)
