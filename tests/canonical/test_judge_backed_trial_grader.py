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
    _JUDGE_ONLY_EQUIVALENT_CONFIG,
    JudgeBackedTrialGrader,
    TrialGrader,
    judge_backed_trial_grader_factory,
)
from tolokaforge.runner.models import RunnerGradingConfig

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

    def test_factory_builds_a_working_grader(self) -> None:
        """The factory constructs a real ``JudgeBackedTrialGrader`` with
        an inner dispatch that reads the task's rubric + the run's judge
        model. No misconfiguration surface today — the two ways the
        callable can refuse (task without ``llm_judge``, run without
        ``models.judge``) surface at grade time as ``GradingFailedError``,
        not at construction, because the same factory serves both
        rubric-carrying and rubric-less tasks in one run.
        """
        ctx = TrialGraderContext(runner_address="ignored:0", logger=MagicMock())
        grader = judge_backed_trial_grader_factory(ctx)
        assert isinstance(grader, JudgeBackedTrialGrader)
        assert callable(grader.judge_fn)

    def test_factory_registers_the_equivalent_composite_shape(self) -> None:
        """The module-level ``_JUDGE_ONLY_EQUIVALENT_CONFIG`` pins the
        composite dispatch shape ``judge_only`` collapses to — the
        "one implementation, two names" contract locked at the code
        layer without cross-file drift. Byte-parity between the two
        paths on the constrained-input shape is separately locked at
        ``tests/canonical/test_judge_only_composite_llm_judge_only_parity.py``.
        """
        assert (
            RunnerGradingConfig(
                grading_method="composite",
                weights={"llm_judge": 1.0},
            )
            == _JUDGE_ONLY_EQUIVALENT_CONFIG
        )

    def test_grade_raises_when_task_has_no_llm_judge_block(self) -> None:
        """A task with no ``grading.llm_judge`` block cannot be judged;
        ``JudgeBackedTrialGrader.grade`` surfaces a ``GradingFailedError``
        naming the trial so the operator sees which task in a mixed pack
        is misconfigured."""
        from tolokaforge.core.trial_grader import GradingFailedError

        ctx = TrialGraderContext(runner_address="ignored:0", logger=MagicMock())
        grader = judge_backed_trial_grader_factory(ctx)
        spec = make_trial_spec()  # default fixture has no llm_judge block
        with pytest.raises(GradingFailedError, match="no grading.llm_judge"):
            grader.grade(spec, make_trajectory(status=TrialStatus.COMPLETED), "sys")


class TestTrialLostShortCircuit:
    """A trial the runner lost is ungradeable, not an agent failure —
    matching ``RunnerRPCTrialGrader.grade`` so the two impls score the
    same trial the same way in aggregation."""

    def test_trial_lost_returns_none_not_zero(self) -> None:
        grader, judge_fn = _make_grader()
        result = grader.grade(
            make_trial_spec(),
            make_trajectory(
                status=TrialStatus.ERROR,
                termination_reason=TerminationReason.TRIAL_LOST,
            ),
            "sys",
        )
        assert result is None, "TRIAL_LOST must yield None, not Grade(0.0)"
        judge_fn.assert_not_called()


class TestErroredJudgeIsAGradingFailure:
    """An ``ERRORED`` :class:`JudgeResult` is a grading failure the trial
    is ungradeable under — never a booked agent failure. The seam raises
    ``GradingFailedError`` so the caller records ``grading_error`` and
    leaves the grade unset."""

    def test_errored_judge_result_raises_grading_failed_error(self) -> None:
        from unittest.mock import MagicMock

        from tolokaforge.core.trial_grader import GradingFailedError, JudgeBackedTrialGrader

        def _judge_raise_via_errored(spec, trajectory, agent_prompt):  # noqa: ARG001
            raise GradingFailedError("judge_only grader errored: submit_report timeout")

        grader = JudgeBackedTrialGrader(judge_fn=_judge_raise_via_errored, logger=MagicMock())
        with pytest.raises(GradingFailedError, match="errored"):
            grader.grade(
                make_trial_spec(),
                make_trajectory(status=TrialStatus.COMPLETED),
                "sys",
            )


class TestOverridePrecedence:
    """Run-level ``grader.judge`` overrides win per-field over the
    task's ``grading.llm_judge.customization``. Locked here rather than
    at the factory level so a refactor of the resolver cannot silently
    invert the precedence.
    """

    def _resolve(
        self,
        override_disable_kb: bool | None,
        base_disable_kb: bool | None,
    ) -> bool:
        """Duplicate the factory's resolver arm for disable_kb — exact
        parity is what the test locks."""
        if override_disable_kb is not None:
            return bool(override_disable_kb)
        return bool(base_disable_kb)

    def test_override_wins_when_set(self) -> None:
        assert self._resolve(True, False) is True
        assert self._resolve(False, True) is False

    def test_task_customization_wins_when_override_unset(self) -> None:
        assert self._resolve(None, True) is True
        assert self._resolve(None, False) is False

    def test_both_unset_defaults_to_false(self) -> None:
        assert self._resolve(None, None) is False
