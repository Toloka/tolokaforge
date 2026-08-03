"""What a fold that counted nothing says, on the in-repo packs that reach each cell.

The verdicts are locked across both substrates at the canonical tier
(``tests/canonical/test_grading_substrate_parity.py`` § 14). What is locked here is the
*sentence*: which components a fail names, driven through the real engine over authored packs
rather than over a config written for the occasion, because the defect being closed was a fail
whose reason read ``"Transcript: All checks passed"``.

Two cells. ``db_probe_grading``'s only state source is ``db_probes``, which resolves only
inside the task's docker network, so core evaluates nothing and the scored set is empty — an
authored pack, graded as it ships. The other cell, a non-empty scored set whose shares cancel,
is reached from a config assembled here: **no pack in the repository is misconfigured that
way**, and the corpus guard
(``tests/canonical/test_example_pack_grading_corpus.py::test_every_authored_pack_and_its_weight_map_name_the_same_components``)
is what keeps it so. Assembling one is therefore the only way to reach the cell, and the two
tests say which of them grades a real artifact.
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


def test_a_config_weighting_its_only_scored_component_to_zero_names_the_share() -> None:
    """The weighted fold's other half: a scored component whose share cancels it.

    ``transcript_rules`` is scored and weighted ``0.0``, so the mean has nothing to average,
    while ``state_checks`` is requested and produces no verdict because ``db_probes`` resolves
    only runner-side. Both facts about the fold are named — the component that produced no
    verdict and the share that cancels the one that did — because either alone leaves the
    author guessing which line to change.

    The two component assertions are what stop this from locking the empty-scored-set cell a
    second time: with ``transcript_rules`` unscored the sum-to-zero clause is unreachable and
    the reason below would come from the other branch entirely.
    """
    grade = GradingEngine(
        GradingConfig(
            combine={
                "method": "weighted",
                "weights": {"state_checks": 1.0, "transcript_rules": 0.0},
                "pass_threshold": 0.7,
            },
            state_checks={
                "db_probes": [
                    {
                        "name": "a_probe_only_the_runner_can_reach",
                        "dsn": "postgresql://grader:grader_pw@app-db:5432/mfg",
                        "query": "SELECT 1",
                        "expect": [{"path": "$.row_count", "equals": 1, "description": "one row"}],
                        "description": "a probe resolving inside the task's docker network",
                    }
                ]
            },
            transcript_rules={"max_turns": 5},
        )
    ).grade_trajectory(
        Trajectory(
            task_id="weights_that_cancel",
            trial_index=0,
            start_ts=_FIXTURE_TIMESTAMP,
            end_ts=_FIXTURE_TIMESTAMP,
            messages=[],
        ),
        {},
    )

    assert (grade.score, grade.binary_pass) == (0.0, False)
    assert grade.components.state_checks is None
    assert grade.components.transcript_rules == 1.0
    assert "state_checks" in grade.reasons, grade.reasons
    assert "transcript_rules=0.0" in grade.reasons, grade.reasons


_A_SUITE_THAT_SKIPS_EVERYTHING = '''
"""Every check declines, which is the shape an enabled suite reaches on a trial it
cannot say anything about — a precondition none of its checks found."""

from tolokaforge.core.grading.checks_interface import CheckContext, CheckSkipped, check, init


@init(interface_version="1.0")
def setup(ctx: CheckContext) -> None:
    pass


@check
def the_counter_was_not_there_to_read():
    return CheckSkipped("no counter in the final state, so there is nothing to compare")


@check
def no_tool_call_was_recorded():
    return CheckSkipped("the trial recorded no tool calls, so none can be counted")
'''


def test_an_enabled_suite_that_skipped_everything_leaves_the_component_unscored(
    tmp_path: Path,
) -> None:
    """Standing single case: the vacuous ``1.0`` sign-flipped, on the real check runner.

    An enabled suite whose every check skips reached a verdict about nothing, and the mean
    over zero verdicts is ``0.0`` — a component scored against no evidence, which fails a
    trial for the author's precondition rather than for anything the agent did. So the
    component is left unscored, and the fold then decides: ``custom_checks`` is the only
    component this config asks for, nothing counted, and the trial fails **with a reason
    naming it** rather than on a silent zero.

    Driven through ``CheckRunner`` over a real ``checks.py`` on disk, because the claim is
    about what the runner reports for a suite of skips — a hand-built ``CheckResultSet``
    would assert the property against itself.
    """
    (tmp_path / "checks.py").write_text(_A_SUITE_THAT_SKIPS_EVERYTHING)
    grade = GradingEngine(
        GradingConfig(
            combine={
                "method": "weighted",
                "weights": {"custom_checks": 1.0},
                "pass_threshold": 0.8,
            },
            custom_checks={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
        ),
        task_dir=tmp_path,
    ).grade_trajectory(
        Trajectory(
            task_id="every_check_skipped",
            trial_index=0,
            start_ts=_FIXTURE_TIMESTAMP,
            end_ts=_FIXTURE_TIMESTAMP,
            messages=[],
        ),
        {},
    )

    assert grade.components.custom_checks is None
    assert (grade.score, grade.binary_pass) == (0.0, False)
    assert "custom_checks" in grade.reasons, grade.reasons
    assert "2 skipped" in grade.reasons, grade.reasons
    assert _NOTHING_TO_REPORT not in grade.reasons, grade.reasons


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
