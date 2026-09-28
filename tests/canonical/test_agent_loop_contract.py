"""Pin the ``AgentLoop`` seam: the Protocol surface, and the conformance kit's teeth.

Three things are locked here.

**The built-in loop conforms.** ``engine-loop`` resolves through the
``tolokaforge.agent_loops`` registry and runs the whole
:class:`~tolokaforge.testing.agent_loops.AgentLoopConformanceSuite`, so the kit
is proven by the implementation it describes rather than by its own fixture.

**The reference fixture conforms.** :class:`InMemoryAgentLoop` is the worked
example an external implementer copies; a reference that does not pass the suite
teaches the wrong loop.

**The suite has teeth.** Every obligation is enforced downstream in grading, so
a loop that breaks one returns a plausible trajectory and a wrong grade rather
than raising. A conformance assertion that nothing can fail proves nothing, so
each :class:`LoopDefects` knob switches off exactly one obligation and the
assertion written for it must fail on that loop.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from tolokaforge.core.loop import (
    AgentLoop,
    AgentLoopContext,
    LoopOutcome,
    ToolCallingLoop,
)
from tolokaforge.core.models import Message, TerminationReason
from tolokaforge.core.plugin_registry import load_agent_loop
from tolokaforge.testing.agent_loops import (
    AgentLoopConformanceSuite,
    InMemoryAgentLoop,
    LoopDefects,
    in_memory_agent_loop_factory,
)

pytestmark = pytest.mark.canonical


class TestProtocolSurface:
    """``AgentLoop`` is ``@runtime_checkable`` and both shipped loops satisfy it."""

    def test_the_builtin_loop_satisfies_the_protocol(self) -> None:
        loop = load_agent_loop("engine-loop")(_bare_context())
        assert isinstance(loop, ToolCallingLoop)
        assert isinstance(loop, AgentLoop)

    def test_the_in_memory_fixture_satisfies_the_protocol(self) -> None:
        loop = in_memory_agent_loop_factory(_bare_context())
        assert isinstance(loop, InMemoryAgentLoop)
        assert isinstance(loop, AgentLoop)

    def test_an_object_without_run_does_not_satisfy_the_protocol(self) -> None:
        class _NotALoop:
            pass

        assert not isinstance(_NotALoop(), AgentLoop)

    @pytest.mark.parametrize(
        "surface",
        [AgentLoop.run, ToolCallingLoop.run, InMemoryAgentLoop.run],
        ids=["protocol", "builtin", "in_memory"],
    )
    def test_run_takes_the_three_declared_positionals(self, surface: Any) -> None:
        """``system_prompt``, ``messages``, ``start_time`` — the caller passes all three
        positionally, so a renamed or reordered parameter is a silent mis-drive."""
        parameters = [name for name in inspect.signature(surface).parameters if name != "self"]
        assert parameters == ["system_prompt", "messages", "start_time"]


def _bare_context() -> AgentLoopContext:
    """The minimum context a factory needs to build a loop, never to run one."""
    from tolokaforge.testing.agent_loops import EpisodeHarness

    return EpisodeHarness.build([]).context()


class TestBuiltinLoopConformance(AgentLoopConformanceSuite):
    """``engine-loop`` — the implementation the kit is written from."""

    @pytest.fixture
    def loop_factory(self) -> Any:
        return load_agent_loop("engine-loop")


class TestInMemoryLoopConformance(AgentLoopConformanceSuite):
    """The reference fixture an external implementer copies."""

    @pytest.fixture
    def loop_factory(self) -> Any:
        return in_memory_agent_loop_factory


def _defective(**defects: Any) -> Any:
    """An ``AgentLoopFactory`` over a loop with the named obligations switched off."""

    def factory(context: AgentLoopContext) -> InMemoryAgentLoop:
        return InMemoryAgentLoop(context=context, defects=LoopDefects(**defects))

    return factory


_VECTORS = [
    pytest.param(
        {"raw_provider_call_ids": True},
        "test_every_recorded_call_id_is_declared_and_the_timeline_builds",
        id="ids-not-drawn-from-the-episode-assigner",
    ),
    pytest.param(
        {"raw_provider_call_ids": True, "execute_in_reverse": True},
        "test_two_calls_to_one_tool_keep_their_own_results",
        id="same-tool-twice-mis-attributed",
    ),
    pytest.param(
        {"plain_tool_error_text": True},
        "test_a_failed_calls_tool_message_carries_the_error_prefix",
        id="failed-call-reads-as-a-success",
    ),
    pytest.param(
        {"skip_metrics": True},
        "test_every_generation_reaches_the_metrics_sink",
        id="cost-never-reaches-the-sink",
    ),
    pytest.param(
        {"skip_should_terminate": True},
        "test_should_terminate_runs_after_the_append_and_before_the_tools",
        id="stuck-detection-disabled",
    ),
    pytest.param(
        {"skip_user_turn": True},
        "test_the_user_turn_runs_when_a_turn_produced_no_tool_calls",
        id="conversational-trial-run-agent-only",
    ),
    pytest.param(
        {"ignore_max_turns": True},
        "test_max_turns_bounds_the_episode",
        id="turn-budget-ignored",
    ),
    pytest.param(
        {"ignore_episode_timeout": True},
        "test_a_spent_episode_budget_stops_the_loop_before_it_generates",
        id="episode-wall-time-budget-ignored",
    ),
    pytest.param(
        {"unearned_excluded_reason": TerminationReason.RATE_LIMIT},
        "test_a_clean_episode_does_not_claim_an_excluded_reason",
        id="trial-excluded-from-the-denominator-unearned",
    ),
    pytest.param(
        {"drop_excluding_reason_evidence": True},
        "test_a_typed_provider_timeout_earns_its_excluded_reason",
        id="exclusion-claimed-without-the-evidence-that-earned-it",
    ),
]


class TestTheSuiteDetectsEachVector:
    """One defect, one failing assertion — the kit's own regression guard.

    Each case switches off a single obligation and asserts the conformance test
    written for it fails. Without this, a suite assertion could be silently
    weakened to something every loop satisfies and nothing would notice.
    """

    @pytest.mark.parametrize(("defects", "test_name"), _VECTORS)
    def test_the_named_assertion_fails_on_the_defective_loop(
        self, defects: dict[str, Any], test_name: str
    ) -> None:
        suite = AgentLoopConformanceSuite()
        with pytest.raises(AssertionError):
            getattr(suite, test_name)(_defective(**defects))

    @pytest.mark.parametrize(("defects", "test_name"), _VECTORS)
    def test_the_same_defect_leaves_the_conforming_loop_green(
        self, defects: dict[str, Any], test_name: str
    ) -> None:
        """The control: the assertion passes on the loop with the defect off."""
        suite = AgentLoopConformanceSuite()
        getattr(suite, test_name)(in_memory_agent_loop_factory)


def test_the_mis_join_the_timelines_own_guard_cannot_see() -> None:
    """Why the same-tool-twice assertion has to exist at all.

    ``_require_every_record_names_its_declared_tool`` is the timeline's
    independent corroboration that its occurrence-order pairing is right: where
    the two views name different tools, the pairing that put them together is
    wrong. Two calls to ONE tool defeat it, exactly as its own docstring says —
    it "mis-joins silently when they name the same one".

    The loop below keys its calls by a raw provider id the provider repeats and
    executes them out of declaration order. ``build_trial_timeline`` returns a
    complete timeline with no error, and every call in it carries the other
    call's result. Nothing downstream notices; the trial is graded on it.
    """
    from tolokaforge.testing.agent_loops import (
        EpisodeHarness,
        assistant_turn,
        run_episode,
        tool_call,
        tool_calls_by_id,
        tool_results_by_id,
    )

    harness = EpisodeHarness.build(
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
    episode = run_episode(_defective(raw_provider_call_ids=True, execute_in_reverse=True), harness)

    events = episode.timeline_events()
    calls = tool_calls_by_id(events)
    results = tool_results_by_id(events)
    assert set(calls) == set(results), "the timeline joined every call to a result"

    swapped = {call_id: (calls[call_id].arguments, results[call_id].result) for call_id in calls}
    assert any(
        (arguments or {}).get("q") not in (result or "") for arguments, result in swapped.values()
    ), (
        "the defective loop was expected to mis-attribute at least one result; "
        f"the timeline reads {swapped!r}"
    )


def test_the_kit_is_importable_from_the_distributed_package() -> None:
    """An external implementer reaches the suite without a source checkout.

    ``tolokaforge.testing`` ships inside the wheel (``[tool.hatch.build.targets.wheel]``
    names the whole ``tolokaforge`` package), so the import path a third party
    writes in their own test file is this one.
    """
    import tolokaforge.testing.agent_loops as kit

    for name in ("AgentLoopConformanceSuite", "InMemoryAgentLoop", "EpisodeHarness"):
        assert name in kit.__all__
        assert getattr(kit, name) is not None


def test_the_in_memory_fixture_keeps_a_call_log() -> None:
    """ADR-0011's fixture convention: a record of what was called, for the caller.

    The suite reads the harness's logs, not the loop's; this one is for an
    orchestrator-level test that wants to assert what the loop itself did.
    """
    from tolokaforge.testing.agent_loops import EpisodeHarness, assistant_turn, tool_call

    harness = EpisodeHarness.build(
        [
            assistant_turn(text="acting", tool_calls=[tool_call("search", {"q": "x"}, "c1")]),
            assistant_turn(text="done"),
        ]
    )
    loop = in_memory_agent_loop_factory(harness.context())
    outcome = loop.run("system", [Message(role="user", content="go")], __import__("time").time())

    assert isinstance(outcome, LoopOutcome)
    assert loop.call_log.turns == 2
    assert loop.call_log.declared_call_ids == ["c1"]
    assert loop.call_log.executed_call_ids == ["c1"]
    assert loop.call_log.termination_checks == 2
    assert loop.call_log.user_turns == 0
