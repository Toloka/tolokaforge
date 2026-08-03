"""The three conditions of the migration bar, and what a corpus contributes to them.

Two tiers of test in one module because they are two halves of one contract. The bar itself
is pure — :func:`reconcile_entry` decides every rule from what each bundle recorded — so the
direction rule, the acknowledgement rule and the recorded-``was`` verification are driven
directly with hand-written evidence, including the degenerate corpora (one trial, no
contributing trial, every label identical) a real corpus would take a run to produce.

The reading half is driven through :func:`reconcile_corpus` over *copies of the committed
corpus*, mutated one field at a time. No production grading is mocked: the constraint verdict
comes from the one production evaluator over the real recorded timelines, and the judge label
comes from the real recorded ``grade.yaml``. What a test writes is the defect it is about.

The n=7 corpora below are the shape #683's measurement M4 used — three met / four not-met
with exactly one disagreement, κ ``0.720`` either way — because that is the corpus on which κ
is provably blind to direction and the direction rule has to do the work alone.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.utils.migration_packs import write_migration_pack
from tolokaforge.core.grading.migration_declaration import (
    MigratedCriterion,
    MigrationAcknowledgement,
    MigrationEntry,
    MigrationEvidence,
    MigrationMode,
    MigrationResidual,
    ResidualKind,
)
from tolokaforge.core.grading.rubric_migration import (
    RecomputationGap,
    ReconcileError,
    ReconcileVerdict,
    RecordedTrialVerdict,
    RefusalKind,
    TrialEvidence,
    TrialExclusion,
    migration_counterfactual,
    reconcile_corpus,
    reconcile_entry,
)

pytestmark = [pytest.mark.unit, pytest.mark.grading]

_REPO = Path(__file__).resolve().parents[3]
_COMMITTED_CORPUS = _REPO / "tests/data/migration_corpora/notes_duplicate_check/not_met"
_CORPUS_TASK_ID = "notes_add_note_duplicate_check_gated"
_CRITERION = "checked_duplicates_first"
_CONSTRAINT = "the_notes_were_listed_before_the_note_was_added"

#: The pre-migration criterion text the committed bundles recorded, written out here so a
#: corpus refreshed against a different rubric reds these tests rather than re-basing them.
_PRE_MIGRATION_TEXT = (
    "Before calling add_note, the assistant called list_notes to check for duplicates AND "
    'warned the user that a similar note ("Team stand-up", "Daily stand-up at 9:30 in the '
    'Maple room.") already exists. If the assistant called add_note without first checking '
    "for duplicates, or did not surface the existing near-duplicate, this criterion is NOT met."
)

_GRADING = textwrap.dedent(f"""
    combine:
      method: weighted
      weights: {{ llm_judge: 0.7, trace_checks: 0.3 }}
      pass_threshold: 0.75
    trace_checks:
      constraints:
        - id: {_CONSTRAINT}
          description: "the assistant listed the notes before adding one"
          severity: gate
          require:
            before:
              left:
                quantifier: any
                match: {{ kind: tool_call, tool: {{ equals: list_notes }} }}
              right:
                quantifier: first
                match: {{ kind: tool_call, tool: {{ equals: add_note }} }}
    llm_judge:
      rubric:
        criteria:
          - id: {_CRITERION}
            description: "the assistant warned the user about the near-duplicate"
            kind: binary
            required: true
            weight: 1.0
    """)


# ---------------------------------------------------------------------------
# The bar, driven with written-out evidence
# ---------------------------------------------------------------------------


def _was(**overrides: Any) -> MigratedCriterion:
    return MigratedCriterion(
        **{
            "description": _PRE_MIGRATION_TEXT,
            "kind": "binary",
            "required": True,
            "weight": 1.0,
            **overrides,
        }
    )


def _entry(
    mode: MigrationMode,
    *,
    acknowledged: tuple[str, ...] = (),
    was: MigratedCriterion | None = None,
    criterion: str = _CRITERION,
) -> MigrationEntry:
    residual = {
        MigrationMode.CANDIDATE: None,
        MigrationMode.NARROWED: MigrationResidual(
            kind=ResidualKind.TEXT, reason="the warning still reaches the judge"
        ),
        MigrationMode.RETIRED: MigrationResidual(
            kind=ResidualKind.NONE, reason="nothing of it remains"
        ),
    }[mode]
    return MigrationEntry(
        criterion=criterion,
        mode=mode,
        by=[_CONSTRAINT],
        was=was or _was(),
        residual=residual,
        evidence=(
            None
            if mode is MigrationMode.CANDIDATE
            else MigrationEvidence(corpus="corpus", observations=7, kappa=0.72)
        ),
        acknowledged=[
            MigrationAcknowledgement(trial=trial, reason="the judge misread the transcript")
            for trial in acknowledged
        ],
    )


def _recorded_verdict(*, judge_met: bool, constraint_passed: bool) -> RecordedTrialVerdict:
    """A bundle whose recorded verdict is consistent with the grade it records.

    Hand-derived, not recomputed: the judge's weighted average runs over the one *non-required*
    criterion and is ``1.0``, while the required one is a pure gate — so a met trial records
    ``1.0`` and passes, and a not-met one records the gate's ``0.0`` and fails. Consistency is
    what keeps the counterfactual from reporting a gap in tests that are about the bar; the
    bar's own view of the pre-migration criterion is ``recorded_criterion``, not this.
    """
    judged = 1.0 if judge_met else 0.0
    criteria = [
        {
            "id": _CRITERION,
            "description": _PRE_MIGRATION_TEXT,
            "kind": "binary",
            "required": True,
            "weight": 1.0,
        },
        {
            "id": "note_saved",
            "description": "a new note was saved",
            "kind": "graded",
            "required": False,
            "weight": 1.0,
        },
    ]
    return RecordedTrialVerdict(
        grading_config={
            "combine": {
                "method": "weighted",
                "weights": {"llm_judge": 1.0},
                "pass_threshold": 0.75,
            },
            "llm_judge": {"rubric": {"criteria": criteria}},
        },
        grade={
            "components": {"llm_judge": judged},
            "score": judged,
            "binary_pass": judge_met,
            "criterion_results": [
                {"id": _CRITERION, "met": judge_met, "score": judged, "justification": "as judged"},
                {"id": "note_saved", "met": True, "score": 1.0, "justification": "as judged"},
            ],
        },
        trace_component=1.0 if constraint_passed else 0.0,
        trace_gate_failed=not constraint_passed,
        gate_constraint_ids=frozenset({_CONSTRAINT}),
    )


def _trial(
    name: str,
    *,
    judge_met: bool | None = None,
    constraint_passed: bool | None = None,
    recorded: MigratedCriterion | None = -1,  # type: ignore[assignment]
    unavailable: tuple[TrialExclusion, str] | None = None,
) -> TrialEvidence:
    labelled = judge_met is not None and constraint_passed is not None
    return TrialEvidence(
        trial=name,
        recorded_criterion=_was() if recorded == -1 else recorded,
        judge_met=judge_met,
        constraint_passed=constraint_passed,
        justification=f"the judge's reading of {name}",
        unavailable=unavailable,
        recorded_verdict=(
            _recorded_verdict(judge_met=bool(judge_met), constraint_passed=bool(constraint_passed))
            if labelled
            else None
        ),
    )


#: The two n=7 corpora κ cannot tell apart: mirror images across the diagonal, so each
#: labeller's marginals are swapped between them and the chance correction is identical.
#: A corpus built by flipping one constraint label *without* swapping the judge marginal is
#: a different corpus with a different κ (0.696), and the pair would no longer isolate
#: direction as the only thing separating them.
_N7 = {
    "permissive": (
        [True, True, True, False, False, False, False],
        [True, True, True, True, False, False, False],
    ),
    "strict": (
        [True, True, True, True, False, False, False],
        [False, True, True, True, False, False, False],
    ),
}


def _n7(*, disagreeing: str) -> list[TrialEvidence]:
    """Seven trials with exactly one disagreement, pointing the named way — κ 0.720 either way."""
    judge, constraint = _N7[disagreeing]
    return [
        _trial(f"corpus/t{index}", judge_met=met, constraint_passed=passed)
        for index, (met, passed) in enumerate(zip(judge, constraint, strict=True))
    ]


@pytest.mark.parametrize(
    ("mode", "verdict"),
    [
        (MigrationMode.NARROWED, ReconcileVerdict.NO_COUNTER_EVIDENCE),
        (MigrationMode.RETIRED, ReconcileVerdict.REFUSED),
    ],
)
def test_a_permissive_disagreement_is_a_narrow_s_expected_shape_and_a_retirement_s_refusal(
    mode: MigrationMode, verdict: ReconcileVerdict
) -> None:
    """The judge saying not-met where the constraint passed is what narrowing *means*.

    The constraint checks one conjunct where the criterion asked for two, so trials the
    criterion failed on the other conjunct pass it — and a retirement claims nothing of the
    criterion remains, which that trial contradicts. κ is 0.720 in both rows, so nothing but
    the direction rule separates them.
    """
    reconciled = reconcile_entry(
        _entry(mode), task_ids=[_CORPUS_TASK_ID], trials=_n7(disagreeing="permissive")
    )

    assert reconciled.verdict is verdict
    assert reconciled.kappa == pytest.approx(0.7197, abs=1e-3)
    assert [row.trial for row in reconciled.permissive_disagreements] == ["corpus/t3"]
    assert reconciled.strict_disagreements == []
    assert (
        reconciled.permissive_disagreements[0].justification == "the judge's reading of corpus/t3"
    )


@pytest.mark.parametrize("mode", [MigrationMode.NARROWED, MigrationMode.RETIRED])
def test_a_strict_disagreement_is_refused_by_every_conversion(mode: MigrationMode) -> None:
    """The judge finding the criterion met where the constraint failed disqualifies both.

    It says the constraint is not even a necessary condition of the criterion, which no
    conversion survives — and κ cannot tell this corpus from the permissive one above.
    """
    reconciled = reconcile_entry(
        _entry(mode), task_ids=[_CORPUS_TASK_ID], trials=_n7(disagreeing="strict")
    )

    assert reconciled.verdict is ReconcileVerdict.REFUSED
    assert reconciled.kappa == pytest.approx(0.7197, abs=1e-3)
    assert [refusal.kind for refusal in reconciled.refusals] == [
        RefusalKind.UNACKNOWLEDGED_DISAGREEMENT
    ]
    assert "corpus/t0" in reconciled.refusals[0].message
    assert [row.trial for row in reconciled.strict_disagreements] == ["corpus/t0"]


def test_an_acknowledged_disagreement_clears_the_direction_rule_and_stays_in_the_report() -> None:
    """A waiver removes the refusal without removing the disagreement from the record."""
    reconciled = reconcile_entry(
        _entry(MigrationMode.RETIRED, acknowledged=("corpus/t3",)),
        task_ids=[_CORPUS_TASK_ID],
        trials=_n7(disagreeing="permissive"),
    )

    assert reconciled.verdict is ReconcileVerdict.NO_COUNTER_EVIDENCE
    assert reconciled.refusals == []
    (row,) = reconciled.permissive_disagreements
    assert row.acknowledged_reason == "the judge misread the transcript"


@pytest.mark.parametrize(
    ("waived", "why"),
    [
        ("corpus/t1", "a trial that agrees"),
        ("corpus/t99", "a trial the corpus does not hold"),
    ],
    ids=["agrees", "absent"],
)
def test_an_acknowledgement_whose_disagreement_is_gone_is_refused(waived: str, why: str) -> None:
    """A standing waiver would waive the next disagreement on that trial unread.

    Both spellings of "no longer disagrees" are one refusal: the trial agrees now, or it is
    not under ``--source`` at all. Neither is a silent pass.
    """
    reconciled = reconcile_entry(
        _entry(MigrationMode.NARROWED, acknowledged=(waived,)),
        task_ids=[_CORPUS_TASK_ID],
        trials=_n7(disagreeing="permissive"),
    )

    assert reconciled.verdict is ReconcileVerdict.REFUSED, why
    assert [refusal.kind for refusal in reconciled.refusals] == [RefusalKind.STALE_ACKNOWLEDGEMENT]
    assert waived in reconciled.refusals[0].message


def test_one_observation_defines_no_kappa_and_is_never_a_pass() -> None:
    """κ needs two observations before chance agreement means anything."""
    reconciled = reconcile_entry(
        _entry(MigrationMode.NARROWED),
        task_ids=[_CORPUS_TASK_ID],
        trials=[_trial("corpus/t0", judge_met=False, constraint_passed=False)],
    )

    assert reconciled.observations == 1
    assert reconciled.accuracy == 1.0
    assert reconciled.kappa is None
    assert reconciled.verdict is ReconcileVerdict.INSUFFICIENT_EVIDENCE


def test_every_label_identical_reads_as_perfect_accuracy_and_no_evidence() -> None:
    """The corpus shape the whole bar exists to refuse: agreement by having no variation."""
    reconciled = reconcile_entry(
        _entry(MigrationMode.RETIRED),
        task_ids=[_CORPUS_TASK_ID],
        trials=[
            _trial(f"corpus/t{index}", judge_met=False, constraint_passed=False)
            for index in range(5)
        ],
    )

    assert (reconciled.accuracy, reconciled.kappa) == (1.0, None)
    assert reconciled.verdict is ReconcileVerdict.INSUFFICIENT_EVIDENCE
    assert reconciled.contingency.judge_not_met_constraint_failed == 5


def test_a_criterion_with_zero_correspondence_reports_no_observation_at_all() -> None:
    """Every trial excluded is zero observations, an undefined κ and an empty table.

    Not an accuracy of 0.0 or 1.0: a rate over no observations is a number with nothing
    behind it, and the report says ``null`` instead.
    """
    reconciled = reconcile_entry(
        _entry(MigrationMode.NARROWED),
        task_ids=[_CORPUS_TASK_ID],
        trials=[
            _trial(
                f"corpus/t{index}",
                unavailable=(TrialExclusion.CONSTRAINT_VERDICT_UNAVAILABLE, "undecided here"),
            )
            for index in range(3)
        ],
    )

    assert reconciled.observations == 0
    assert reconciled.accuracy is None
    assert reconciled.kappa is None
    assert reconciled.verdict is ReconcileVerdict.INSUFFICIENT_EVIDENCE
    assert {row.exclusion for row in reconciled.excluded_trials} == {
        TrialExclusion.CONSTRAINT_VERDICT_UNAVAILABLE
    }


@pytest.mark.parametrize(
    ("field", "recorded"),
    [
        ("required", _was(required=False)),
        ("kind", _was(kind="graded")),
        ("weight", _was(weight=0.5)),
        ("description", _was(description="the post-narrow text")),
    ],
)
def test_a_recorded_rubric_contradicting_was_is_refused_naming_the_bundle_and_the_field(
    field: str, recorded: MigratedCriterion
) -> None:
    """The full ``was`` block, because the bypass this closes turns on ``required`` alone.

    An author who flips the declaration and the pack's criterion in one commit satisfies
    every load-time check — no load-time source holds the pre-migration shape. The bundle
    does, and it does not move with them.
    """
    reconciled = reconcile_entry(
        _entry(MigrationMode.NARROWED),
        task_ids=[_CORPUS_TASK_ID],
        trials=[
            _trial("corpus/t0", judge_met=False, constraint_passed=False, recorded=recorded),
            _trial("corpus/t1", judge_met=True, constraint_passed=True, recorded=recorded),
        ],
    )

    assert reconciled.verdict is ReconcileVerdict.REFUSED
    kinds = {refusal.kind for refusal in reconciled.refusals}
    assert RefusalKind.RECORDED_RUBRIC_CONTRADICTS_WAS in kinds
    message = next(
        refusal.message
        for refusal in reconciled.refusals
        if refusal.kind is RefusalKind.RECORDED_RUBRIC_CONTRADICTS_WAS
    )
    assert "corpus/t0" in message
    assert field in message
    assert "the corpus records the post-migration rubric" in message


def test_two_bundles_recording_different_shapes_refuse_naming_both() -> None:
    """A corpus straddling a pack revision has no single ``was`` to check against."""
    reconciled = reconcile_entry(
        _entry(MigrationMode.NARROWED),
        task_ids=[_CORPUS_TASK_ID],
        trials=[
            _trial("corpus/t0", judge_met=False, constraint_passed=False),
            _trial(
                "corpus/t1",
                judge_met=True,
                constraint_passed=True,
                recorded=_was(weight=0.25),
            ),
        ],
    )

    straddling = next(
        refusal
        for refusal in reconciled.refusals
        if refusal.kind is RefusalKind.CORPUS_STRADDLES_A_PACK_REVISION
    )
    assert "corpus/t0" in straddling.message
    assert "corpus/t1" in straddling.message


def test_a_bundle_whose_recorded_rubric_lacks_the_criterion_is_excluded_and_not_refused() -> None:
    """An absent criterion is a statement about that bundle's provenance, not the declaration.

    The two outcomes must not collapse: a mismatch is the author's defect and refuses the
    entry, while an absence means the bundle cannot speak to the shape at all — and where
    every bundle is absent, the reason is what names the rot.
    """
    reconciled = reconcile_entry(
        _entry(MigrationMode.RETIRED),
        task_ids=[_CORPUS_TASK_ID],
        trials=[
            _trial("corpus/t0", judge_met=False, constraint_passed=False, recorded=None),
            _trial("corpus/t1", judge_met=True, constraint_passed=True, recorded=None),
        ],
    )

    assert reconciled.refusals == []
    assert reconciled.observations == 0
    assert [row.exclusion for row in reconciled.excluded_trials] == [
        TrialExclusion.CRITERION_ABSENT_FROM_RECORDED_RUBRIC
    ] * 2
    assert "the corpus records the post-migration rubric" in reconciled.excluded_trials[0].reason
    assert reconciled.verdict is ReconcileVerdict.INSUFFICIENT_EVIDENCE


def test_a_candidate_is_not_charged_the_recorded_rubric_check() -> None:
    """A candidacy's ``was`` is already checked against the criterion the pack still holds.

    So a bundle whose recorded rubric lacks it still contributes: the pack is a live source
    for that entry, which is exactly what a candidate has and a conversion does not.
    """
    reconciled = reconcile_entry(
        _entry(MigrationMode.CANDIDATE),
        task_ids=[_CORPUS_TASK_ID],
        trials=[
            _trial("corpus/t0", judge_met=False, constraint_passed=False, recorded=None),
            _trial("corpus/t1", judge_met=True, constraint_passed=True, recorded=None),
        ],
    )

    assert reconciled.excluded_trials == []
    assert reconciled.observations == 2
    assert reconciled.verdict is ReconcileVerdict.NO_COUNTER_EVIDENCE
    assert reconciled.gates_the_exit_code is False
    # A candidacy exists to be measured, so the measurement it is waiting for is computed for it
    # too — it converts nothing, which is why the number gates nothing here either.
    assert len(reconciled.counterfactual.trials) == 2


def test_a_trial_cannot_be_both_labelled_and_unlabelled() -> None:
    """The invariant that keeps one trial from being counted and excluded at once."""
    with pytest.raises(ValueError, match="both a label pair and a reason it has none"):
        TrialEvidence(
            trial="corpus/t0",
            judge_met=True,
            constraint_passed=True,
            unavailable=(TrialExclusion.NO_CRITERION_RESULTS, "no results"),
        )


def test_a_contributing_trial_without_its_recorded_verdict_is_refused() -> None:
    """The second invariant, and the reason it is one: an observation with no recorded grade
    behind it would drop out of the counterfactual silently, which reads as a migration that
    moves nothing rather than as a trial nobody measured."""
    with pytest.raises(ValueError, match="no recorded_verdict"):
        TrialEvidence(trial="corpus/t0", judge_met=True, constraint_passed=True)


# ---------------------------------------------------------------------------
# The counterfactual: what the entry's declared map does, per trial
# ---------------------------------------------------------------------------

#: ``cache_debug``'s rubric shape — one required gate plus two graded criteria whose weights sum
#: to the ``1.5`` denominator that makes retiring the graded ``explains_mechanism`` raise the
#: judge component. Written out rather than read from the pack: this is the shape #683's
#: measurement M5 used, and the pack is free to change without re-basing the arithmetic below.
_SCORED_CRITERION = "explains_mechanism"


def _cache_debug_shaped_verdict(*, scored: float, trace: float = 1.0) -> RecordedTrialVerdict:
    """A recorded trial that met the gate and was awarded ``scored`` on the graded criterion.

    The recorded grade is hand-derived: the judge's average runs over the two *non-required*
    criteria, ``(1.0·scored + 0.5·1.0) / 1.5``, and the judge-only weight map makes the trial
    score equal to it.
    """
    judge = (scored + 0.5) / 1.5
    criteria = [
        {
            "id": "identifies_bug",
            "description": "names the stale cache as the root cause",
            "kind": "binary",
            "required": True,
            "weight": 1.0,
        },
        {
            "id": _SCORED_CRITERION,
            "description": "explains that the write does not invalidate the cached key",
            "kind": "graded",
            "required": False,
            "weight": 1.0,
        },
        {
            "id": "no_false_fix",
            "description": "does not attribute the fault to an unrelated cause",
            "kind": "graded",
            "required": False,
            "weight": 0.5,
        },
    ]
    return RecordedTrialVerdict(
        grading_config={
            "combine": {
                "method": "weighted",
                "weights": {"llm_judge": 1.0},
                "pass_threshold": 0.75,
            },
            "llm_judge": {"rubric": {"criteria": criteria}},
        },
        grade={
            "components": {"llm_judge": judge},
            "score": judge,
            "binary_pass": judge >= 0.75,
            "criterion_results": [
                {"id": "identifies_bug", "met": True, "score": 1.0, "justification": "as judged"},
                {
                    "id": _SCORED_CRITERION,
                    "met": scored >= 0.5,
                    "score": scored,
                    "justification": "as judged",
                },
                {"id": "no_false_fix", "met": True, "score": 1.0, "justification": "as judged"},
            ],
        },
        trace_component=trace,
        trace_gate_failed=False,
        gate_constraint_ids=frozenset(),
    )


def _identity_map_retirement() -> MigrationEntry:
    """A retirement of the graded criterion declaring the map the pack already has.

    The identity map is what the freed-share rule leaves an author who shifts nothing, and it
    absorbs no freed share: this entry is exactly the accepted residual that rule surfaces
    rather than gates.
    """
    return MigrationEntry(
        criterion=_SCORED_CRITERION,
        mode=MigrationMode.RETIRED,
        by=[_CONSTRAINT],
        was=MigratedCriterion(
            description="explains that the write does not invalidate the cached key",
            kind="graded",
            required=False,
            weight=1.0,
        ),
        residual=MigrationResidual(kind=ResidualKind.NONE, reason="the check reads it whole"),
        combine_weights={"llm_judge": 1.0},
        evidence=MigrationEvidence(corpus="corpus", observations=1, kappa=0.72),
    )


@pytest.mark.parametrize(
    ("scored", "judge_before"),
    [(1.0, 1.0), (0.9, 0.933333333), (0.5, 0.666666667), (0.0, 0.333333333)],
    ids=["full-marks", "0.9", "0.5", "0.0"],
)
def test_an_identity_map_on_a_criterion_scored_below_full_marks_reports_the_judge_rising(
    scored: float, judge_before: float
) -> None:
    """The standing case that makes the freed-share rule's accepted residual visible.

    A scored criterion's weight sits in the judge component's *denominator*, so retiring one the
    agent did not ace raises the component — and an identity map absorbs none of the freed
    share, which is why the rule can only require the declaration and not prove it safe. The
    report is the instrument: it says so per trial. Full marks is the boundary where the hazard
    vanishes, and it is here to show the rise is the *score's* doing rather than the retirement's.
    """
    entry = _identity_map_retirement()
    trial = _trial("corpus/t0", judge_met=True, constraint_passed=True)
    trial = TrialEvidence(
        trial=trial.trial,
        recorded_criterion=entry.was,
        judge_met=True,
        constraint_passed=True,
        justification=trial.justification,
        recorded_verdict=_cache_debug_shaped_verdict(scored=scored),
    )

    (row,) = migration_counterfactual(entry, [trial]).trials

    assert row.judge_component_before == pytest.approx(judge_before, abs=1e-9)
    assert row.judge_component_after == pytest.approx(1.0, abs=1e-9)
    assert row.weights_before == row.weights_after == {"llm_judge": 1.0}


def test_the_declared_map_is_the_map_the_after_column_is_folded_under() -> None:
    """The point of the mandatory declaration: the report answers what the map *this entry
    declares* does, so the declared map has to reach the fold and not merely the printout.

    Discriminating by construction. The trial recorded ``{llm_judge: 1.0}``, and the trace
    component the migration adds is ``0.0``: under the recorded map the trace component arrives
    unweighted at an implicit ``1.0`` (#744) and the trial scores ``0.5``, while under the
    declared ``{llm_judge: 0.75, trace_checks: 0.25}`` it scores ``0.75``. Reporting the declared
    map while folding under the recorded one therefore reds on the score, not only on the map.
    """
    entry = _identity_map_retirement().model_copy(
        update={"combine_weights": {"llm_judge": 0.75, "trace_checks": 0.25}}
    )
    trial = TrialEvidence(
        trial="corpus/t0",
        recorded_criterion=entry.was,
        judge_met=True,
        constraint_passed=False,
        recorded_verdict=_cache_debug_shaped_verdict(scored=0.0, trace=0.0),
    )

    (row,) = migration_counterfactual(entry, [trial]).trials

    assert row.weights_before == {"llm_judge": 1.0}
    assert row.weights_after == {"llm_judge": 0.75, "trace_checks": 0.25}
    assert row.score_after == pytest.approx(0.75)


def test_the_declared_map_is_reported_even_where_it_shifts_nothing() -> None:
    """An identity map is a declaration a reviewer reads, so it is named rather than elided."""
    entry = _identity_map_retirement()

    counterfactual = migration_counterfactual(entry, [])

    assert counterfactual.weights_declared == {"llm_judge": 1.0}
    assert counterfactual.trials == []


def test_an_entry_declaring_no_map_carries_the_map_its_trial_was_graded_under() -> None:
    """A ``required`` criterion frees no share, so the freed-share rule asks for no map — and the
    counterfactual then folds under the map the trial recorded, never the one the pack holds
    today, which is the post-migration state the report exists to let a reviewer judge."""
    (row,) = migration_counterfactual(
        _entry(MigrationMode.NARROWED),
        [_trial("corpus/t0", judge_met=True, constraint_passed=True)],
    ).trials

    assert row.weights_before == row.weights_after == {"llm_judge": 1.0}


def test_retiring_a_pack_s_last_criterion_leaves_the_judge_component_unevaluated() -> None:
    """A rubric with nothing left in it is a judge that scores nothing, not one that scores
    ``1.0``: folding in the aggregate's vacuous pass would hand the trial a component no
    criterion produced."""
    entry = _entry(MigrationMode.RETIRED)
    recorded = _recorded_verdict(judge_met=True, constraint_passed=True)
    only_one = {
        "llm_judge": {
            "rubric": {
                "criteria": [
                    row
                    for row in recorded.grading_config["llm_judge"]["rubric"]["criteria"]
                    if row["id"] == _CRITERION
                ]
            }
        },
        "combine": recorded.grading_config["combine"],
    }
    trial = TrialEvidence(
        trial="corpus/t0",
        recorded_criterion=_was(),
        judge_met=True,
        constraint_passed=True,
        recorded_verdict=RecordedTrialVerdict(
            grading_config=only_one,
            grade={
                "components": {"llm_judge": 1.0},
                "score": 1.0,
                "binary_pass": True,
                "criterion_results": [
                    {"id": _CRITERION, "met": True, "score": 1.0, "justification": "as judged"}
                ],
            },
            trace_component=1.0,
            trace_gate_failed=False,
            gate_constraint_ids=frozenset({_CONSTRAINT}),
        ),
    )

    (row,) = migration_counterfactual(entry, [trial]).trials

    assert row.judge_component_before == 1.0
    assert row.judge_component_after == -1.0


def test_a_recorded_composed_component_is_named_rather_than_dropped() -> None:
    """``state_checks`` is folded from several sources and no single runner field holds it, so a
    recorded one cannot be routed back through the fold that produced it. Naming the gap is the
    alternative to quietly recomposing the trial without a component it was graded on."""
    recorded = _recorded_verdict(judge_met=True, constraint_passed=True)
    trial = TrialEvidence(
        trial="corpus/t0",
        recorded_criterion=_was(),
        judge_met=True,
        constraint_passed=True,
        recorded_verdict=RecordedTrialVerdict(
            grading_config=recorded.grading_config,
            grade={**recorded.grade, "components": {"llm_judge": 1.0, "state_checks": 0.8}},
            trace_component=recorded.trace_component,
            trace_gate_failed=recorded.trace_gate_failed,
            gate_constraint_ids=recorded.gate_constraint_ids,
        ),
    )

    counterfactual = migration_counterfactual(_entry(MigrationMode.NARROWED), [trial])

    assert counterfactual.trials == []
    (gap,) = counterfactual.unrecomputed_trials
    assert gap.gap is RecomputationGap.COMPOSED_COMPONENT_HAS_NO_RUNNER_FIELD
    assert "state_checks" in gap.reason


def test_a_verdict_the_composition_cannot_reproduce_is_named_and_not_used_as_a_baseline() -> None:
    """The runtime half of the extraction's equivalence claim: the *before* column is the verdict
    the bundle recorded, so the recomposition is checked against it before any *after* column is
    believed. A composition that cannot reproduce what the runner already decided says nothing
    about what the migration would decide, and the report says that instead of a number."""
    recorded = _recorded_verdict(judge_met=False, constraint_passed=False)
    trial = TrialEvidence(
        trial="corpus/t0",
        recorded_criterion=_was(),
        judge_met=False,
        constraint_passed=False,
        recorded_verdict=RecordedTrialVerdict(
            grading_config=recorded.grading_config,
            grade={**recorded.grade, "score": 0.75, "binary_pass": True},
            trace_component=recorded.trace_component,
            trace_gate_failed=recorded.trace_gate_failed,
            gate_constraint_ids=recorded.gate_constraint_ids,
        ),
    )

    counterfactual = migration_counterfactual(_entry(MigrationMode.NARROWED), [trial])

    assert counterfactual.trials == []
    (gap,) = counterfactual.unrecomputed_trials
    assert gap.gap is RecomputationGap.RECOMPUTED_VERDICT_DIVERGES
    assert "score=0.75" in gap.reason


def test_the_migrated_criterion_leaves_the_veto_set_and_the_trace_gate_joins_it() -> None:
    """For a ``required`` criterion the two weight maps are identical and say nothing, which is
    why the veto sets are reported beside them: the transfer of the veto is the whole change."""
    (row,) = migration_counterfactual(
        _entry(MigrationMode.RETIRED), [_trial("corpus/t0", judge_met=True, constraint_passed=True)]
    ).trials

    assert _CRITERION in row.vetoes_before
    assert _CRITERION not in row.vetoes_after
    assert row.vetoes_after == [_CONSTRAINT]


def test_the_trace_gate_the_migration_installs_fails_the_after_verdict_it_gates() -> None:
    """Naming the veto set is not the same as applying the veto, and this is the half that
    applies it: the gate leaves the score alone and fails the trial outright.

    Discriminating by construction, which the committed corpus cannot be: no trial there is
    failed by the gate *alone*. The five the gate fails score ``0.5`` after the migration
    against a ``0.75`` threshold, so the threshold fails them anyway, and the twelve it does
    not fail score ``1.0`` and pass — so a counterfactual ignoring the trace gate outright
    reports every verdict unchanged. Measured: that patch reds nothing in the whole unit and
    canonical suite. Here the retirement leaves a full-marks judge component under an identity
    map, so the after score is ``1.0`` and only the gate can fail it.
    """
    entry = _identity_map_retirement()
    recorded = _cache_debug_shaped_verdict(scored=1.0)
    trial = TrialEvidence(
        trial="corpus/t0",
        recorded_criterion=entry.was,
        judge_met=True,
        constraint_passed=False,
        recorded_verdict=RecordedTrialVerdict(
            grading_config=recorded.grading_config,
            grade=recorded.grade,
            trace_component=recorded.trace_component,
            trace_gate_failed=True,
            gate_constraint_ids=frozenset({_CONSTRAINT}),
        ),
    )

    (row,) = migration_counterfactual(entry, [trial]).trials

    assert row.binary_pass_before is True
    assert row.score_after == pytest.approx(1.0, abs=1e-9)
    assert row.binary_pass_after is False


# ---------------------------------------------------------------------------
# Reading a corpus, over copies of the committed one
# ---------------------------------------------------------------------------


def _corpus(tmp_path: Path, *, keep: int | None = None) -> Path:
    """A writable copy of the committed corpus, optionally trimmed to ``keep`` bundles."""
    destination = tmp_path / "corpus"
    shutil.copytree(_COMMITTED_CORPUS, destination)
    if keep is not None:
        for bundle in sorted(destination.iterdir())[keep:]:
            shutil.rmtree(bundle)
    return destination


def _packs(tmp_path: Path, *, name: str = "packs", **pack: Any) -> Path:
    """A search root holding one fixture pack for the corpus's task id."""
    root = tmp_path / name
    write_migration_pack(
        root / "notes",
        grading_text=_GRADING,
        task_id=pack.pop("task_id", _CORPUS_TASK_ID),
        migration={"migrations": [_declared(**pack)]},
    )
    return root


def _declared(**overrides: Any) -> dict[str, Any]:
    declared: dict[str, Any] = {
        "criterion": _CRITERION,
        "mode": "narrowed",
        "by": [_CONSTRAINT],
        "was": {
            "kind": "binary",
            "required": True,
            "weight": 1.0,
            "description": _PRE_MIGRATION_TEXT,
        },
        "residual": {"kind": "text", "reason": "the warning still reaches the judge"},
        "evidence": {"corpus": "corpus", "observations": 5, "kappa": None},
    }
    declared.update(overrides)
    return declared


def _reconcile(source: Path, *packs: Path) -> Any:
    return reconcile_corpus(source, replay_id="unit", packs=list(packs), dry_run=True)


def _patch_grade(bundle: Path, **fields: Any) -> None:
    grade = yaml.safe_load((bundle / "grade.yaml").read_text())
    grade.update(fields)
    (bundle / "grade.yaml").write_text(yaml.safe_dump(grade))


@pytest.mark.parametrize(
    ("patch", "exclusion", "names"),
    [
        ({"judge_status": "errored"}, TrialExclusion.JUDGE_DID_NOT_COMPLETE, "'errored'"),
        ({"criterion_results": []}, TrialExclusion.NO_CRITERION_RESULTS, "no criterion_results"),
        (
            {"criterion_results": [{"id": "note_saved", "met": True, "score": 1.0, "j": ""}]},
            TrialExclusion.NO_VERDICT_FOR_CRITERION,
            _CRITERION,
        ),
    ],
    ids=["errored", "no-results", "no-verdict-for-criterion"],
)
def test_a_trial_the_judge_never_labelled_is_excluded_with_its_reason(
    tmp_path: Path, patch: dict[str, Any], exclusion: TrialExclusion, names: str
) -> None:
    """An errored judge is *not* a not-met label — folding it in manufactures agreement.

    Each exclusion is reported with the reason it happened, so a shrinking denominator is
    visible rather than a corpus that quietly got smaller.
    """
    corpus = _corpus(tmp_path)
    patched = sorted(corpus.iterdir())[0]
    _patch_grade(patched, **patch)

    (entry,) = _reconcile(corpus, _packs(tmp_path)).entries

    assert entry.observations == 4
    (excluded,) = entry.excluded_trials
    assert excluded.trial == str(patched)
    assert excluded.exclusion is exclusion
    assert names in excluded.reason


def test_a_source_holding_no_bundle_reconciles_nothing_and_says_so(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ReconcileError, match="no trial bundle under"):
        _reconcile(empty, _packs(tmp_path))


def test_a_task_id_resolving_in_no_root_names_the_id_and_the_roots(tmp_path: Path) -> None:
    """A corpus recorded against one tree and reconciled against another reconciles nothing."""
    elsewhere = _packs(tmp_path, task_id="some_other_task")

    with pytest.raises(ReconcileError) as raised:
        _reconcile(_corpus(tmp_path), elsewhere)

    assert _CORPUS_TASK_ID in str(raised.value)
    assert str(elsewhere) in str(raised.value)


def test_a_task_id_resolving_in_two_roots_names_every_pack_that_claims_it(tmp_path: Path) -> None:
    first = _packs(tmp_path, name="packs_a")
    second = _packs(tmp_path, name="packs_b")

    with pytest.raises(ReconcileError) as raised:
        _reconcile(_corpus(tmp_path), first, second)

    assert "2 packs" in str(raised.value)
    assert str(first) in str(raised.value) and str(second) in str(raised.value)


def test_a_pack_declaring_no_migration_leaves_nothing_to_reconcile(tmp_path: Path) -> None:
    """Reconciling nothing is a refusal, not an empty report that exits zero."""
    root = tmp_path / "bare"
    write_migration_pack(root / "notes", grading_text=_GRADING, task_id=_CORPUS_TASK_ID)

    with pytest.raises(ReconcileError, match="declares a migration"):
        _reconcile(_corpus(tmp_path), root)


def _second_task(tmp_path: Path, corpus: Path, *, task_id: str, **pack: Any) -> Path:
    """A second task's bundles, re-stamped from the first task's, and its own pack."""
    for bundle in sorted(corpus.iterdir())[:2]:
        twin = corpus.parent / f"{task_id}_{bundle.name}"
        shutil.copytree(bundle, twin)
        task = yaml.safe_load((twin / "task.yaml").read_text())
        task["task_id"] = task_id
        (twin / "task.yaml").write_text(yaml.safe_dump(task))
        shutil.move(str(twin), str(corpus / twin.name))
    root = tmp_path / "packs"
    write_migration_pack(
        root / task_id,
        grading_text=_GRADING,
        task_id=task_id,
        migration={"migrations": [_declared(**pack)]},
    )
    return root


def test_two_tasks_declaring_one_criterion_identically_pool_into_one_verdict(
    tmp_path: Path,
) -> None:
    """One measurement quoted by two declarations, not two independent measurements."""
    corpus = _corpus(tmp_path)
    packs = _packs(tmp_path)
    _second_task(tmp_path, corpus, task_id="notes_sibling")

    (entry,) = _reconcile(corpus, packs).entries

    assert entry.task_ids == [_CORPUS_TASK_ID, "notes_sibling"]
    assert entry.observations == 7


def test_two_tasks_declaring_one_criterion_differently_cannot_be_pooled(tmp_path: Path) -> None:
    """A shared criterion id over two different claims is two measurements folded into one."""
    corpus = _corpus(tmp_path)
    packs = _packs(tmp_path)
    _second_task(
        tmp_path,
        corpus,
        task_id="notes_sibling",
        was={
            "kind": "binary",
            "required": True,
            "weight": 1.0,
            "description": "a different pre-migration text entirely",
        },
    )

    with pytest.raises(ReconcileError) as raised:
        _reconcile(corpus, packs)

    assert _CORPUS_TASK_ID in str(raised.value) and "notes_sibling" in str(raised.value)


def test_a_declaration_the_pack_refuses_is_a_named_refusal_and_not_a_traceback(
    tmp_path: Path,
) -> None:
    """Every rule the sidecar is refused for at authoring time is refused here too.

    The corpus reaches the pack through the same gate ``tolokaforge validate`` applies, so a
    pack whose declaration cannot be honoured is reported as such rather than reaching the
    operator as an unhandled error from a module they did not invoke.
    """
    root = tmp_path / "packs"
    write_migration_pack(
        root / "notes",
        grading_text=_GRADING.replace("severity: gate", "severity: scored"),
        task_id=_CORPUS_TASK_ID,
        migration={"migrations": [_declared()]},
    )

    with pytest.raises(ReconcileError, match="does not load"):
        _reconcile(_corpus(tmp_path), root)


def test_a_bundle_that_cannot_be_read_is_named_and_blocks_the_migration(tmp_path: Path) -> None:
    """An unreadable bundle is a defect in the corpus, not a thin denominator.

    It is reported apart from the exclusions — an excluded trial was read and had nothing to
    contribute — and it blocks, because a verdict over a corpus that partly failed to load is
    a verdict over an unknown denominator.
    """
    corpus = _corpus(tmp_path)
    broken = sorted(corpus.iterdir())[0]
    (broken / "trajectory.yaml").write_text("- not a mapping\n")

    report = _reconcile(corpus, _packs(tmp_path))

    (unreadable,) = report.unreadable_trials
    assert unreadable.trial == str(broken)
    assert "trajectory.yaml" in unreadable.reason
    assert report.entries[0].observations == 4
    assert any(str(broken) in reason for reason in report.blocking)


def test_a_block_the_corpus_cannot_be_graded_against_stops_the_run(tmp_path: Path) -> None:
    """A constraint naming a tool the corpus never had would fail on every trial.

    Reported as a disagreement with the judge it would be a statement about the pack's drift
    dressed as evidence, so the gate the pack meets before a run is applied here too —
    against the tool set each bundle *recorded*, before any trial is re-checked.
    """
    root = tmp_path / "packs"
    write_migration_pack(
        root / "notes",
        grading_text=_GRADING.replace("equals: list_notes", "equals: list_notez"),
        task_id=_CORPUS_TASK_ID,
        migration={"migrations": [_declared()]},
    )

    with pytest.raises(ReconcileError, match="cannot be graded against the tools"):
        _reconcile(_corpus(tmp_path), root)


def test_the_report_lands_under_the_subtree_the_reconciliation_owns(tmp_path: Path) -> None:
    """The artifact path written out rather than composed from the module's own constants.

    Composing it from ``RECONCILE_DIRNAME`` would move both sides of the assertion together
    and pin nothing — measured: renaming the constant reds no test written that way.
    """
    corpus = _corpus(tmp_path)

    reconcile_corpus(corpus, replay_id="written", packs=[_packs(tmp_path)], dry_run=False)

    written = corpus / "reconcile" / "written" / "reconcile_report.yaml"
    assert yaml.safe_load(written.read_text())["replay_id"] == "written"
