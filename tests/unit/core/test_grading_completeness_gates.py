"""The two completion gates on :class:`GradingCompleteness` and their persistence.

Locks the invariants ADR-0041 declares:

- ``zero_coverage`` fires only on a run that had trials to measure.
- ``zero_judge_graded`` requires every scored trial to carry a judge that
  errored — a run with no grades at all does not fire it.
- The two booleans round-trip through ``RunState``'s persisted shape.
- ``scored_trials`` follows the same predicate ``_measured_averages`` uses
  (``t.grade is not None``) — a MEASURED trial missing its grade is not
  scored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tolokaforge.core.models.trajectory import Trajectory
from tolokaforge.core.orchestrator import GradingCompleteness
from tolokaforge.core.resume import RunState, RunStateManager, TrialState

pytestmark = pytest.mark.unit


def test_grading_completeness_widens_with_the_three_derived_counts() -> None:
    completeness = GradingCompleteness(
        total_attempts=5,
        ungradeable_trial_ids=(),
        measured_trials=5,
        scored_trials=4,
        judge_errored_trials=1,
    )

    assert completeness.measured_trials == 5
    assert completeness.scored_trials == 4
    assert completeness.judge_errored_trials == 1
    assert completeness.zero_coverage is False
    assert completeness.zero_judge_graded is False


def test_zero_coverage_only_fires_when_a_run_had_trials_to_measure() -> None:
    on_a_run_that_measured_nothing = GradingCompleteness(
        total_attempts=3, ungradeable_trial_ids=(), measured_trials=0
    )
    assert on_a_run_that_measured_nothing.zero_coverage is True

    on_a_run_with_no_trials_at_all = GradingCompleteness(
        total_attempts=0, ungradeable_trial_ids=(), measured_trials=0
    )
    assert on_a_run_with_no_trials_at_all.zero_coverage is False


def test_zero_coverage_fires_when_every_measured_trial_is_synthesized() -> None:
    """The widened trigger: a run whose every measured trial carries a
    harness-synthesised auto-fail grade fires ``zero_coverage``. The run
    passed the classifier (``measured_trials > 0``) but every measurement
    was an artefact of the harness — no evaluator ran on any trial — so
    ``--fail-on-zero-coverage`` exits non-zero. See ADR-0041.
    """
    on_a_run_of_only_synth_grades = GradingCompleteness(
        total_attempts=3,
        ungradeable_trial_ids=(),
        measured_trials=3,
        scored_trials=3,
        synthesized_trials=3,
    )
    assert on_a_run_of_only_synth_grades.zero_coverage is True


def test_zero_coverage_holds_when_at_least_one_trial_is_real_measured() -> None:
    """The control: one measured trial that produced a real (non-synth) grade
    keeps the run out of ``zero_coverage``. The widening does not swallow a
    run that reached its evaluator at least once, even when most trials
    auto-failed.
    """
    on_a_run_with_one_real_measurement = GradingCompleteness(
        total_attempts=3,
        ungradeable_trial_ids=(),
        measured_trials=3,
        scored_trials=3,
        synthesized_trials=2,
    )
    assert on_a_run_with_one_real_measurement.zero_coverage is False


def test_zero_judge_graded_requires_all_scored_trials_to_have_errored_judges() -> None:
    every_scored_grade_errored = GradingCompleteness(
        total_attempts=3,
        ungradeable_trial_ids=(),
        measured_trials=3,
        scored_trials=3,
        judge_errored_trials=3,
    )
    assert every_scored_grade_errored.zero_judge_graded is True

    some_grades_survived = GradingCompleteness(
        total_attempts=3,
        ungradeable_trial_ids=(),
        measured_trials=3,
        scored_trials=3,
        judge_errored_trials=2,
    )
    assert some_grades_survived.zero_judge_graded is False

    a_run_that_produced_no_grades = GradingCompleteness(
        total_attempts=3,
        ungradeable_trial_ids=(),
        measured_trials=3,
        scored_trials=0,
        judge_errored_trials=0,
    )
    assert a_run_that_produced_no_grades.zero_judge_graded is False


def test_a_measured_trial_missing_its_grade_does_not_count_as_scored() -> None:
    """The single-source derivation follows ``metrics._measured_averages``.

    Constructs a trajectory that classifies as MEASURED (no infra-abort
    termination, no ``grading_error``) but carries no grade — an intermediate
    state the run may write before grading completes. ``scored_trials`` must
    count trajectories by the same predicate the averaged metrics do, so a
    downstream computation cannot disagree with this one on whether the trial
    was scored.
    """
    a_measured_but_ungraded_trajectory = Trajectory(
        task_id="task",
        trial_index=0,
        start_ts=datetime.now(UTC),
        end_ts=datetime.now(UTC),
        messages=[],
        grade=None,
        grading_error=None,
    )
    assert a_measured_but_ungraded_trajectory.grade is None

    scored = sum(1 for t in [a_measured_but_ungraded_trajectory] if t.grade is not None)
    assert scored == 0


def test_run_state_persists_the_two_completion_gate_booleans(tmp_path: Path) -> None:
    state_manager = RunStateManager(output_dir=tmp_path)
    state_manager.initialize_run(
        run_id="run-0", config_path="config.yaml", task_ids=["t"], repeats=1
    )

    state_manager.mark_run_completed(zero_coverage=True, zero_judge_graded=False)

    reloaded = state_manager.load_state()
    assert reloaded is not None
    assert reloaded.status == "completed"
    assert reloaded.zero_coverage is True
    assert reloaded.zero_judge_graded is False


def test_run_state_defaults_the_two_completion_gate_booleans_to_false() -> None:
    """A state file written before the fields existed loads with both False.

    ``RunState`` has no ``extra="forbid"`` so adding two optional booleans is
    non-breaking; the defaults ensure old state files continue to parse.
    """
    from_a_pre_field_state = RunState(
        run_id="run-0",
        config_path="config.yaml",
        output_dir="/tmp/x",
        start_ts=datetime.now(UTC),
        last_updated=datetime.now(UTC),
        status="completed",
        total_trials=1,
        completed_trials=1,
        failed_trials=0,
        trials={"t:0": TrialState(task_id="t", trial_index=0, status="completed")},
    )

    assert from_a_pre_field_state.zero_coverage is False
    assert from_a_pre_field_state.zero_judge_graded is False


def test_orchestrator_config_carries_both_completion_gate_flags() -> None:
    """The two flags are opt-in — both default False.

    Locks the "infrastructure aborts do not fail the run" back-compat contract
    ADR-0041 preserves.
    """
    from tolokaforge.core.models.run_config import OrchestratorConfig

    default = OrchestratorConfig()
    assert default.fail_on_zero_coverage is False
    assert default.fail_on_zero_judge_graded is False

    opted_in = OrchestratorConfig(fail_on_zero_coverage=True, fail_on_zero_judge_graded=True)
    assert opted_in.fail_on_zero_coverage is True
    assert opted_in.fail_on_zero_judge_graded is True
