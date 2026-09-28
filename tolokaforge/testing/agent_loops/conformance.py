"""The conformance suite every :class:`~tolokaforge.core.loop.AgentLoop` runs against itself.

:class:`~tolokaforge.core.loop.AgentLoop` states its obligations in prose, and
every one of them is enforced downstream in grading rather than by the type
checker. A loop that breaks one still returns a
:class:`~tolokaforge.core.loop.LoopOutcome`, still fills a trajectory, and still
gets graded — so the failure shows up as a **wrong number**, not an exception.
Five of the nine vectors this suite covers move the number *up*.

Each test here is therefore behavioural: it drives a scripted episode through
the factory under test and reads the artifacts grading reads — the caller's
``messages``, the trial's tool-call record, and the
:class:`~tolokaforge.core.grading.trace_timeline.TrialTimeline` built from both.
Nothing is asserted by inspecting the implementation.

Adoption is the repo's standard suite shape — subclass and supply one fixture::

    from tolokaforge.testing.agent_loops import AgentLoopConformanceSuite

    class TestMyLoopConformance(AgentLoopConformanceSuite):
        @pytest.fixture
        def loop_factory(self):
            return my_agent_loop_factory

The base class carries no ``Test`` prefix so pytest does not collect it.

**The one obligation this suite states but cannot prove: termination honesty.**
A reason in
:data:`~tolokaforge.core.failure_attribution.EXCLUDED_TYPED_REASONS` removes the
trial from the measured denominator, so it may be emitted only on *typed*
evidence — an exception type, an HTTP status, or a typed empty-completion
observation. The suite pins the reachable half of that rule: a loop must not
report an excluded reason on an episode where nothing typed went wrong, it must
route a raised exception through ``context.classify_error`` rather than naming a
reason itself, and where the classifier does spend an exclusion the loop must
carry the evidence it was handed onto the outcome — the same pairing the caller
reads before it decides whether the trial leaves the denominator. What it cannot
check is a loop that reaches its *own* provider, matches prose against an
exception message, and calls the result a rate limit. That one is on the
implementer.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tolokaforge.core.failure_attribution import EXCLUDED_TYPED_REASONS
from tolokaforge.core.grading.trace_timeline import (
    TraceEvent,
    TraceEventKind,
    build_trial_timeline,
)
from tolokaforge.core.llm.client import LLMApiTimeoutError
from tolokaforge.core.loop import AgentLoop, AgentLoopFactory, LoopConfig, LoopOutcome
from tolokaforge.core.models import Message, MessageRole, TerminationReason
from tolokaforge.core.tool_message_format import TOOL_ERROR_MESSAGE_PREFIX
from tolokaforge.testing.agent_loops.harness import (
    EpisodeHarness,
    ScriptedUserTurn,
    assistant_turn,
    tool_call,
    tool_output_for,
)

__all__ = [
    "AgentLoopConformanceSuite",
    "EpisodeResult",
    "run_episode",
    "tool_calls_by_id",
    "tool_results_by_id",
]

_SYSTEM_PROMPT = "You are the agent under conformance test."
_FIRST_USER_MESSAGE = "Do the scripted task."


class EpisodeResult:
    """One conformance episode, and the two views grading joins."""

    def __init__(self, harness: EpisodeHarness, messages: list[Message], outcome: LoopOutcome):
        self.harness = harness
        self.messages = messages
        self.outcome = outcome

    @property
    def declared_calls(self) -> tuple[Any, ...]:
        return EpisodeHarness.declared_calls(self.messages)

    @property
    def recorded(self) -> tuple[Any, ...]:
        return self.harness.recorder.recorded

    def timeline_events(self, *, with_records: bool = True) -> tuple[TraceEvent, ...]:
        """The timeline grading builds, optionally from the message view alone.

        ``with_records=False`` is the bundle re-grade shape: a trial whose
        ``tool_log.yaml`` sidecar is absent recovers every tool result from the
        ``role: tool`` messages instead.
        """
        records = self.recorded if with_records else ()
        return build_trial_timeline(self.messages, records, self.outcome.termination_reason).events


def run_episode(
    factory: AgentLoopFactory, harness: EpisodeHarness, *, start_time: float | None = None
) -> EpisodeResult:
    """Build the loop from *harness* and drive one episode through it."""
    loop = factory(harness.context())
    messages = [Message(role=MessageRole.USER, content=_FIRST_USER_MESSAGE)]
    outcome = loop.run(
        _SYSTEM_PROMPT, messages, start_time if start_time is not None else time.time()
    )
    return EpisodeResult(harness, messages, outcome)


def _two_tools_in_one_turn() -> EpisodeHarness:
    """One turn calling two different tools, then a tool-call-free closing turn."""
    return EpisodeHarness.build(
        [
            assistant_turn(
                text="looking two things up",
                tool_calls=[
                    tool_call("search", {"q": "alpha"}, "call_1"),
                    tool_call("fetch", {"url": "https://example.test/beta"}, "call_2"),
                ],
            ),
            assistant_turn(text="done"),
        ]
    )


def _same_tool_twice_in_one_turn() -> EpisodeHarness:
    """One turn calling ONE tool twice, under a raw id the provider repeats.

    ``lookup:0`` twice is the ``<tool>:<index within the turn>`` shape some
    providers emit. Only ``context.call_ids`` makes the two calls separable, and
    because both name one tool, the timeline's tool-name corroboration cannot
    notice when they are not.
    """
    return EpisodeHarness.build(
        [
            assistant_turn(
                text="checking both",
                tool_calls=[
                    tool_call("lookup", {"q": "alpha"}, "lookup:0"),
                    tool_call("lookup", {"q": "beta"}, "lookup:0"),
                ],
            ),
            assistant_turn(text="done"),
        ]
    )


def _one_failing_call() -> EpisodeHarness:
    """Two calls in one turn; the first one fails."""
    return EpisodeHarness.build(
        [
            assistant_turn(
                text="trying",
                tool_calls=[
                    tool_call("write", {"path": "/tmp/x"}, "call_1"),
                    tool_call("read", {"path": "/tmp/x"}, "call_2"),
                ],
            ),
            assistant_turn(text="done"),
        ],
        fail_call_indices=[0],
    )


def tool_results_by_id(events: tuple[TraceEvent, ...]) -> dict[str, TraceEvent]:
    """The timeline's ``TOOL_RESULT`` events, keyed by the call each answers."""
    return {
        event.call_id: event
        for event in events
        if event.kind is TraceEventKind.TOOL_RESULT and event.call_id
    }


def tool_calls_by_id(events: tuple[TraceEvent, ...]) -> dict[str, TraceEvent]:
    """The timeline's ``TOOL_CALL`` events, keyed by their episode-unique id."""
    return {
        event.call_id: event
        for event in events
        if event.kind is TraceEventKind.TOOL_CALL and event.call_id
    }


class AgentLoopConformanceSuite:
    """Subclass and override ``loop_factory`` to certify one loop implementation."""

    @pytest.fixture
    def loop_factory(self) -> AgentLoopFactory:
        raise NotImplementedError(
            "subclasses of AgentLoopConformanceSuite must override the "
            "`loop_factory` fixture to return an AgentLoopFactory — the same "
            "callable the `tolokaforge.agent_loops` entry point resolves to"
        )

    # -- 0. the seam itself ------------------------------------------------

    def test_factory_builds_an_agent_loop(self, loop_factory: AgentLoopFactory) -> None:
        """The factory's product satisfies the runtime-checkable Protocol."""
        loop = loop_factory(_two_tools_in_one_turn().context())
        assert isinstance(loop, AgentLoop), (
            f"{type(loop).__name__} does not satisfy the AgentLoop Protocol; the "
            "runner resolves the factory and calls `run` on whatever it returns"
        )

    def test_run_mutates_the_callers_message_list_in_place(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """The caller owns ``messages``; the loop appends to that object.

        A loop that builds its own list and returns nothing but a verdict leaves
        the trajectory holding only the first user message — a trial graded as
        if the agent never spoke.
        """
        episode = run_episode(loop_factory, _two_tools_in_one_turn())

        assert len(episode.messages) > 1, (
            "the loop appended nothing to the caller's list; the trajectory the "
            "caller assembles from it would carry no agent turn at all"
        )
        assert isinstance(episode.outcome, LoopOutcome)

    # -- 1. every recorded call answers a declared one ---------------------

    def test_every_recorded_call_id_is_declared_and_the_timeline_builds(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """The id join holds, so grading can build the trial's one timeline.

        ``call_id`` is the only key joining a call to the result it produced.
        A recorded id no assistant message declares makes
        ``build_trial_timeline`` raise at grade time, which is a refusal rather
        than a wrong number — but only because the ids are episode-unique. A
        loop that keys calls by a raw provider id the provider repeats produces
        collisions instead, and those join silently.
        """
        episode = run_episode(loop_factory, _same_tool_twice_in_one_turn())

        declared_ids = [call.id for call in episode.declared_calls]
        recorded_ids = [record.call_id for record in episode.recorded]
        assert recorded_ids, "the loop executed tool calls but recorded none of them"

        assert len(set(declared_ids)) == len(declared_ids), (
            "two tool calls in one episode were declared under the same id: "
            f"{declared_ids}. The provider repeats raw ids within an episode; "
            "`context.call_ids.assign(...)` is what makes each declaration "
            "separable, and without it the two calls' results are "
            "interchangeable to every consumer"
        )
        assert len(set(recorded_ids)) == len(
            recorded_ids
        ), f"the trial's record holds duplicate call ids: {recorded_ids}"
        assert set(recorded_ids) <= set(declared_ids), (
            "the trial's two views of itself disagree — recorded ids "
            f"{sorted(set(recorded_ids) - set(declared_ids))} answer no declared "
            "tool call. Every call must be keyed by "
            "`context.call_ids.assign(<provider id>)` in all three places: the "
            "assistant message's ToolCall.id, `recorder.record(call_id=...)` "
            "and `tool_executor.execute(call_id=...)`"
        )

        # Raises TimelineInconsistencyError rather than failing an assertion —
        # that is the grade-time symptom this obligation exists to prevent.
        episode.timeline_events()

    # -- 2. same tool, twice, different arguments --------------------------

    def test_two_calls_to_one_tool_keep_their_own_results(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """The single most valuable assertion in the kit.

        Two calls to the SAME tool with different arguments are the one case the
        timeline's own corroboration cannot catch: it compares the tool *name*
        on each view, and both views name one tool. A mis-join here produces a
        complete, plausible trajectory in which each call carries the other's
        result — and a ``result:`` trace check reads it without complaint.

        The episode drives it through the whole path: declaration → execution →
        record → timeline, with the executor's output a pure function of the
        arguments it was handed, so a swapped result is visible as text.
        """
        episode = run_episode(loop_factory, _same_tool_twice_in_one_turn())

        declared = episode.declared_calls
        assert (
            len(declared) == 2
        ), f"the episode scripted two calls to one tool; the message view declares {len(declared)}"
        assert {tuple(sorted(call.arguments.items())) for call in declared} == {
            (("q", "alpha"),),
            (("q", "beta"),),
        }, "the loop altered the scripted arguments before declaring the calls"

        by_id = {call.id: call for call in declared}
        tool_messages = EpisodeHarness.tool_messages_by_call_id(episode.messages)

        for record in episode.recorded:
            declaration = by_id[record.call_id]
            assert record.arguments == declaration.arguments, (
                f"call {record.call_id!r} was recorded with arguments "
                f"{record.arguments!r} but declared with {declaration.arguments!r} "
                "— the record and the message view describe different calls"
            )
            expected = tool_output_for(declaration.name, declaration.arguments)
            assert record.output == expected, (
                f"call {record.call_id!r} declared {declaration.arguments!r} but "
                f"recorded the output of a different call: {record.output!r}"
            )
            message = tool_messages.get(record.call_id)
            assert message is not None, (
                f"no `role: tool` message answers call {record.call_id!r}; a trial "
                "re-graded from its messages alone would have no result for it"
            )
            assert expected in message.content, (
                f"the `role: tool` message for call {record.call_id!r} carries "
                f"{message.content!r}, which is not the result of the arguments "
                f"that call declared ({declaration.arguments!r})"
            )

        events = episode.timeline_events()
        calls = tool_calls_by_id(events)
        results = tool_results_by_id(events)
        assert set(calls) == set(results), (
            "the timeline pairs every call with a result; unpaired keys: "
            f"{sorted(set(calls) ^ set(results))}"
        )
        for call_id, call_event in calls.items():
            assert results[call_id].result == tool_output_for(
                call_event.tool_name or "", call_event.arguments or {}
            ), (
                f"the timeline attributes {results[call_id].result!r} to the call "
                f"that asked for {call_event.arguments!r}. Two calls to one tool "
                "were joined to each other's results — silently, because both "
                "views name the same tool"
            )

    # -- 3. a failed call says so in the message view ----------------------

    def test_a_failed_calls_tool_message_carries_the_error_prefix(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """Without the prefix, a failed call reads as a success at grade time.

        The message view records no status. The prefix is the only mark a failed
        call carries there, and the grading path recovers the tool's own text by
        stripping exactly it — so a differently-worded prefix also breaks the
        round trip, and a ``result:`` trace check then evaluates one text on a
        bundle with a tool-call record and a different one on the same bundle
        without.
        """
        harness = _one_failing_call()
        episode = run_episode(loop_factory, harness)

        failed_ids = harness.executor.failed_call_ids()
        assert failed_ids, "the scripted failing call never reached the executor"
        tool_messages = EpisodeHarness.tool_messages_by_call_id(episode.messages)

        for call_id in failed_ids:
            message = tool_messages.get(call_id)
            assert message is not None, f"no `role: tool` message answers failed call {call_id!r}"
            assert message.content.startswith(TOOL_ERROR_MESSAGE_PREFIX), (
                f"the `role: tool` message for the FAILED call {call_id!r} reads "
                f"{message.content!r}. It must start with "
                f"{TOOL_ERROR_MESSAGE_PREFIX!r} "
                "(tolokaforge.core.tool_message_format) — that prefix is the only "
                "evidence in the message view that the call failed at all"
            )

        for call_id, message in tool_messages.items():
            if call_id in failed_ids:
                continue
            assert not message.content.startswith(TOOL_ERROR_MESSAGE_PREFIX), (
                f"the `role: tool` message for the SUCCESSFUL call {call_id!r} "
                "starts with the failure prefix, so grading reads a success as a "
                "failure"
            )

        with_records = tool_results_by_id(episode.timeline_events())
        without_records = tool_results_by_id(episode.timeline_events(with_records=False))
        assert set(with_records) == set(without_records)
        for call_id, event in with_records.items():
            assert event.result == without_records[call_id].result, (
                f"call {call_id!r} reads as {event.result!r} from the tool-call "
                f"record and {without_records[call_id].result!r} from the message "
                "view alone. The same bundle would grade differently with and "
                "without its `tool_log.yaml` sidecar"
            )

    # -- 4. the metrics sink is fed --------------------------------------

    def test_every_generation_reaches_the_metrics_sink(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """The run's spend is the sum of the trials'; an unfed sink reports zero.

        A loop that never calls ``record_generation`` leaves every trial at
        ``cost_usd == 0``, so ``compute.max_budget_usd`` never fires however much
        the run actually spends.
        """
        harness = _two_tools_in_one_turn()
        episode = run_episode(loop_factory, harness)

        assert harness.llm.call_count > 0, "the loop never asked the client to generate"
        assert len(harness.metrics.generations) == harness.llm.call_count, (
            f"the client served {harness.llm.call_count} generations but "
            f"`metrics.record_generation` was called "
            f"{len(harness.metrics.generations)} times. Every generation's usage "
            "and cost must reach the sink"
        )
        assert harness.metrics.cost_usd > 0.0, (
            "the trial's accumulated cost is zero on an episode whose every "
            "generation was priced — the run's budget cap cannot fire off this"
        )
        assert harness.metrics.tool_calls == len(
            harness.executor.executions
        ), "`metrics.record_tool_call` was not called once per executed tool call"
        assert episode.outcome is not None

    # -- 5. the termination policy runs on every assistant turn ------------

    def test_should_terminate_runs_after_the_append_and_before_the_tools(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """Stuck detection is this call; skipping it disables it silently.

        Both halves of the placement are load-bearing. A policy called *before*
        the assistant message is appended cannot read the turn it is judging; a
        policy called *after* the tools ran has already let a stuck agent act.
        """
        harness = _two_tools_in_one_turn()
        run_episode(loop_factory, harness)

        calls = harness.should_terminate.calls
        assistant_turns = harness.llm.call_count
        assert len(calls) == assistant_turns, (
            f"the loop produced {assistant_turns} assistant turns but consulted "
            f"`should_terminate` {len(calls)} times. It is called once per turn, "
            "and skipping it turns off the trial's stuck detection with nothing "
            "in the output to say so"
        )

        executed_before = 0
        for index, call in enumerate(calls):
            assert call.last_role is MessageRole.ASSISTANT, (
                f"`should_terminate` call {index} saw {call.last_role} as the last "
                "message. It runs after the turn's assistant message is appended, "
                "so the policy can read the turn it is judging"
            )
            assert call.last_declared_call_ids == call.result_call_ids, (
                f"`should_terminate` call {index} saw an appended message whose "
                f"declared ids {call.last_declared_call_ids} are not the ids of "
                f"the result it was handed {call.result_call_ids}"
            )
            assert call.executions_before == executed_before, (
                f"`should_terminate` call {index} ran with "
                f"{call.executions_before} tool calls already executed; "
                f"{executed_before} had run before this turn. The policy must be "
                "consulted BEFORE the turn's tools execute, or a stuck agent acts "
                "one more time than the policy allowed"
            )
            executed_before += len(call.result_call_ids)

        assert (
            len(harness.executor.executions) == executed_before
        ), "the loop did not execute the tool calls it declared"

    # -- 6. the user turn runs when one is supplied ------------------------

    def test_the_user_turn_runs_when_a_turn_produced_no_tool_calls(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """A supplied user turn is an obligation, not an offer.

        ``user_turn`` is ``None`` only when the trial's turn policy dispatches no
        user — the caller decides that. When one is supplied and the agent's turn
        asked for no tool, a loop that ignores it runs a conversational task
        agent-only and reports nothing unusual.
        """
        user_turn = ScriptedUserTurn(["and now the second half, please"])
        harness = EpisodeHarness.build(
            [
                assistant_turn(text="first half done"),
                assistant_turn(text="second half done"),
            ],
            user_turn=user_turn,
            decide=None,
        )
        episode = run_episode(loop_factory, harness)

        assert user_turn.call_count >= 1, (
            "an assistant turn produced no tool calls and a `user_turn` was "
            "supplied, but the loop never called it. The trial's conversational "
            "half never happens and nothing in the output says so"
        )
        assert any(
            message.role is MessageRole.USER
            and message.content == "and now the second half, please"
            for message in episode.messages
        ), "the user turn's reply was never appended to the caller's message list"
        assert (
            harness.llm.call_count >= 2
        ), "the loop stopped after the user turn replied instead of taking the agent's next turn"

    # -- 7. the two budgets are the loop's own to enforce ------------------

    def test_max_turns_bounds_the_episode(self, loop_factory: AgentLoopFactory) -> None:
        """Nothing outside the loop counts turns on this path."""
        harness = EpisodeHarness.build([], config=LoopConfig(max_turns=3), decide=None)
        episode = run_episode(loop_factory, harness)

        assert harness.llm.call_count >= 1, "the loop never ran a turn at all"
        assert harness.llm.call_count <= 3, (
            f"`config.max_turns` is 3 and the loop took {harness.llm.call_count} "
            "turns. Nothing outside the loop enforces the turn bound, so a trial "
            "runs until its wall-clock budget or its cost budget stops it"
        )
        assert sum(1 for message in episode.messages if message.role is MessageRole.ASSISTANT) <= 3
        assert episode.outcome.termination_reason is not None, (
            "a loop that ran out of turns must name a termination reason; None "
            "reads downstream as a trial that ended for no stated cause"
        )

    def test_a_spent_episode_budget_stops_the_loop_before_it_generates(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """``episode_timeout_s`` is measured against the ``start_time`` given to ``run``.

        The episode below begins already over budget. A loop that does not check
        before its first generation spends a full episode's tokens on a trial
        whose time was gone before it started.
        """
        harness = EpisodeHarness.build(
            [], config=LoopConfig(max_turns=20, episode_timeout_s=30), decide=None
        )
        episode = run_episode(loop_factory, harness, start_time=time.time() - 120.0)

        assert harness.llm.call_count == 0, (
            "the episode's wall-time budget was already spent when `run` was "
            f"called, and the loop generated {harness.llm.call_count} times "
            "anyway. `start_time` is the epoch the episode began at and "
            "`config.episode_timeout_s` is the budget measured against it; "
            "nothing outside the loop enforces either"
        )
        assert episode.outcome.termination_reason is not None

    # -- 8. termination honesty -------------------------------------------

    def test_a_clean_episode_does_not_claim_an_excluded_reason(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """A denominator-excluding reason needs typed evidence, and there is none here.

        Membership of ``EXCLUDED_TYPED_REASONS`` removes the trial from the
        measured denominator. Excluding a trial the agent actually failed
        inflates every benchmark number with nothing in the output to show it.
        """
        for label, harness in (
            ("a scripted episode that ran to its own end", _two_tools_in_one_turn()),
            (
                "an episode that exhausted its turn budget",
                EpisodeHarness.build([], config=LoopConfig(max_turns=2), decide=None),
            ),
        ):
            episode = run_episode(loop_factory, harness)
            assert episode.outcome.termination_reason not in EXCLUDED_TYPED_REASONS, (
                f"{label} reported "
                f"{episode.outcome.termination_reason!r}, which excludes the trial "
                "from the measured denominator. Nothing typed went wrong: no "
                "provider exception, no HTTP status, no empty completion"
            )

    def test_a_raised_exception_is_classified_by_the_context(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """``classify_error`` decides the reason; the loop does not name one itself.

        A loop that labels any raised exception an API timeout or a rate limit
        earns exclusion for every trial its own bugs kill. The classifier is the
        one place that decision lives, and it spends exclusion only on a typed
        exception.
        """
        boom = RuntimeError("the loop's own defect, not the provider's")
        harness = EpisodeHarness.build([], raise_at_call={0: boom})
        episode = run_episode(loop_factory, harness)

        expected = harness.context().classify_error(boom)
        assert episode.outcome.termination_reason is expected.reason, (
            f"an untyped exception terminated the trial as "
            f"{episode.outcome.termination_reason!r}; `context.classify_error` "
            f"classifies it as {expected.reason!r}"
        )
        assert episode.outcome.termination_reason not in EXCLUDED_TYPED_REASONS, (
            "an exception with no typed provider evidence behind it must leave "
            "the trial in the measured denominator"
        )

    def test_a_typed_provider_timeout_earns_its_excluded_reason(
        self, loop_factory: AgentLoopFactory
    ) -> None:
        """The positive half of the same rule: typed evidence does earn exclusion.

        Both halves of the outcome are checked, because the caller reads both.
        ``TrialRunner`` keeps an excluding reason only where
        :attr:`~tolokaforge.core.loop.LoopOutcome.excluding_reason_evidence`
        shows what earned it, so a loop that reports the classifier's reason and
        drops the evidence it came with is downgraded in production while
        passing an assertion written over the reason alone.
        """
        timeout = LLMApiTimeoutError("the provider did not answer in time")
        harness = EpisodeHarness.build([], raise_at_call={0: timeout})
        episode = run_episode(loop_factory, harness)

        assert episode.outcome.termination_reason is TerminationReason.API_TIMEOUT, (
            "a typed provider timeout must reach the trial as "
            f"{TerminationReason.API_TIMEOUT!r}; the loop reported "
            f"{episode.outcome.termination_reason!r}. Counting a trial the "
            "provider killed deflates the run's numbers"
        )
        expected = harness.context().classify_error(timeout)
        assert episode.outcome.excluding_reason_evidence == expected.excluding_reason_evidence, (
            "the loop reported "
            f"{TerminationReason.API_TIMEOUT!r} with "
            f"excluding_reason_evidence={episode.outcome.excluding_reason_evidence!r}; "
            "`context.classify_error` handed it "
            f"{expected.excluding_reason_evidence!r}. Carry the decision's evidence "
            "onto the outcome beside the reason taken from the same decision — "
            "without it the caller counts the trial as a harness defect"
        )
