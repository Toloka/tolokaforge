"""What a fold that counted nothing says, on the in-repo packs that reach each cell.

The verdicts are locked across both substrates at the canonical tier
(``tests/canonical/test_grading_substrate_parity.py`` § 14). What is locked here is the
*sentence*: which components a fail names, driven through the real engine over authored packs
rather than over a config written for the occasion, because the defect being closed was a fail
whose reason read ``"Transcript: All checks passed"``.

Two packs, two cells. ``db_probe_grading``'s only state source is ``db_probes``, which resolves
only inside the task's docker network, so core evaluates nothing and the scored set is empty.
``widgets_id_fields`` scores ``transcript_rules`` at an authored weight of ``0.0`` while its
``state_checks`` block declares no source core can read, so the scored set is non-empty and its
shares sum to zero. Together they cover both halves of "no scored component carries weight" on
real fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import Grade, GradingConfig, Trajectory

pytestmark = [pytest.mark.unit, pytest.mark.grading]

_REPO = Path(__file__).resolve().parents[3]
_TASKS = _REPO / "tests/data/tasks"
_FIXTURE_TIMESTAMP = "2026-01-01T00:00:00+00:00"

#: The fallback ``Grade.reasons`` carries when no component reported anything. A fold that
#: counted nothing and left this in place is the defect: a failing trial whose only sentence
#: says every check passed.
_NOTHING_TO_REPORT = "All checks passed"


def _grade(pack: str) -> Grade:
    """One authored pack graded on a trial that did nothing, through the real engine."""
    adapter = NativeAdapter({"base_dir": str(_TASKS.parent), "tasks_glob": "tasks/**/task.yaml"})
    return GradingEngine(adapter.get_grading_config(pack), task_dir=_TASKS / pack).grade_trajectory(
        Trajectory(
            task_id=pack,
            trial_index=0,
            start_ts=_FIXTURE_TIMESTAMP,
            end_ts=_FIXTURE_TIMESTAMP,
            messages=[],
        ),
        {},
    )


def test_a_pack_whose_only_source_core_cannot_read_fails_naming_the_component() -> None:
    """``db_probes`` is ``RUNNER_ONLY`` by design, so core scoring nothing here is the declared
    asymmetry rather than a regression — and the trial still has to fail, because a component
    the author configured produced no verdict.

    The negative half is the whole point. A fold reaching ``0.0`` by arithmetic rather than by
    decision leaves ``Grade.reasons`` naming nothing, and the sentence it then carries is the
    fallback below — a failing trial whose only account of itself says every check passed.
    """
    grade = _grade("db_probe_grading")

    assert (grade.score, grade.binary_pass) == (0.0, False)
    assert "state_checks" in grade.reasons, grade.reasons
    assert _NOTHING_TO_REPORT not in grade.reasons, grade.reasons


def test_a_pack_weighting_its_only_scored_component_to_zero_names_the_share() -> None:
    """The weighted fold's other half, on the one in-repo pack that reaches it.

    ``transcript_rules`` is scored and carries an authored weight of ``0.0``, so the mean has
    nothing to average. Both facts about the fold are named: the component that produced no verdict and the share that cancels the
    one that did, because either alone leaves the author guessing which line to change.
    """
    grade = _grade("widgets_id_fields")

    assert (grade.score, grade.binary_pass) == (0.0, False)
    assert grade.components.transcript_rules == 1.0, (
        "transcript_rules is no longer scored here, so the sum-to-zero clause below is "
        "unreachable and this locks the empty-scored-set cell a second time"
    )
    assert "state_checks" in grade.reasons, grade.reasons
    assert "transcript_rules=0.0" in grade.reasons, grade.reasons


def test_a_config_asking_for_nothing_is_the_one_fold_that_owes_no_reason() -> None:
    """The deliberately non-scoring shape, and the only pass a fold hands out unearned.

    Nothing configured and nothing weighted asked for nothing, so nothing is owed and there is
    no sentence to write. Asserted beside the two fails above so the rule cannot be read as
    "an unscored trial always fails": a wire-shape probe pack that scores nothing on purpose
    keeps its pass.
    """
    grade = GradingEngine(
        GradingConfig(combine={"method": "weighted", "weights": {}, "pass_threshold": 1.0})
    ).grade_trajectory(
        Trajectory(
            task_id="asks_for_nothing",
            trial_index=0,
            start_ts=_FIXTURE_TIMESTAMP,
            end_ts=_FIXTURE_TIMESTAMP,
            messages=[],
        ),
        {},
    )

    assert (grade.score, grade.binary_pass) == (1.0, True)
    assert grade.reasons == _NOTHING_TO_REPORT
