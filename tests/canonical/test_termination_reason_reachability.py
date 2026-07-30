"""Every ``TerminationReason`` is producible, and only some of them are graded.

Two locks over one observation. Both rest on the ``observed_outcomes`` fixture,
which drives the real termination paths — the dialogue runner, the tool-calling
loop's error classifier and wall-clock check, and the provisioning bracket — and
reports the ``(status, reason)`` pairs it saw. Nothing here is a hand-written
table of what the code is believed to do.

1. Every member of the enum is produced by one of those paths. A reason no path
   produces is a distinction grading cannot draw, however carefully the field
   carrying it is typed.
2. Only reasons describing a trial the agent drove to a normal end reach
   ``GradeTrial``. Every other reason is answered by the host grader itself,
   without an RPC: a task grade describes how the agent performed the task, and
   a trial that never completed has no such performance to describe. Routing one
   to the task grader would produce a score for work that never happened.

The second is asserted as that invariant, not as a description of the mechanism
behind it. Today the mechanism is a pair of short-circuits in
``RunnerRPCTrialGrader.grade`` that synthesise a fail-:class:`Grade` for those
trials; the invariant holds whatever answers them, so a change to how they are
answered leaves this lock intact while a change to *which* reasons reach the
runner fails it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_task_config, make_trajectory, make_trial_spec
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.llm.client import (
    GenerationResult,
    LLMApiTimeoutError,
    UserSimulator,
)
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import Message, TerminationReason, Trajectory, TrialStatus
from tolokaforge.core.output.artifacts import InMemoryArtifactWriter
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor
from tolokaforge.core.trial_grader import RunnerRPCTrialGrader
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

pytestmark = pytest.mark.canonical

# The reasons a trial can end with and still be graded by the runner. Each names
# a trial the agent drove to an end the harness planned for: it signalled
# completion, the simulated user closed the dialogue, or the turn budget ran
# out. Task grading is meaningful for exactly these.
GRADED_REASONS = frozenset(
    {
        TerminationReason.AGENT_DONE,
        TerminationReason.USER_STOP,
        TerminationReason.MAX_TURNS,
    }
)


class _ScriptedAgent:
    """Agent generate seam yielding one queued item per turn, repeating the last.

    An ``Exception`` in the queue is raised rather than returned, which is how
    the loop's error classifier is reached.
    """

    def __init__(self, *items: GenerationResult | Exception) -> None:
        self._items = list(items)

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        item = self._items.pop(0) if len(self._items) > 1 else self._items[0]
        if isinstance(item, Exception):
            raise item
        return item


def _text(body: str) -> GenerationResult:
    return GenerationResult(
        text=body, tool_calls=[], usage=Usage(prompt_tokens=10, completion_tokens=5)
    )


def _run_trial(
    *agent_items: GenerationResult | Exception,
    user_reply: str = "Please carry on.",
    max_turns: int = 2,
    episode_timeout_s: int = 1200,
    stuck_detector: StuckDetector | None = None,
) -> Trajectory:
    """Drive one whole trial through :class:`TrialRunner` and return its trajectory."""
    return TrialRunner(
        task_id="reachability",
        trial_index=0,
        agent_client=_ScriptedAgent(*agent_items),
        user_simulator=UserSimulator(mode="scripted", scripted_flow=[{"default": user_reply}]),
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=max_turns,
        episode_timeout_s=episode_timeout_s,
        stuck_detector=stuck_detector,
    ).run("You are an agent.", "Do the task.")


def _provision_failure_trajectory() -> Trajectory:
    """Drive the provisioning bracket with an environment that never comes up."""
    result = ProvisioningTrialExecutor(
        runtime_backend=InMemoryRuntimeBackend(await_ready_times_out=True),
        conductor=InMemoryConductor(),
        logger=MagicMock(),
        output_dir=Path("/nonexistent-run-dir"),
        artifact_writer=InMemoryArtifactWriter(),
    ).execute(make_trial_spec(), make_task_config())
    return result.trajectory


@pytest.fixture(scope="module")
def observed_outcomes() -> frozenset[tuple[TrialStatus, TerminationReason]]:
    """The ``(status, reason)`` pairs the real termination paths produce."""
    trajectories = [
        _run_trial(_text("All set. ###STOP###")),
        _run_trial(_text("Anything else?"), user_reply="###STOP###"),
        _run_trial(_text("Thinking."), stuck_detector=StuckDetector(max_idle_turns=1)),
        _run_trial(_text("Still working."), max_turns=1),
        _run_trial(_text("unreached"), episode_timeout_s=0),
        _run_trial(LLMApiTimeoutError("upstream never answered")),
        _run_trial(RuntimeError("429 Too Many Requests")),
        _run_trial(RuntimeError("OpenAI returned 500")),
        _run_trial(RuntimeError("something the classifier cannot name")),
        _provision_failure_trajectory(),
    ]
    for trajectory in trajectories:
        assert trajectory.termination_reason is not None, (
            "a driven path ended with no termination reason at all, so it "
            "contributes nothing to either lock"
        )
    return frozenset(
        (trajectory.status, trajectory.termination_reason) for trajectory in trajectories
    )


def test_every_termination_reason_has_a_producing_path(
    observed_outcomes: frozenset[tuple[TrialStatus, TerminationReason]],
) -> None:
    produced = {reason for _, reason in observed_outcomes}
    assert produced == set(TerminationReason), (
        "a TerminationReason member is produced by no termination path, so the "
        "distinction it names cannot occur in any run and grading can never see "
        f"it: {sorted(m.value for m in set(TerminationReason) - produced)}"
    )


class _RecordingBackend:
    """Runtime backend that records what ``grade_trial`` was called with."""

    def __init__(self) -> None:
        self.termination_reasons: list[str | None] = []

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        self.termination_reasons.append(termination_reason)
        return {
            "success": True,
            "grade": {
                "binary_pass": True,
                "score": 1.0,
                "components": {"state_checks": 1.0},
                "reasons": "graded",
            },
        }


def test_only_reasons_from_a_completed_trial_reach_grade_trial(
    observed_outcomes: frozenset[tuple[TrialStatus, TerminationReason]],
) -> None:
    backend = _RecordingBackend()
    grader = RunnerRPCTrialGrader(runtime_backend=backend, logger=MagicMock())

    for status, reason in sorted(observed_outcomes, key=lambda pair: pair[1].value):
        grader.grade(
            make_trial_spec(),
            make_trajectory(status=status, termination_reason=reason),
            "You are an agent.",
        )

    reached = {TerminationReason(value) for value in backend.termination_reasons if value}
    completed = {reason for status, reason in observed_outcomes if status is TrialStatus.COMPLETED}
    never_completed = {reason for _, reason in observed_outcomes} - completed

    assert reached & never_completed == set(), (
        "a reason no completed trial ever carries reached GradeTrial: "
        f"{sorted(r.value for r in reached & never_completed)}. The host grader "
        "answers those trials itself, so this means its routing was bypassed and "
        "the runner scored a task the agent never finished."
    )
    assert reached == GRADED_REASONS, (
        "the set of termination reasons that reach GradeTrial changed. It is a "
        "design invariant, not an accident: "
        f"expected {sorted(r.value for r in GRADED_REASONS)}, "
        f"got {sorted(r.value for r in reached)}"
    )
