"""Out-of-tree tolokaforge plug-in, installed (not workspace-linked) by the
integration tests to prove a downstream package's seams become discoverable and
runnable end-to-end.

Registers one name in each of the three orchestrator seam groups via this
package's own ``pyproject.toml``:

* ``fixture_backend`` (``tolokaforge.runtime_backends``) — reuses the in-memory
  backend's behaviour.
* ``fixture_grader`` (``tolokaforge.trial_graders``) — returns a fixed sentinel
  :class:`~tolokaforge.core.models.Grade` no built-in produces, so a test can
  prove a grade came from the fixture.
* ``fixture_conductor`` (``tolokaforge.conductors``) — emits a fully pinned
  synthetic :class:`~tolokaforge.core.models.Trajectory` (no LLM, no RPC) and
  grades it with an explicitly-held grader.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from tolokaforge.core.models import Grade, TaskConfig, Trajectory, TrialStatus
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.trial import TrialResult, TrialSpec

if TYPE_CHECKING:
    from tolokaforge.core.conductor import ConductorContext
    from tolokaforge.core.plugin_registry import TrialGraderContext
    from tolokaforge.core.trial_grader import TrialGrader

__all__ = [
    "FixtureConductor",
    "FixtureGrader",
    "FixtureRuntimeBackend",
    "fixture_backend_factory",
    "fixture_conductor_factory",
    "fixture_grader_factory",
]

#: Fixed, obviously-synthetic timestamp for both trajectory bounds — keeps the
#: serialized ``TrialResult`` byte-stable so the golden transcript needs no mask.
_FIXED_TS = datetime(2020, 1, 1, tzinfo=timezone.utc)

#: Sentinel grade values no built-in grader produces — a test asserting these
#: proves the grade came from this fixture, not a shipped grader.
_SENTINEL_SCORE = 0.42
_SENTINEL_REASON = "fixture-grader sentinel"


class FixtureRuntimeBackend(InMemoryRuntimeBackend):
    """Minimal downstream backend — reuses the in-memory fixture's behaviour."""


def fixture_backend_factory(ctx: object) -> FixtureRuntimeBackend:
    """Build the fixture backend. Ignores the build context (nothing to seed)."""
    return FixtureRuntimeBackend()


class FixtureGrader:
    """Downstream :class:`~tolokaforge.core.trial_grader.TrialGrader` returning a
    fixed sentinel :class:`~tolokaforge.core.models.Grade`."""

    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade:
        return Grade(binary_pass=True, score=_SENTINEL_SCORE, reasons=_SENTINEL_REASON)


def fixture_grader_factory(ctx: TrialGraderContext) -> FixtureGrader:
    """Build the fixture grader. Ignores the context (nothing to bind)."""
    return FixtureGrader()


class FixtureConductor:
    """Downstream :class:`~tolokaforge.core.conductor.Conductor` that emits a
    fully pinned synthetic trajectory and grades it with its held grader.

    Takes an explicit grader so both the ``run_trial`` factory path and an
    orchestrator baseline (whose ``ConductorContext.trial_grader`` is hardcoded
    to ``runner_rpc``) can inject the fixture grader deterministically.
    """

    def __init__(self, grader: TrialGrader) -> None:
        self._grader = grader

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        task_id, trial_idx = spec.trial_id.rsplit(":", 1)
        trajectory = Trajectory(
            task_id=task_id,
            trial_index=int(trial_idx),
            start_ts=_FIXED_TS,
            end_ts=_FIXED_TS,
            status=TrialStatus.COMPLETED,
            messages=[],
        )
        trajectory.grade = self._grader.grade(spec, trajectory, agent_system_prompt="")
        return TrialResult.from_trajectory(
            trial_id=spec.trial_id, trajectory=trajectory, worker_id=None
        )


def fixture_conductor_factory(ctx: ConductorContext) -> FixtureConductor:
    """Build the fixture conductor, threading the context-resolved grader.

    On the ``run_trial`` path ``ctx.trial_grader`` is the resolved
    ``fixture_grader``, so the fixture grader is genuinely exercised.
    """
    return FixtureConductor(grader=ctx.trial_grader)
