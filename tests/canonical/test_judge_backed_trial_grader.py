"""``JudgeBackedTrialGrader`` — the plug-in seam's second registered impl.

The seam accepts more than the runner-RPC shape: a grader can also dispatch
to an injected judge callable directly, no runner state / transcript /
custom-check machinery. This test locks that shape and pins the entry-point
registration so a downstream grader can rely on the same discovery path as
``runner_rpc``.

See ADR-0038 § Design Drivers (Decision 5 — a second impl proves the seam).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory, make_trial_spec
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    TerminationReason,
    TrialStatus,
)
from tolokaforge.core.plugin_registry import TrialGraderContext, load_trial_grader
from tolokaforge.core.trial_grader import (
    JudgeBackedTrialGrader,
    TrialGrader,
    judge_backed_trial_grader_factory,
)

pytestmark = pytest.mark.canonical


def _make_grader(
    judge_return: Grade | None = None,
) -> tuple[JudgeBackedTrialGrader, MagicMock]:
    """Build a grader whose judge dispatch returns ``judge_return`` unconditionally."""
    judge_fn = MagicMock(return_value=judge_return)
    grader = JudgeBackedTrialGrader(judge_fn=judge_fn, logger=MagicMock())
    return grader, judge_fn


class TestProtocolContract:
    def test_satisfies_trial_grader_protocol(self) -> None:
        grader, _ = _make_grader()
        assert isinstance(grader, TrialGrader)


class TestSuccessPath:
    def test_grade_delegates_to_judge_fn_and_returns_its_verdict(self) -> None:
        expected = Grade(
            binary_pass=True,
            score=0.87,
            components=GradeComponents(llm_judge=0.87),
            reasons="rubric met",
        )
        grader, judge_fn = _make_grader(judge_return=expected)
        spec = make_trial_spec()
        trajectory = make_trajectory(status=TrialStatus.COMPLETED)

        result = grader.grade(spec, trajectory, "you are the agent")

        judge_fn.assert_called_once_with(spec, trajectory, "you are the agent")
        assert result is expected

    def test_none_from_judge_is_propagated(self) -> None:
        grader, _ = _make_grader(judge_return=None)
        result = grader.grade(
            make_trial_spec(), make_trajectory(status=TrialStatus.COMPLETED), "sys"
        )
        assert result is None


class TestAutoFailBranches:
    """Matches ``RunnerRPCTrialGrader``: error / timeout / stuck short-circuit to
    an auto-fail :class:`Grade` before the judge is invoked."""

    def test_error_status_short_circuits_without_calling_judge(self) -> None:
        grader, judge_fn = _make_grader()
        result = grader.grade(make_trial_spec(), make_trajectory(status=TrialStatus.ERROR), "sys")
        assert isinstance(result, Grade)
        assert result.binary_pass is False
        assert result.score == 0.0
        judge_fn.assert_not_called()

    def test_timeout_status_short_circuits(self) -> None:
        grader, judge_fn = _make_grader()
        result = grader.grade(make_trial_spec(), make_trajectory(status=TrialStatus.TIMEOUT), "sys")
        assert isinstance(result, Grade)
        assert result.binary_pass is False
        judge_fn.assert_not_called()

    def test_stuck_termination_short_circuits(self) -> None:
        grader, judge_fn = _make_grader()
        result = grader.grade(
            make_trial_spec(),
            make_trajectory(
                status=TrialStatus.COMPLETED,
                termination_reason=TerminationReason.STUCK_DETECTED,
            ),
            "sys",
        )
        assert isinstance(result, Grade)
        assert result.binary_pass is False
        judge_fn.assert_not_called()


class TestFactoryAndRegistration:
    def test_registered_under_judge_only_entry_point(self) -> None:
        """The plug-in registry resolves ``judge_only`` to the factory —
        proving the second impl ships as a first-class seam consumer."""
        factory = load_trial_grader("judge_only")
        assert factory is judge_backed_trial_grader_factory

    def test_factory_raises_until_wired(self) -> None:
        """The factory fails loud at orchestrator startup — matching the
        altitude ``queue_trial_grader_factory`` fails at — so an operator
        selecting ``judge_only`` sees the misconfiguration before a trial
        is dispatched, not deep inside :meth:`grade` after a paid trial.
        """
        ctx = TrialGraderContext(runner_address="ignored:0", logger=MagicMock())
        with pytest.raises(NotImplementedError, match="not yet wired"):
            judge_backed_trial_grader_factory(ctx)
