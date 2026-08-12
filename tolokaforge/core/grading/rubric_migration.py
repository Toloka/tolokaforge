"""Whether a corpus of recorded judge verdicts contradicts the migration a pack declares.

A pack's ``migration.yaml`` claims that trace constraints stand in for a rubric criterion.
This module checks that claim the only way it can be checked: over trials whose judge already
graded the criterion, by recomputing the named constraints against each trial's recorded
timeline and joining the two verdicts per trial.

**What the verdict is, and is not.** The bar is an absence-of-counter-evidence test over N
observations. :attr:`ReconcileVerdict.NO_COUNTER_EVIDENCE` says no trial in this corpus
contradicted the claim; it is never a claim that the constraint is equivalent to the
criterion, and nothing here ranks one declared ``mode`` above another — the mode is the
author's recorded judgment, which the report renders and does not grade.

**The three conditions.**

* **Evidence.** Cohen's κ over the joined labels must be *defined*. A corpus with no label
  variation reads as perfect accuracy and no evidence at all (κ ``None``), so κ undefined is
  :attr:`ReconcileVerdict.INSUFFICIENT_EVIDENCE` and never a pass. No threshold on κ's value.
* **Direction.** κ cannot see which way a disagreement went, so direction is a condition of
  its own: ``retired`` tolerates no unacknowledged disagreement either way, while a narrow
  tolerates the *permissive* ones (judge not-met, constraint passed) that are its expected
  shape and reports each with the judge's own justification.
* **Acknowledgement.** A disagreement is waived only by naming its trial and a reason in the
  declaration — and an acknowledgement naming a trial that no longer disagrees is a refusal,
  not a silent pass, because a waiver outliving what it waived hides the next disagreement.

**Which rubric ``was`` is checked against.** The pack holds the *post*-migration criterion —
for a ``retired`` one, nothing at all — so the only surviving record of the pre-migration
shape is the rubric each contributing bundle recorded. Verifying ``was`` there is what closes
the bypass an author opens by changing the declaration and the pack in one commit, and it is
what catches a corpus regenerated *after* the migration, whose evidence would otherwise
silently re-base onto the state the migration produced.

**The constraint block comes from the pack, never from a flag.** The block a trial is
re-checked against is resolved through the bundle's ``task_id`` to the pack whose
``migration.yaml`` declares the entry. There is deliberately no ``--constraints``-style
override: pinning a fixture would make a CI re-verification decorative, where resolving from
the pack makes editing the shipped constraint red the lock over the frozen corpus.

**Two sources, one bar.** A reconciliation reads either a corpus somebody pointed at, or —
for every declared migration at once — the corpus each declaration itself names. Every rule
is the same across the two but one: where the source *is* the declared corpus there is no
subset of it to allow for, so ``evidence.observations`` is checked for equality rather than
as a lower bound.

Nothing here spends anything. It reaches the trace-replay reader and its bundle discovery,
the one production trace evaluator, the outcome classifier a run's own attribution uses,
the pure agreement maths and the task loader ``tolokaforge validate`` already uses, and
stops there — measured: no ``litellm`` / ``openai`` / ``anthropic`` module and no judge
module enters ``sys.modules`` on import.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from math import isclose
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from tolokaforge.adapters._task_loader import grading_source_under_adapter, load_task_yaml
from tolokaforge.core.grading.agreement import (
    CriterionObservation,
    accuracy,
    cohen_kappa,
)
from tolokaforge.core.grading.combine_weights import MissingComponentWeight
from tolokaforge.core.grading.config_validation import inspect_grading_authoring
from tolokaforge.core.grading.grade_components import (
    COMPONENT_BY_NAME,
    GRADE_COMPONENTS,
    runner_score_field,
)
from tolokaforge.core.grading.migration_declaration import (
    EVERY_DECLARED_FIELD,
    MIGRATION_FILENAME,
    MigratedCriterion,
    MigrationEntry,
    MigrationMode,
    MigrationResidual,
    corpus_base_for,
    criterion_shape_disagreement,
    inspect_migration_declaration,
)
from tolokaforge.core.grading.replay_layout import discover_trial_bundles
from tolokaforge.core.grading.rubric import aggregate_rubric
from tolokaforge.core.grading.trace_replay import (
    MissingTraceReplayInputError,
    TraceChecksOverride,
    aborted_without_a_task_snapshot,
    read_trace_replay_inputs,
    recorded_grade,
    recorded_task,
    recorded_task_id,
    replay_trace_checks,
    tool_inventory_from_bundle,
)
from tolokaforge.core.grading.trace_timeline import TimelineInconsistencyError
from tolokaforge.core.models import (
    CriterionResult,
    Rubric,
    TraceChecksConfig,
    TraceChecksResult,
    TraceConstraintSeverity,
)
from tolokaforge.runner.grading import RunnerTrialVerdict, compose_runner_trial_verdict

__all__ = [
    "CANDIDATE_LABELLER",
    "DEFAULT_PACKS_ROOT",
    "RECONCILE_DIRNAME",
    "RECONCILE_REPORT_FILENAME",
    "REFERENCE_LABELLER",
    "ContingencyTable",
    "DisagreementDirection",
    "DisagreementRow",
    "ExcludedTrial",
    "MigrationCounterfactual",
    "RecomputationGap",
    "RecordedTrialVerdict",
    "ReconcileError",
    "ReconcileReport",
    "ReconcileVerdict",
    "ReconciledEntry",
    "Refusal",
    "RefusalKind",
    "TrialCounterfactual",
    "TrialEvidence",
    "TrialExclusion",
    "Unavailable",
    "UnreadableTrial",
    "UnrecomputedTrial",
    "emit_reconcile_report",
    "migration_counterfactual",
    "reconcile_corpus",
    "reconcile_declared_corpora",
    "reconcile_entry",
    "reconcile_root",
]

#: Subdirectory the report is written under, a sibling of both replay commands' output.
RECONCILE_DIRNAME = "reconcile"
#: The run-level artifact's name — one report per reconciliation, not one per bundle.
RECONCILE_REPORT_FILENAME = "reconcile_report.yaml"
#: Where a bundle's ``task_id`` is looked up when the operator names no root. The default is
#: what makes a re-verification read the *shipped* pack rather than a fixture.
DEFAULT_PACKS_ROOT = Path("examples")

#: Which side of the pair each label comes from. The maths is symmetric and will not say, so
#: the report does: the judge is the incumbent labeller a migration proposes to replace, and
#: the recomputed constraint is the candidate replacement.
REFERENCE_LABELLER = "the judge's recorded criterion verdict (reference)"
CANDIDATE_LABELLER = "the recomputed trace-constraint verdict (candidate)"

_CORPUS_RECORDS_THE_POST_MIGRATION_RUBRIC = (
    "the corpus records the post-migration rubric; evidence for a migration must come from "
    "bundles recorded before it"
)


class ReconcileError(ValueError):
    """The corpus cannot be reconciled at all, and the message says what to point where.

    Raised for every defect that is a property of the *invocation* rather than of one
    declaration: a source holding no bundle, a ``task_id`` that resolves to no pack or to
    two, a pack whose constraints cannot be graded against what a bundle recorded, and a
    criterion pooled across tasks that do not declare the same thing. A defect in one
    declaration is a :class:`Refusal` on that entry instead — the run still reports every
    other entry, because an author fixing a migration wants the whole list.
    """


class ReconcileVerdict(str, Enum):
    """What the corpus says about one declared entry.

    ``NO_COUNTER_EVIDENCE`` is deliberately not named ``supported``: it says no trial in this
    corpus contradicted the claim over N observations, which is weaker than agreement and far
    weaker than equivalence. It is the only verdict an exit code of ``0`` means.
    """

    NO_COUNTER_EVIDENCE = "no_counter_evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REFUSED = "refused"


class DisagreementDirection(str, Enum):
    """Which way a trial's two labellers differed.

    ``STRICT`` — the judge found the criterion met and the constraint failed, so the
    constraint is not even a necessary condition of the criterion. ``PERMISSIVE`` — the judge
    found it not met and the constraint passed, which is the expected shape of a *narrow*:
    the constraint checks one conjunct where the criterion asked for two.
    """

    STRICT = "strict"
    PERMISSIVE = "permissive"


class TrialExclusion(str, Enum):
    """Why a recorded trial contributes no observation.

    An exclusion is never a refusal. Each of these is a statement about that bundle's
    provenance or completeness, and folding one in as a not-met label would manufacture
    agreement with a failing constraint.
    """

    JUDGE_DID_NOT_COMPLETE = "judge_did_not_complete"
    NO_CRITERION_RESULTS = "no_criterion_results"
    NO_VERDICT_FOR_CRITERION = "no_verdict_for_criterion"
    CRITERION_ABSENT_FROM_RECORDED_RUBRIC = "criterion_absent_from_recorded_rubric"
    CONSTRAINT_VERDICT_UNAVAILABLE = "constraint_verdict_unavailable"


class RefusalKind(str, Enum):
    """Why one declared entry is refused, classified so a caller need not match on prose."""

    UNACKNOWLEDGED_DISAGREEMENT = "unacknowledged_disagreement"
    STALE_ACKNOWLEDGEMENT = "stale_acknowledgement"
    RECORDED_RUBRIC_CONTRADICTS_WAS = "recorded_rubric_contradicts_was"
    CORPUS_STRADDLES_A_PACK_REVISION = "corpus_straddles_a_pack_revision"
    DECLARED_EVIDENCE_CONTRADICTS_MEASUREMENT = "declared_evidence_contradicts_measurement"


class Refusal(BaseModel):
    """One reason an entry is refused: its class, and the sentence naming what to change."""

    kind: RefusalKind
    message: str

    model_config = {"extra": "forbid"}


class ContingencyTable(BaseModel):
    """The 2×2 the two labellers produced — required beside κ, never optional.

    A corpus whose mass sits in one or two cells is *visibly* a designed experiment, which a
    scalar hides. Read as judge (reference) × constraint (candidate).
    """

    judge_met_constraint_passed: int
    judge_met_constraint_failed: int
    judge_not_met_constraint_passed: int
    judge_not_met_constraint_failed: int

    model_config = {"extra": "forbid"}


class DisagreementRow(BaseModel):
    """One trial where the two labellers differed, carrying the judge's own reasoning.

    ``justification`` is the judge's text for that criterion on that trial, never an
    aggregate: a disagreement is triaged by reading why the judge said what it said.
    ``acknowledged_reason`` is the author's waiver where the declaration names this trial.
    """

    trial: str
    judge_met: bool
    constraint_passed: bool
    justification: str
    acknowledged_reason: str | None

    model_config = {"extra": "forbid"}


class ExcludedTrial(BaseModel):
    """A trial that contributed no observation, and the reason it did not."""

    trial: str
    exclusion: TrialExclusion
    reason: str

    model_config = {"extra": "forbid"}


class RecomputationGap(str, Enum):
    """Why a contributing trial carries no counterfactual row.

    Every one is a statement about what the recomputation could establish, never about the
    declaration: the counterfactual gates nothing, so none is a :class:`Refusal`.
    """

    COMPOSED_COMPONENT_HAS_NO_RUNNER_FIELD = "composed_component_has_no_runner_field"
    RECOMPUTED_VERDICT_DIVERGES = "recomputed_verdict_diverges"
    FOLDED_COMPONENT_HAS_NO_DECLARED_WEIGHT = "folded_component_has_no_declared_weight"


class UnrecomputedTrial(BaseModel):
    """A contributing trial whose recorded verdict the composition could not reproduce.

    The *before* column is the verdict the bundle recorded, and the recomputation is checked
    against it before any *after* column is believed: a composition that cannot reproduce what
    the runner already decided says nothing trustworthy about what the migration would decide.
    """

    trial: str
    gap: RecomputationGap
    reason: str

    model_config = {"extra": "forbid"}


class TrialCounterfactual(BaseModel):
    """What the declared migration would have done to one recorded trial's verdict.

    The *before* columns are read from the bundle's own ``grade.yaml``; the *after* columns are
    recomposed by the runner's own verdict function over the reduced rubric, the recomputed
    trace component and the map the entry **declares** — or, where it declares none, the map the
    trial was graded under, nothing having been freed to move. Both weight maps and both veto
    sets ride here per trial rather than once per entry, because each is a fact about the trial
    that recorded it.

    The veto sets are stated beside the weight maps because a ``required`` criterion carries no
    score share: whatever a declared map moves for one is the author's own rebalance rather than
    the conversion's arithmetic, while the veto is what the conversion itself moves.

    **The component and score columns are mode-blind, and the veto set is not.** The *after*
    judge component drops the criterion from the judge's side whatever the mode declares, so for
    a ``narrowed`` entry it is the bound of a **full retirement** rather than the narrow's own
    effect: the narrowed text has no recorded label anywhere, since the corpus was graded
    against the text it replaced, so the narrow's own component is not computable from it. The
    veto set is mode-aware because requiredness is *declared* rather than judged — a narrow
    leaves the criterion in the rubric still vetoing, which the report can state exactly.

    ``judge_component_after`` is ``None`` where the reduced rubric holds no criterion at all: a
    judge that scores nothing has no component, and any number here would read as one it scored.
    """

    trial: str
    weights_before: dict[str, float]
    weights_after: dict[str, float]
    vetoes_before: list[str]
    vetoes_after: list[str]
    judge_component_before: float
    judge_component_after: float | None
    score_before: float
    score_after: float
    binary_pass_before: bool
    binary_pass_after: bool

    model_config = {"extra": "forbid"}


class MigrationCounterfactual(BaseModel):
    """What the entry's declared weight map does to the corpus the entry rests on.

    Evidence a reviewer reads, and deliberately **nothing more**: no verdict, no exit code and
    no refusal reads it. Gating on it would infer an unbounded safety property from a finite
    corpus, which is the inference this bar is built to refuse — the freed-share rule is
    therefore satisfied by *declaring* a map, and this is where the declared map is measured.

    ``weights_declared`` is ``None`` where the entry declares no map, which the freed-share rule
    permits only for a criterion carrying no score share. The per-trial *after* map is then the
    map that trial was graded under, since nothing was freed to move.
    """

    weights_declared: dict[str, float] | None
    trials: list[TrialCounterfactual]
    unrecomputed_trials: list[UnrecomputedTrial]

    model_config = {"extra": "forbid"}


class UnreadableTrial(BaseModel):
    """A bundle that could not be read or re-checked, named with the defect.

    Distinct from an exclusion: an excluded trial was read and had nothing to contribute,
    while this one could not be read, which is a defect in the corpus rather than a
    statement about the criterion.
    """

    trial: str
    reason: str

    model_config = {"extra": "forbid"}


class CorpusExclusion(BaseModel):
    """A discovered bundle that is no part of the corpus, and why it is not.

    A trial the substrate killed before it ran records no ``task.yaml``, so nothing
    names the pack whose migration it could speak to. It is neither an
    :class:`UnreadableTrial` — nothing about it is broken — nor an
    :class:`ExcludedTrial`, which is a statement about one entry's label pool. It
    never blocks: a corpus is not defective for containing a trial that never
    happened.
    """

    bundle: str
    reason: str

    model_config = {"extra": "forbid"}


class ReconciledEntry(BaseModel):
    """One declared migration, measured against the corpus.

    ``mode`` and the residual claim are rendered exactly as declared. There is deliberately
    no field ranking or comparing modes: on a corpus with zero disagreements ``narrowed`` and
    ``retired`` satisfy the same condition, so the evidence *cannot* choose between them and
    a field suggesting it had would be read as a recommendation.

    ``residual`` is the declaration's own block rather than its two fields flattened, because a
    kind and a reason are only meaningful together: a reason with no kind names something
    without saying whether it survives, and the pair is absent exactly for a ``candidate``.
    """

    task_ids: list[str]
    criterion: str
    mode: MigrationMode
    residual: MigrationResidual | None
    by: list[str]
    observations: int
    contingency: ContingencyTable
    accuracy: float | None
    kappa: float | None
    strict_disagreements: list[DisagreementRow]
    permissive_disagreements: list[DisagreementRow]
    excluded_trials: list[ExcludedTrial]
    counterfactual: MigrationCounterfactual
    verdict: ReconcileVerdict
    refusals: list[Refusal]

    model_config = {"extra": "forbid"}

    @property
    def gates_the_exit_code(self) -> bool:
        """Whether this entry's verdict decides the command's exit code.

        A ``candidate`` retires nothing, converts nothing and changes no grade — it records
        an intention to be measured later — so its verdict is reported and gates nothing.
        """
        return self.mode is not MigrationMode.CANDIDATE


class ReconcileReport(BaseModel):
    """Every declared migration the corpus under ``source`` could speak to.

    ``replay_id`` is the name this reconciliation is filed under — the artifact
    subdirectory a written report lands in. It is ``None`` for a reconciliation that takes
    no name because it can write nothing: the declaration sweep reads committed corpora,
    and a report inside one would dirty the tree.
    """

    source: str
    replay_id: str | None
    packs_searched: list[str]
    reference_labeller: str
    candidate_labeller: str
    trials_read: int
    entries: list[ReconciledEntry]
    unreadable_trials: list[UnreadableTrial]
    excluded_bundles: list[CorpusExclusion]

    model_config = {"extra": "forbid"}

    @property
    def blocking(self) -> tuple[str, ...]:
        """Every reason this reconciliation is not a clean pass, in the order found.

        Empty is what exit ``0`` means, and it means one thing: every converting entry
        reached ``no_counter_evidence`` and every bundle that is part of the corpus was
        readable. An excluded bundle is not: a trial that never ran is reported, not a
        defect.
        """
        blocking = [f"{trial.trial}: {trial.reason}" for trial in self.unreadable_trials]
        blocking.extend(
            f"{entry.criterion} ({entry.mode.value}): {entry.verdict.value}"
            for entry in self.entries
            if entry.gates_the_exit_code
            and entry.verdict is not ReconcileVerdict.NO_COUNTER_EVIDENCE
        )
        return tuple(blocking)


@dataclass(frozen=True)
class RecordedTrialVerdict:
    """One bundle's own grade, the config it was graded under, and what the replay recomputed.

    ``grading_config`` and ``grade`` are read from the bundle rather than resolved from the
    pack: the pack holds the *post*-migration state, so only the bundle records what this trial
    scored and under which weight map. ``gate_constraint_ids`` are those of the entry's ``by``
    constraints the recomputation carried as ``severity: gate`` — the vetoes the migration hands
    to the trace side.
    """

    grading_config: Mapping[str, Any]
    grade: Mapping[str, Any]
    trace_component: float
    trace_gate_failed: bool
    gate_constraint_ids: frozenset[str]


@dataclass(frozen=True)
class Unavailable:
    """Why one side of a trial's label pair has no verdict to compare.

    The pair a trial contributes is all-or-nothing, so the reason it contributes none is one
    value rather than an exclusion class and a sentence travelling together: every producer
    returns it whole and :class:`ExcludedTrial` is built from it in one place.
    """

    exclusion: TrialExclusion
    reason: str


@dataclass(frozen=True)
class TrialEvidence:
    """What one recorded trial contributes to one declared entry.

    ``recorded_criterion`` is the criterion as *this bundle's* rubric recorded it — the only
    surviving record of the pre-migration shape — or ``None`` where that rubric held no such
    criterion. ``judge_met`` / ``constraint_passed`` are the pair of labels, and
    ``unavailable`` replaces them where one side has no verdict to compare: exactly one of
    the two states holds, because a trial that is both labelled and unlabelled would be
    counted in one place and excluded in another.

    ``recorded_verdict`` is required of a labelled trial and of no other: a trial that
    contributes an observation is a trial the counterfactual must be able to speak about, so
    omitting it would empty the counterfactual silently rather than reporting a gap.
    """

    trial: str
    recorded_criterion: MigratedCriterion | None = None
    judge_met: bool | None = None
    constraint_passed: bool | None = None
    justification: str = ""
    unavailable: Unavailable | None = None
    recorded_verdict: RecordedTrialVerdict | None = None

    def __post_init__(self) -> None:
        if self.labelled is not (self.unavailable is None):
            raise ValueError(
                f"trial {self.trial} carries "
                + (
                    "both a label pair and a reason it has none"
                    if self.labelled
                    else "neither a label pair nor a reason it has none"
                )
                + ". A trial either contributes an observation or names why it does not"
            )
        if self.labelled and self.recorded_verdict is None:
            raise ValueError(
                f"trial {self.trial} contributes an observation with no recorded_verdict, so "
                "the counterfactual would silently omit it instead of reporting a gap. Pass "
                "the grade and config the bundle recorded"
            )

    @property
    def labelled(self) -> bool:
        return self.judge_met is not None and self.constraint_passed is not None


_FORBIDDEN_DIRECTIONS: Mapping[MigrationMode, frozenset[DisagreementDirection]] = MappingProxyType(
    {
        # A candidacy claims the constraint could stand in for the criterion, so a strict
        # disagreement is counter-evidence against it too; the permissive ones are the
        # reason a candidacy exists to be measured rather than assumed.
        MigrationMode.CANDIDATE: frozenset({DisagreementDirection.STRICT}),
        MigrationMode.NARROWED: frozenset({DisagreementDirection.STRICT}),
        MigrationMode.RETIRED: frozenset(DisagreementDirection),
    }
)
"""Total over the modes, so a new one has to state what it tolerates rather than tolerate all."""


def _direction(judge_met: bool, constraint_passed: bool) -> DisagreementDirection:
    return DisagreementDirection.STRICT if judge_met else DisagreementDirection.PERMISSIVE


def _recorded_shape_refusal(trial: TrialEvidence, entry: MigrationEntry) -> Refusal | None:
    """Where the rubric one bundle recorded contradicts what ``was`` claims.

    The full block — ``kind``, ``required``, ``weight``, ``description`` — because every other
    rule keys on ``was`` and the pack holds only the post-migration state: an author who flips
    the declaration and the pack's criterion in one commit satisfies every load-time check,
    and this is the source that does not move with them.
    """
    recorded = trial.recorded_criterion
    if recorded is None:
        return None
    written = criterion_shape_disagreement(entry.was, recorded, over=EVERY_DECLARED_FIELD)
    if written is None:
        return None
    return Refusal(
        kind=RefusalKind.RECORDED_RUBRIC_CONTRADICTS_WAS,
        message=(
            f"{trial.trial}: was does not match the rubric this bundle recorded ({written}). "
            "was claims the criterion's pre-migration shape and the bundle is the only "
            "surviving record of it, so either was is wrong or "
            f"{_CORPUS_RECORDS_THE_POST_MIGRATION_RUBRIC}. Correct was to what the bundles "
            "recorded, or keep the bundles the migration was measured on"
        ),
    )


def _straddling_revision_refusal(trials: Sequence[TrialEvidence]) -> Refusal | None:
    """Two bundles recording different shapes for one criterion have no one ``was`` to check.

    Mirrors the rule replay already applies to two bundles claiming one task while declaring
    different constraint blocks, rather than inventing a second answer for the same
    situation: the corpus straddles a pack revision and the fix is to split it.

    Every recorded shape is compared against the first one through the same comparison the
    declaration is checked with — used here as a predicate, its wording addressing an author's
    ``was`` rather than two bundles — so a field added to ``was`` is straddle-checked without
    being listed again here, and a description that differs only in its wrapping is not a
    straddle.
    """
    known = [
        (trial.trial, trial.recorded_criterion)
        for trial in trials
        if trial.recorded_criterion is not None
    ]
    if not known:
        return None
    first, reference = known[0]
    for name, shape in known[1:]:
        if criterion_shape_disagreement(reference, shape, over=EVERY_DECLARED_FIELD) is None:
            continue
        return Refusal(
            kind=RefusalKind.CORPUS_STRADDLES_A_PACK_REVISION,
            message=(
                f"{first} and {name} record different shapes for the criterion, so the corpus "
                "straddles a pack revision and no single 'was' describes what the evidence was "
                "gathered against. Reconcile each revision's bundles on their own, or drop the "
                "ones from the other revision"
            ),
        )
    return None


def _trial_keys(trial: str) -> tuple[str, ...]:
    """Both spellings one trial path answers to, so a waiver matches it either way.

    A declaration writes ``acknowledged.trial`` from the repository root while ``--source``
    may be given as an absolute path, and a waiver that fails to match its own disagreement
    reads as *stale* — a refusal for something the author wrote correctly.
    """
    path = Path(trial)
    return (str(path), str(path.resolve()))


def _acknowledged_reasons(entry: MigrationEntry) -> dict[str, str]:
    """Each waived trial's reason, under every spelling of the path the waiver named."""
    return {
        key: waiver.reason for waiver in entry.acknowledged for key in _trial_keys(waiver.trial)
    }


def _disagreement_row(trial: TrialEvidence, waivers: Mapping[str, str]) -> DisagreementRow:
    waived = (waivers.get(key) for key in _trial_keys(trial.trial))
    return DisagreementRow(
        trial=trial.trial,
        judge_met=bool(trial.judge_met),
        constraint_passed=bool(trial.constraint_passed),
        justification=trial.justification,
        acknowledged_reason=next((reason for reason in waived if reason is not None), None),
    )


def _stale_acknowledgement_refusals(
    entry: MigrationEntry, disagreeing: frozenset[str] | set[str]
) -> list[Refusal]:
    """A waiver naming a trial that no longer disagrees, or names no trial in the corpus.

    An error rather than a silent pass: a waiver outliving the disagreement it waived is a
    standing licence, and the next disagreement on that trial would be waived unread.
    """
    return [
        Refusal(
            kind=RefusalKind.STALE_ACKNOWLEDGEMENT,
            message=(
                f"acknowledged names {waiver.trial}, which contributed no disagreement to "
                "this reconciliation — the trial agrees, contributed no observation, or is "
                "not under --source. A waiver whose disagreement is gone waives the next "
                "one unread: drop it, or point it at the trial that disagrees"
            ),
        )
        for waiver in entry.acknowledged
        if disagreeing.isdisjoint(_trial_keys(waiver.trial))
    ]


def _direction_refusal(
    entry: MigrationEntry, rows: Sequence[DisagreementRow], direction: DisagreementDirection
) -> Refusal | None:
    unwaived = [row.trial for row in rows if row.acknowledged_reason is None]
    if not unwaived or direction not in _FORBIDDEN_DIRECTIONS[entry.mode]:
        return None
    return Refusal(
        kind=RefusalKind.UNACKNOWLEDGED_DISAGREEMENT,
        message=(
            f"mode: {entry.mode.value} does not tolerate a {direction.value} disagreement, "
            f"and {unwaived} carry one unacknowledged. A {direction.value} disagreement is "
            + (
                "the judge finding the criterion met where the constraint failed, so the "
                "constraint is not even a necessary condition of it"
                if direction is DisagreementDirection.STRICT
                else "the judge finding the criterion not met where the constraint passed, "
                "so something the criterion asked for is no longer checked"
            )
            + ". Waive each one in 'acknowledged' with the reason the judge's verdict is the "
            "one to discount, or declare the mode the evidence supports"
        ),
    )


def _contingency(trials: Sequence[TrialEvidence]) -> ContingencyTable:
    counted = {
        (judge, passed): sum(
            1 for t in trials if t.judge_met is judge and t.constraint_passed is passed
        )
        for judge in (True, False)
        for passed in (True, False)
    }
    return ContingencyTable(
        judge_met_constraint_passed=counted[(True, True)],
        judge_met_constraint_failed=counted[(True, False)],
        judge_not_met_constraint_passed=counted[(False, True)],
        judge_not_met_constraint_failed=counted[(False, False)],
    )


def _exclusion(trial: TrialEvidence, entry: MigrationEntry) -> ExcludedTrial | None:
    """Why this trial contributes nothing, or ``None`` where it contributes a pair.

    The recorded rubric is asked *first*, and only of an entry carrying evidence. First,
    because a bundle whose rubric never held the criterion also records no verdict for it,
    and the operator needs the provenance answer rather than the missing-verdict one. Only of
    an evidence-carrying entry, because a candidate's ``was`` is already checked against the
    criterion the pack still holds, which is the same shape by that rule.
    """
    if entry.evidence is not None and trial.recorded_criterion is None:
        return ExcludedTrial(
            trial=trial.trial,
            exclusion=TrialExclusion.CRITERION_ABSENT_FROM_RECORDED_RUBRIC,
            reason=(
                f"the rubric this bundle recorded declares no criterion "
                f"{entry.criterion!r}, so the bundle cannot say what shape the evidence was "
                "gathered against. Where every bundle is excluded for this, "
                f"{_CORPUS_RECORDS_THE_POST_MIGRATION_RUBRIC}"
            ),
        )
    if trial.unavailable is None:
        return None
    return ExcludedTrial(
        trial=trial.trial,
        exclusion=trial.unavailable.exclusion,
        reason=trial.unavailable.reason,
    )


_DisagreementRows = Mapping[DisagreementDirection, list[DisagreementRow]]


def _disagreement_rows(
    entry: MigrationEntry, contributing: Sequence[TrialEvidence]
) -> _DisagreementRows:
    """Every trial the two labellers differed on, split by which way it went."""
    waivers = _acknowledged_reasons(entry)
    return {
        direction: [
            _disagreement_row(trial, waivers)
            for trial in contributing
            if trial.judge_met is not trial.constraint_passed
            and _direction(bool(trial.judge_met), bool(trial.constraint_passed)) is direction
        ]
        for direction in DisagreementDirection
    }


def _recorded_rubric_refusals(
    entry: MigrationEntry, contributing: Sequence[TrialEvidence]
) -> list[Refusal]:
    """What the rubric the corpus recorded says about the entry's ``was``.

    Only for an entry carrying evidence: a candidate's ``was`` is already checked against the
    criterion the pack still holds, which is the same shape by that rule.
    """
    if entry.evidence is None:
        return []
    refusals = [
        found
        for trial in contributing
        if (found := _recorded_shape_refusal(trial, entry)) is not None
    ]
    straddling = _straddling_revision_refusal(contributing)
    return refusals if straddling is None else [*refusals, straddling]


#: κ is compared with the declaration at the precision the report prints it. Three decimals is
#: what the console shows and therefore what an author copies into ``evidence.kappa``, so
#: comparing past it would refuse a number that was copied correctly.
_DECLARED_KAPPA_PRECISION = 3


def _same_kappa(declared: float | None, measured: float | None) -> bool:
    """Whether two readings of κ agree, undefined included, at the precision it is reported."""
    if declared is None or measured is None:
        return declared is measured
    return round(declared, _DECLARED_KAPPA_PRECISION) == round(measured, _DECLARED_KAPPA_PRECISION)


def _declared_evidence_refusal(
    entry: MigrationEntry,
    *,
    observations: int,
    kappa: float | None,
    over_the_declared_corpus: bool,
) -> Refusal | None:
    """Where the numbers the entry declares contradict the ones this run measured.

    ``evidence`` is what a reviewer reads *instead of* re-running the command, so it is the
    measurement or it is nothing. How far below the declared count is charged depends on what
    was read: ``evidence.observations`` counts the whole corpus the entry names, and a
    ``--source`` may deliberately be a part of it — pointing at one arm of a two-arm corpus is
    how each half is shown to be the other's falsifier, and refusing that invocation would turn
    a diagnostic into an authoring error. So over an arbitrary source the rule is a **bound**: a
    run measuring fewer observations says nothing. Over the corpus the entry *itself* names
    (``over_the_declared_corpus``) it is an **equality**, because there is no subset to allow
    for and a count below the declared one means bundles went missing.

    Either way a run measuring **more** has read a corpus the declaration under-counts, and one
    reaching the declared count must reproduce the declared κ.
    """
    declared = entry.evidence
    if declared is None:
        return None
    if observations < declared.observations:
        if not over_the_declared_corpus:
            return None
        return Refusal(
            kind=RefusalKind.DECLARED_EVIDENCE_CONTRADICTS_MEASUREMENT,
            message=(
                f"evidence.observations declares {declared.observations} and this "
                f"reconciliation measured {observations} over {entry.corpus}, the corpus the "
                "entry itself names. There is no part of the corpus left out for the count to "
                "be short of, so the bundles the declaration was written against are gone: "
                "restore them, or re-run reconcile over the corpus and write what it reports"
            ),
        )
    if observations > declared.observations:
        return Refusal(
            kind=RefusalKind.DECLARED_EVIDENCE_CONTRADICTS_MEASUREMENT,
            message=(
                f"evidence.observations declares {declared.observations} and this "
                f"reconciliation measured {observations}, so the corpus carries more evidence "
                "than the declaration was written against and the numbers a reviewer reads are "
                f"not the ones the command reaches. Re-run reconcile over {entry.corpus} and "
                "write what it reports"
            ),
        )
    if _same_kappa(declared.kappa, kappa):
        return None
    return Refusal(
        kind=RefusalKind.DECLARED_EVIDENCE_CONTRADICTS_MEASUREMENT,
        message=(
            f"evidence.kappa declares {declared.kappa} and this reconciliation measured "
            f"{kappa} over the {declared.observations} observations the declaration claims. A "
            "reviewer reads the declared evidence instead of re-running the command, so it is "
            f"the measurement or it is nothing. Re-run reconcile over {entry.corpus} and "
            "write the kappa it reports"
        ),
    )


def _direction_and_waiver_refusals(
    entry: MigrationEntry, contributing: Sequence[TrialEvidence], rows: _DisagreementRows
) -> list[Refusal]:
    """The direction rule and the anti-rot rule that keeps a waiver from outliving its cause."""
    disagreeing = {
        key
        for trial in contributing
        if trial.judge_met is not trial.constraint_passed
        for key in _trial_keys(trial.trial)
    }
    return [
        *(
            found
            for direction in DisagreementDirection
            if (found := _direction_refusal(entry, rows[direction], direction)) is not None
        ),
        *_stale_acknowledgement_refusals(entry, disagreeing),
    ]


#: A recorded score crossed a YAML round trip before it was read back, so the reproduction is
#: compared to it at the precision that survives that trip rather than bit for bit.
_REPRODUCTION_TOLERANCE = 1e-9

_A_COMPOSED_COMPONENT = (
    "the recorded grade carries a {name} component, which the runner folds from several "
    "sources and no single runner field holds, so the recorded verdict cannot be recomposed "
    "from it and neither can the counterfactual"
)
_THE_REPRODUCTION_DIVERGED = (
    "recomposing the recorded verdict gives judge={judge}, score={score}, pass={passed} where "
    "the bundle recorded judge={recorded_judge}, score={recorded_score}, pass={recorded_pass}. "
    "A composition that cannot reproduce what the runner decided says nothing about what the "
    "migration would decide"
)
_A_WEIGHTLESS_COMPONENT = (
    "the {column} column folds a component the weight map it is folded under does not weight, "
    "so any verdict reported for it could only come from an invented share: {detail}"
)


def _recorded_judge_verdicts(grade: Mapping[str, Any]) -> list[CriterionResult]:
    """The judge's per-criterion rows as one bundle's ``grade.yaml`` recorded them.

    One expression for both readers — the per-bundle refusal that proves a grade readable
    and the counterfactual that recomposes from it — so what the net admits is exactly what
    the recomposition later reads.
    """
    return [CriterionResult(**row) for row in grade.get("criterion_results") or ()]


def _refuse_a_recorded_grade_the_counterfactual_cannot_read(
    bundle: Path, grade: Mapping[str, Any] | None
) -> None:
    """Refuse a malformed ``grade.yaml`` here, where it costs one trial and no other.

    The counterfactual recomposes a contributing trial's verdict out of the grade and checks
    it against what the bundle recorded, reading the judge's rows and two numbers with no net
    of its own — and it runs once per *pooled entry*, long after this bundle stopped being the
    thing the batch is doing. A shape it cannot read reaching it aborts the whole
    reconciliation under a raw traceback, taking every other entry's report with it.
    """
    if grade is None:
        return
    components = grade.get("components") or {}
    if not isinstance(components, Mapping):
        raise MissingTraceReplayInputError(
            f"{bundle / 'grade.yaml'} holds {type(components).__name__} where the component "
            "scores belong, so nothing says what this trial scored"
        )
    for where, value in (("score", grade.get("score")), *sorted(components.items())):
        if value is None or isinstance(value, (int, float)):
            continue
        raise MissingTraceReplayInputError(
            f"{bundle / 'grade.yaml'} records {where} as {value!r}, which is not a number, so "
            "the recomposition has nothing to check itself against"
        )
    try:
        _recorded_judge_verdicts(grade)
    except (TypeError, ValidationError) as exc:
        raise MissingTraceReplayInputError(
            f"{bundle / 'grade.yaml'} records a criterion_results entry that does not read as "
            f"a judge verdict, so nothing says what the judge concluded on this trial: {exc}"
        ) from exc


def _runner_components(recorded: Mapping[str, Any]) -> dict[str, float]:
    """A recorded grade's components under the field names the runner's fold reads.

    Every component but the judge's, whose score the caller supplies: the *before* column feeds
    the judge's own recorded aggregate and the *after* column the reduced one.

    Raises:
        KeyError: from :func:`runner_score_field`, naming a composed component the recorded
            grade carries. ``state_checks`` has no single runner field, so its recorded value
            cannot be routed back through the fold that produced it.
    """
    lowered: dict[str, float] = {}
    for spec in GRADE_COMPONENTS:
        value = recorded.get(spec.core_field)
        if value is None or spec.name == "llm_judge":
            continue
        lowered[runner_score_field(spec.name)] = float(value)
    return lowered


#: The fold reads a component's config section only to tell a *configured* component from a
#: merely weighted one, so the ``trace_checks`` block the migration introduces is represented by
#: its presence alone: the recorded config carries none, the migration adds one, and no field of
#: it is ever read.
_THE_MIGRATION_DECLARES_TRACE_CHECKS: Mapping[str, Any] = MappingProxyType({})


def _flat_grading_config(
    recorded_config: Mapping[str, Any], *, weights: Mapping[str, float], judge_scored: bool
) -> dict[str, Any]:
    """The recorded config in the flat shape the runner's fold takes it in.

    The recorded block nests ``combine``; the runner's own model carries ``combine_method`` /
    ``weights`` / ``pass_threshold`` at the top. ``pass_threshold`` is carried only where the
    recorded block declares one, so the fold applies its own default rather than one invented
    here. The config sections ride along for the *configured* check above.
    """
    combine = recorded_config.get("combine") or {}
    flat: dict[str, Any] = {
        "combine_method": combine.get("method", "weighted"),
        "weights": dict(weights),
    }
    if "pass_threshold" in combine:
        flat["pass_threshold"] = combine["pass_threshold"]
    for spec in GRADE_COMPONENTS:
        flat[spec.config_section] = recorded_config.get(spec.config_section)
    if not judge_scored:
        flat[COMPONENT_BY_NAME["llm_judge"].config_section] = None
    return flat


def _judge_component_of(
    rubric: Rubric, results: Sequence[CriterionResult]
) -> tuple[float | None, bool]:
    """The judge's aggregate over ``rubric``, and whether its required gate failed.

    An empty rubric is the judge component *not evaluated* — ``None`` — rather than a free
    ``1.0``: retiring a pack's last criterion deletes the judge, and folding in the aggregate's
    vacuous pass would hand the trial a component nothing scored.
    """
    if not rubric.criteria:
        return None, False
    aggregate = aggregate_rubric(rubric, list(results))
    return aggregate.score, aggregate.gate_failed


def _reproduces_the_recorded_verdict(
    recorded: RecordedTrialVerdict, verdict: RunnerTrialVerdict
) -> str | None:
    """``None`` where the recomposition matches the bundle's grade, else how it diverged."""
    grade = recorded.grade
    recorded_judge = (grade.get("components") or {}).get(COMPONENT_BY_NAME["llm_judge"].core_field)
    same = (
        recorded_judge is not None
        and isclose(verdict.judge_component, recorded_judge, abs_tol=_REPRODUCTION_TOLERANCE)
        and isclose(verdict.score, grade.get("score", -1.0), abs_tol=_REPRODUCTION_TOLERANCE)
        and verdict.binary_pass is bool(grade.get("binary_pass"))
    )
    if same:
        return None
    return _THE_REPRODUCTION_DIVERGED.format(
        judge=verdict.judge_component,
        score=verdict.score,
        passed=verdict.binary_pass,
        recorded_judge=recorded_judge,
        recorded_score=grade.get("score"),
        recorded_pass=grade.get("binary_pass"),
    )


def _composed_column(
    trial: str,
    column: str,
    components: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    judge_gate_failed: bool,
    trace_gate_failed: bool,
) -> RunnerTrialVerdict | UnrecomputedTrial:
    """One column's verdict, or the gap a map that does not weight what it folds leaves.

    The *after* column installs ``trace_checks`` as a scored component, and an entry
    declaring no weight map is folded under the map the trial recorded — which cannot weight
    a check the migration has not made yet. Folding it at an invented share would put the
    very defect this report exists to measure inside the number a reviewer reads, so the row
    states the missing key instead.
    """
    try:
        return compose_runner_trial_verdict(
            dict(components),
            dict(config),
            judge_gate_failed=judge_gate_failed,
            trace_gate_failed=trace_gate_failed,
        )
    except MissingComponentWeight as exc:
        return UnrecomputedTrial(
            trial=trial,
            gap=RecomputationGap.FOLDED_COMPONENT_HAS_NO_DECLARED_WEIGHT,
            reason=_A_WEIGHTLESS_COMPONENT.format(column=column, detail=exc),
        )


def _scored(field: str, score: float | None) -> dict[str, float]:
    """The component under ``field``, or nothing where the component was not evaluated.

    An unevaluated judge is *absent* from the fold rather than present at a sentinel: the fold
    weights every component it is handed, so a placeholder would be averaged in as a score.
    """
    return {} if score is None else {field: score}


_VETO_SURVIVES_THE_MIGRATION: Mapping[MigrationMode, bool] = MappingProxyType(
    {
        # A candidacy's counterfactual is the projection of the conversion it is a candidate
        # *for*, which is a full retirement — it is the measurement the candidacy is waiting
        # for, and projecting a narrow would need a narrowed text the entry does not carry.
        MigrationMode.CANDIDATE: False,
        MigrationMode.NARROWED: True,
        MigrationMode.RETIRED: False,
    }
)
"""Total over the modes, so a new one states whether its criterion still vetoes afterwards."""


def _vetoes_after(entry: MigrationEntry, rubric: Rubric) -> set[str]:
    """The rubric-side vetoes the migration leaves standing.

    A narrow leaves the criterion in the rubric, still ``required: true`` — the shipped narrow's
    whole shape — so an *after* set omitting it would report the veto as lost where the pack
    keeps it, defeating the purpose of stating the veto set at all. A retirement removes the
    criterion, so its veto goes and the trace gate is what holds one.
    """
    vetoes = {item.id for item in rubric.criteria if item.required}
    if not _VETO_SURVIVES_THE_MIGRATION[entry.mode]:
        vetoes.discard(entry.criterion)
    return vetoes


def _counterfactual_for_trial(
    entry: MigrationEntry, trial: TrialEvidence
) -> TrialCounterfactual | UnrecomputedTrial:
    """One trial's before/after pair, or the named gap that stopped it.

    The *before* pair is recomposed from what the bundle recorded and checked against the
    verdict the bundle carries, so a divergence is reported rather than presented as a
    baseline. The *after* pair drops the migrated criterion from the judge's side, takes the
    replay's trace component in its place, and folds under the map the entry **declares** —
    never the map the pack holds today, which is the post-migration state the report exists to
    let a reviewer judge.
    """
    recorded = trial.recorded_verdict
    if recorded is None:
        raise ValueError(
            f"trial {trial.trial} has no recorded verdict to compare a counterfactual against. "
            "Only a trial that contributed an observation has one, and only those are passed "
            "here"
        )
    try:
        carried = _runner_components(recorded.grade.get("components") or {})
    except KeyError as exc:
        return UnrecomputedTrial(
            trial=trial.trial,
            gap=RecomputationGap.COMPOSED_COMPONENT_HAS_NO_RUNNER_FIELD,
            reason=_A_COMPOSED_COMPONENT.format(name=exc.args[0]),
        )

    judge_field = runner_score_field("llm_judge")
    trace_field = runner_score_field("trace_checks")
    trace_section = COMPONENT_BY_NAME["trace_checks"].config_section
    rubric = _recorded_rubric(recorded.grading_config)
    results = _recorded_judge_verdicts(recorded.grade)
    weights_before = dict((recorded.grading_config.get("combine") or {}).get("weights") or {})

    before_score, before_gate = _judge_component_of(rubric, results)
    before = _composed_column(
        trial.trial,
        "before",
        {**carried, **_scored(judge_field, before_score)},
        _flat_grading_config(
            recorded.grading_config, weights=weights_before, judge_scored=bool(rubric.criteria)
        ),
        judge_gate_failed=before_gate,
        trace_gate_failed=False,
    )
    if isinstance(before, UnrecomputedTrial):
        return before
    if (diverged := _reproduces_the_recorded_verdict(recorded, before)) is not None:
        return UnrecomputedTrial(
            trial=trial.trial, gap=RecomputationGap.RECOMPUTED_VERDICT_DIVERGES, reason=diverged
        )

    reduced = Rubric(
        criteria=[item for item in rubric.criteria if item.id != entry.criterion],
        reference=rubric.reference,
    )
    after_score, after_gate = _judge_component_of(
        reduced, [row for row in results if row.id != entry.criterion]
    )
    weights_after = dict(entry.combine_weights or weights_before)
    after = _composed_column(
        trial.trial,
        "after",
        {**carried, **_scored(judge_field, after_score), trace_field: recorded.trace_component},
        _flat_grading_config(
            {**recorded.grading_config, trace_section: _THE_MIGRATION_DECLARES_TRACE_CHECKS},
            weights=weights_after,
            judge_scored=bool(reduced.criteria),
        ),
        judge_gate_failed=after_gate,
        trace_gate_failed=recorded.trace_gate_failed,
    )
    if isinstance(after, UnrecomputedTrial):
        return after
    return TrialCounterfactual(
        trial=trial.trial,
        weights_before=weights_before,
        weights_after=weights_after,
        vetoes_before=sorted(item.id for item in rubric.criteria if item.required),
        vetoes_after=sorted(_vetoes_after(entry, rubric) | recorded.gate_constraint_ids),
        judge_component_before=before.judge_component,
        judge_component_after=None if after_score is None else after.judge_component,
        score_before=before.score,
        score_after=after.score,
        binary_pass_before=before.binary_pass,
        binary_pass_after=after.binary_pass,
    )


def migration_counterfactual(
    entry: MigrationEntry, trials: Sequence[TrialEvidence]
) -> MigrationCounterfactual:
    """What the entry's declared map would have done to each trial that contributed one.

    Computed for every mode, a ``candidate`` included: a candidacy exists to be measured, and
    this is the measurement it is waiting for. Nothing here decides anything — see
    :class:`MigrationCounterfactual`.
    """
    computed = [_counterfactual_for_trial(entry, trial) for trial in trials]
    return MigrationCounterfactual(
        weights_declared=dict(entry.combine_weights) if entry.combine_weights else None,
        trials=[row for row in computed if isinstance(row, TrialCounterfactual)],
        unrecomputed_trials=[row for row in computed if isinstance(row, UnrecomputedTrial)],
    )


def reconcile_entry(
    entry: MigrationEntry,
    *,
    task_ids: Sequence[str],
    trials: Sequence[TrialEvidence],
    over_the_declared_corpus: bool = False,
) -> ReconciledEntry:
    """Measure one declared migration against the trials that can speak to it.

    Pure: every rule of the bar is decided here, from what each bundle recorded. The verdict
    is ``refused`` where anything about the declaration is contradicted, else
    ``insufficient_evidence`` where κ is undefined, else ``no_counter_evidence``. Refusal
    wins over undefined κ because a contradicted declaration is an authoring defect, and
    reporting it as thin evidence would send the author to collect more trials.

    ``over_the_declared_corpus`` says the trials came from the corpus the entry itself names
    rather than from a source somebody pointed at, which is what turns the declared-evidence
    bound into an equality.
    """
    excluded = [found for trial in trials if (found := _exclusion(trial, entry)) is not None]
    excluded_trials = {row.trial for row in excluded}
    contributing = [trial for trial in trials if trial.trial not in excluded_trials]

    rows = _disagreement_rows(entry, contributing)
    observations = [
        CriterionObservation(
            observation_id=trial.trial,
            criterion_id=entry.criterion,
            reference_met=bool(trial.judge_met),
            candidate_met=bool(trial.constraint_passed),
            reference_raw=trial.judge_met,
            candidate_raw=trial.constraint_passed,
            justification=trial.justification,
        )
        for trial in contributing
    ]
    kappa = cohen_kappa(observations)
    declared = _declared_evidence_refusal(
        entry,
        observations=len(observations),
        kappa=kappa,
        over_the_declared_corpus=over_the_declared_corpus,
    )
    refusals = [
        *_recorded_rubric_refusals(entry, contributing),
        *_direction_and_waiver_refusals(entry, contributing, rows),
        *([] if declared is None else [declared]),
    ]
    return ReconciledEntry(
        task_ids=sorted(set(task_ids)),
        criterion=entry.criterion,
        mode=entry.mode,
        residual=entry.residual,
        by=list(entry.by),
        observations=len(observations),
        contingency=_contingency(contributing),
        accuracy=accuracy(observations) if observations else None,
        kappa=kappa,
        strict_disagreements=rows[DisagreementDirection.STRICT],
        permissive_disagreements=rows[DisagreementDirection.PERMISSIVE],
        excluded_trials=excluded,
        counterfactual=migration_counterfactual(entry, contributing),
        verdict=_verdict(refusals, kappa),
        refusals=refusals,
    )


def _verdict(refusals: Sequence[Refusal], kappa: float | None) -> ReconcileVerdict:
    if refusals:
        return ReconcileVerdict.REFUSED
    if kappa is None:
        return ReconcileVerdict.INSUFFICIENT_EVIDENCE
    return ReconcileVerdict.NO_COUNTER_EVIDENCE


# ---------------------------------------------------------------------------
# Reading a corpus and the packs its trials name
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResolvedPack:
    """The pack one ``task_id`` resolved to, and what it declares about its rubric."""

    task_id: str
    grading_path: Path
    entries: tuple[MigrationEntry, ...]
    trace_checks: Mapping[str, Any]


def _declaring_task_files(task_id: str, roots: Sequence[Path]) -> list[Path]:
    """Every ``task.yaml`` under ``roots`` declaring ``task_id``.

    The field is read raw rather than through the task loader, for two measured reasons: a
    tree holds packs that do not load at all (``tolokaforge validate`` reports two under
    ``examples/``), and one of those must not make an unrelated id unresolvable; and loading
    every task to find one is the whole tree's cost for one lookup. The selected file is then
    loaded properly, so what a pack's ``grading:`` resolves to is the loader's answer.
    """
    return [
        task_file
        for root in roots
        for task_file in sorted(Path(root).rglob("task.yaml"))
        if _declares_task_id(task_file, task_id)
    ]


def _declares_task_id(task_file: Path, task_id: str) -> bool:
    declared = yaml.safe_load(task_file.read_text(encoding="utf-8"))
    return isinstance(declared, Mapping) and declared.get("task_id") == task_id


def _grading_path_of(task_id: str, roots: Sequence[Path]) -> Path:
    """The ``grading.yaml`` of the one pack declaring ``task_id``, or a refusal naming both.

    Zero and two are the same defect from the operator's side — the search root is wrong —
    and both are named with the roots searched, because a corpus recorded against one tree
    reconciled against another is otherwise silently reconciled against nothing.

    A pack that resolves and has no grading block on disk — naming none, or naming a path
    with no file at it — is refused too, under every declared adapter, unlike
    ``tolokaforge validate``, which passes such a pack where its declared adapter resolves
    its own grading config: a config resolved that way carries no migration declaration and
    no ``trace_checks`` block for a recorded verdict to be recomputed from.
    """
    written = ", ".join(str(root) for root in roots)
    found = _declaring_task_files(task_id, roots)
    if not found:
        raise ReconcileError(
            f"no pack under {written} declares task_id {task_id!r}, which a bundle under "
            "--source records, so nothing says which constraints its verdicts are compared "
            "against. Point --packs at the tree the corpus was recorded from"
        )
    if len(found) > 1:
        raise ReconcileError(
            f"{len(found)} packs under {written} declare task_id {task_id!r} "
            f"({', '.join(str(path) for path in found)}), so a bundle recording it resolves "
            "to no single pack. Give each testcase its own task_id, or search one root"
        )
    task_config, task_dir = load_task_yaml(found[0])
    adapter_type = task_config.adapter_type
    source = grading_source_under_adapter(task_config, task_dir, adapter_type)
    if source.path is None:
        raise ReconcileError(
            f"{found[0]} declares task_id {task_id!r} and no grading block is on disk for it, "
            "so a corpus recording this task's verdicts has nothing to reconcile them "
            "against: the migration is declared beside that file, and the trace_checks block "
            "it names is what every recorded verdict is recomputed from. That holds under "
            f"every declared adapter, {adapter_type!r} included: one that grades from the "
            "file has none to read, and one that resolves its own grading config writes "
            "neither. Point `grading:` at the block this pack grades by, or add a "
            "grading.yaml beside its task.yaml"
        )
    return source.path


def _resolve_pack(
    task_id: str, roots: Sequence[Path], *, corpus_base: Path | None
) -> _ResolvedPack | None:
    """The pack ``task_id`` names, or ``None`` where it declares no migration.

    The declaration goes through the same ``inspect_migration_declaration`` gate
    ``tolokaforge validate`` applies, so a pack the bar reads is a pack that already loads:
    every rule the sidecar is refused for at authoring time is refused here too, before any
    evidence is weighed against it — the corpus each entry names among them, read against
    ``corpus_base``.
    """
    grading_path = _grading_path_of(task_id, roots)
    try:
        declaration = inspect_migration_declaration(grading_path, corpus_base=corpus_base)
    except (ValueError, ValidationError, RuntimeError, yaml.YAMLError) as exc:
        raise ReconcileError(
            f"the migration declared beside {grading_path} does not load, so there is no "
            f"claim to check against the corpus: {exc}"
        ) from exc
    if declaration is None:
        return None
    grading = yaml.safe_load(grading_path.read_text(encoding="utf-8")) or {}
    block = grading.get("trace_checks") if isinstance(grading, Mapping) else None
    if not isinstance(block, Mapping):
        raise ReconcileError(
            f"{grading_path} declares a migration but no trace_checks block, so no constraint "
            "the declaration names can be recomputed. Declare the constraints the migration "
            "is by, or drop the migration file"
        )
    return _ResolvedPack(
        task_id=task_id,
        grading_path=grading_path,
        entries=tuple(declaration.migrations),
        trace_checks=block,
    )


def _refuse_a_block_the_corpus_cannot_be_graded_against(
    pack: _ResolvedPack, bundles: Sequence[Path]
) -> list[UnreadableTrial]:
    """Stop before any trial is re-checked when the pack's block does not fit the corpus.

    The same gate a pack meets before a run, applied against the tool set each bundle
    *recorded* rather than the tools the pack declares today. Without it a constraint naming
    a tool the corpus never had would fail on every trial and be reported as a disagreement
    with the judge, which is a statement about the pack's drift dressed as evidence.

    A bundle whose recorded tool set cannot be read answers this gate for nobody, so it is
    returned as unreadable and left out of the corpus the gate is applied against: one
    trial's damaged artifact is not grounds for refusing every other trial a report.
    """
    grading = {"trace_checks": dict(pack.trace_checks)}
    unreadable: list[UnreadableTrial] = []
    for bundle in bundles:
        try:
            inventory = tool_inventory_from_bundle(bundle)
        except MissingTraceReplayInputError as exc:
            unreadable.append(UnreadableTrial(trial=str(bundle), reason=str(exc)))
            continue
        report = inspect_grading_authoring(grading, inventory)
        if not report.errors:
            continue
        written = "\n".join(f"  - {item.where}: {item.message}" for item in report.errors)
        raise ReconcileError(
            f"the trace_checks block in {pack.grading_path} cannot be graded against the "
            f"tools {bundle} recorded:\n{written}"
        )
    return unreadable


def _recorded_rubric(grading_config: Mapping[str, Any]) -> Rubric:
    """The rubric a bundle recorded, through the model that wrote it.

    Read through :class:`~tolokaforge.core.models.Rubric` so a recorded criterion and a declared
    ``was`` are compared field for field rather than key by key. A bundle that recorded no rubric
    at all yields one with no criteria: it says nothing about any criterion's pre-migration shape.
    """
    judge = (grading_config or {}).get("llm_judge")
    rubric = judge.get("rubric") if isinstance(judge, Mapping) else None
    return Rubric(**rubric) if isinstance(rubric, Mapping) else Rubric(criteria=[])


def _recorded_criteria(task: Mapping[str, Any]) -> dict[str, MigratedCriterion]:
    """The rubric a bundle recorded, by criterion id, in the shape ``was`` is written in.

    Built with ``model_construct``, deliberately skipping the authoring validators: a recorded
    criterion is *data*, so a bundle whose judge graded a criterion with a blank description
    records a fact about that trial rather than an authoring defect for this run to refuse — and
    refusing it would fail the reconciliation under the wrong file's message. The values are
    already typed, coming off :class:`~tolokaforge.core.models.Criterion`, so nothing here is
    unvalidated input; the field list is ``was``'s own, so a field added to it is carried across
    or fails loud rather than being silently dropped.
    """
    return {
        criterion.id: MigratedCriterion.model_construct(
            **{name: getattr(criterion, name) for name in EVERY_DECLARED_FIELD}
        )
        for criterion in _recorded_rubric(task.get("grading_config") or {}).criteria
    }


def _judge_verdict(
    criterion_id: str, grade: Mapping[str, Any] | None
) -> tuple[bool | None, str, Unavailable | None]:
    """The judge's recorded label for one criterion, or why the bundle carries none.

    ``JudgeStatus.ERRORED`` is *not* a not-met label: an errored judge reached no verdict, and
    folding it in as not-met would manufacture agreement with a failing constraint.
    """
    status = None if grade is None else grade.get("judge_status")
    if status != "completed":
        return (
            None,
            "",
            Unavailable(
                exclusion=TrialExclusion.JUDGE_DID_NOT_COMPLETE,
                reason=(
                    f"judge_status is {status!r}, not 'completed', so the judge reached no "
                    "verdict to compare — an errored or absent judge is not a not-met label"
                ),
            ),
        )
    recorded = grade.get("criterion_results") if grade else None
    if not recorded:
        return (
            None,
            "",
            Unavailable(
                exclusion=TrialExclusion.NO_CRITERION_RESULTS,
                reason=(
                    "the recorded grade carries no criterion_results, so nothing says what "
                    "the judge concluded per criterion"
                ),
            ),
        )
    for result in recorded:
        if isinstance(result, Mapping) and result.get("id") == criterion_id:
            return bool(result.get("met")), str(result.get("justification") or ""), None
    return (
        None,
        "",
        Unavailable(
            exclusion=TrialExclusion.NO_VERDICT_FOR_CRITERION,
            reason=(
                f"the recorded grade holds no verdict for criterion {criterion_id!r}, so this "
                "trial's judge never labelled it"
            ),
        ),
    )


def _constraint_verdict(
    entry: MigrationEntry, verdicts: Mapping[str, Any]
) -> tuple[bool | None, Unavailable | None]:
    """The recomputed label: every constraint the entry is ``by`` had to decide, and pass.

    A conjunction, because that is what the declaration claims — the criterion is replaced by
    *these* checks together. An undecided constraint is not a failure here even though it
    forfeits its weight at grade time: undecided means the trial's evidence could not say,
    which is no label to agree or disagree with.
    """
    passed = True
    for constraint_id in entry.by:
        result = verdicts.get(constraint_id)
        if result is None:
            return None, Unavailable(
                exclusion=TrialExclusion.CONSTRAINT_VERDICT_UNAVAILABLE,
                reason=(
                    f"the recomputation reached no verdict for constraint {constraint_id!r} — "
                    "a route-scoped constraint is measured only on the trials its route won"
                ),
            )
        if result.undecided:
            return None, Unavailable(
                exclusion=TrialExclusion.CONSTRAINT_VERDICT_UNAVAILABLE,
                reason=f"constraint {constraint_id!r} is undecided on this trial: {result.message}",
            )
        passed = passed and result.passed
    return passed, None


def _trial_evidence(
    entry: MigrationEntry,
    *,
    trial: str,
    recorded_criteria: Mapping[str, MigratedCriterion],
    task: Mapping[str, Any],
    grade: Mapping[str, Any] | None,
    verdicts: Mapping[str, Any],
    trace: TraceChecksResult,
) -> TrialEvidence:
    judge_met, justification, judge_missing = _judge_verdict(entry.criterion, grade)
    constraint_passed, constraint_missing = _constraint_verdict(entry, verdicts)
    unavailable = judge_missing or constraint_missing
    return TrialEvidence(
        trial=trial,
        recorded_criterion=recorded_criteria.get(entry.criterion),
        judge_met=None if unavailable else judge_met,
        constraint_passed=None if unavailable else constraint_passed,
        justification=justification,
        unavailable=unavailable,
        recorded_verdict=(
            None
            if unavailable or grade is None
            else RecordedTrialVerdict(
                grading_config=task.get("grading_config") or {},
                grade=grade,
                trace_component=trace.score,
                trace_gate_failed=trace.gate_failed,
                gate_constraint_ids=frozenset(
                    name
                    for name in entry.by
                    if (found := verdicts.get(name)) is not None
                    and found.severity == TraceConstraintSeverity.GATE
                ),
            )
        ),
    )


# ---------------------------------------------------------------------------
# The command's one entry point
# ---------------------------------------------------------------------------


@dataclass
class _Pool:
    """One criterion's evidence, gathered across every task whose pack declares it."""

    entry: MigrationEntry
    task_ids: list[str]
    trials: list[TrialEvidence]


def _resolved_constraints(pack: _ResolvedPack, entry: MigrationEntry) -> str:
    """The constraint definitions an entry is ``by``, as one comparable document.

    Half of the pooling key. Two packs may quote one measurement only if they claim the same
    criterion *and* recompute it the same way: a shared criterion text over two different
    predicates is two measurements folded into one row, which is the fold replay's
    ``(task_id, constraint_id)`` keying exists to prevent.
    """
    config = TraceChecksConfig(**pack.trace_checks)
    declared = {item.id: item for item in config.constraints}
    for path in config.alternatives or ():
        declared.update({item.id: item for item in path.constraints})
    return yaml.safe_dump(
        [declared[name].model_dump(mode="json") for name in entry.by if name in declared],
        sort_keys=True,
    )


_THE_CLAIM_AN_ENTRY_MAKES = {"mode", "residual", "combine_weights"}
"""What an entry claims *about* the criterion, as against which criterion it claims.

Two entries agreeing on the criterion, its text and the constraints still differ if one narrows
where the other retires, or lands the freed share elsewhere. Pooling those folds them into one
verdict under whichever was read first, discarding the second's tolerance and its map — and the
two tolerances are not interchangeable: :data:`_FORBIDDEN_DIRECTIONS` gives a permissive
disagreement to a narrow and refuses it to a retirement, so a retirement folded under a narrow's
mode passes on exactly the evidence that must refuse it.
"""


@dataclass(frozen=True, order=True)
class _PoolKey:
    """What two declarations must agree on before their trials are one measurement.

    ``criterion`` and ``was_text`` say the two packs claim the same criterion, ``constraints``
    that they recompute it the same way, and ``claim`` that they claim the same thing about it —
    see :data:`_THE_CLAIM_AN_ENTRY_MAKES`.

    Ordered, so the report's entries come out in a stable order rather than in the order the
    corpus's directory listing happened to file them.
    """

    criterion: str
    was_text: str
    constraints: str
    claim: str


def _pool_key(pack: _ResolvedPack, entry: MigrationEntry) -> _PoolKey:
    return _PoolKey(
        criterion=entry.criterion,
        was_text=" ".join(entry.was.description.split()),
        constraints=_resolved_constraints(pack, entry),
        claim=entry.model_dump_json(include=_THE_CLAIM_AN_ENTRY_MAKES),
    )


def _refuse_pooling_two_different_claims(pools: Mapping[_PoolKey, _Pool]) -> None:
    """One criterion claimed by two tasks that do not declare the same thing."""
    by_criterion: dict[str, _PoolKey] = {}
    for key, pool in pools.items():
        first = by_criterion.setdefault(key.criterion, key)
        if first == key:
            continue
        raise ReconcileError(
            f"criterion {key.criterion!r} is declared by {sorted(pools[first].task_ids)} and "
            f"{sorted(pool.task_ids)} with a different criterion text, different constraints, or "
            "a different claim about it (mode, residual or combine_weights), so their trials "
            "measure two different claims and cannot be pooled into one verdict. Make the "
            "declarations identical, or reconcile each task alone"
        )


def _pooled_evidence(
    packs: Mapping[str, _ResolvedPack], by_task: Mapping[str, Sequence[Path]]
) -> tuple[dict[_PoolKey, _Pool], list[UnreadableTrial]]:
    """Read every bundle once and file its evidence under each entry it can speak to.

    Each pack's entries are keyed once here rather than once per bundle: a key resolves the whole
    ``trace_checks`` block and dumps it, which is a fact about the (pack, entry) pair and says
    nothing about the trial being filed under it.
    """
    pools: dict[_PoolKey, _Pool] = {}
    unreadable: list[UnreadableTrial] = []
    for task_id, pack in sorted(packs.items()):
        without_a_tool_set = _refuse_a_block_the_corpus_cannot_be_graded_against(
            pack, by_task[task_id]
        )
        unreadable.extend(without_a_tool_set)
        named = {row.trial for row in without_a_tool_set}
        override = _override_from(pack)
        keyed = tuple((entry, _pool_key(pack, entry)) for entry in pack.entries)
        for bundle in (item for item in by_task[task_id] if str(item) not in named):
            failure = _file_one_bundle(
                bundle, pack=pack, keyed=keyed, override=override, pools=pools
            )
            if failure is not None:
                unreadable.append(failure)
    return pools, unreadable


def _override_from(pack: _ResolvedPack) -> TraceChecksOverride:
    """The pack's own block, in the shape the replay reader takes a supplied one in."""
    try:
        return TraceChecksOverride(path=pack.grading_path, block=pack.trace_checks)
    except ValueError as exc:
        raise ReconcileError(
            f"the trace_checks block in {pack.grading_path} cannot be used as written: {exc}"
        ) from exc


def _file_one_bundle(
    bundle: Path,
    *,
    pack: _ResolvedPack,
    keyed: Sequence[tuple[MigrationEntry, _PoolKey]],
    override: TraceChecksOverride,
    pools: dict[_PoolKey, _Pool],
) -> UnreadableTrial | None:
    """Re-check one bundle and file its evidence, or name why it could not be read.

    The recorded rubric is read here, inside the net, because it is read from the bundle's own
    ``task.yaml``: a rubric that will not parse is one unreadable bundle, and letting it out would
    abort the whole reconciliation under the message of whatever file was being resolved at the
    time. The recorded *grade* is proved readable here for the same reason and before the filing
    loop rather than inside it, so a bundle refused is a bundle that filed nothing.
    """
    try:
        task = recorded_task(bundle)
        criteria = _recorded_criteria(task)
        grade = recorded_grade(bundle)
        _refuse_a_recorded_grade_the_counterfactual_cannot_read(bundle, grade)
        inputs = read_trace_replay_inputs(bundle, override=override)
        result = replay_trace_checks(inputs)
    except (MissingTraceReplayInputError, TimelineInconsistencyError) as exc:
        return UnreadableTrial(trial=str(bundle), reason=str(exc))
    except ValidationError as exc:
        return UnreadableTrial(
            trial=str(bundle),
            reason=(
                f"the rubric recorded in {bundle / 'task.yaml'} does not read as one, so nothing "
                f"says what shape the criterion had when this trial was graded: {exc}. Drop the "
                "bundle from the corpus, or reconcile it against the tree that wrote it"
            ),
        )

    verdicts = {constraint.id: constraint for constraint in result.constraints}
    for entry, key in keyed:
        pool = pools.setdefault(key, _Pool(entry=entry, task_ids=[], trials=[]))
        if pack.task_id not in pool.task_ids:
            pool.task_ids.append(pack.task_id)
        pool.trials.append(
            _trial_evidence(
                entry,
                trial=str(bundle),
                recorded_criteria=criteria,
                task=task,
                grade=grade,
                verdicts=verdicts,
                trace=result,
            )
        )
    return None


def reconcile_root(source: Path, replay_id: str) -> Path:
    """The subtree one reconciliation owns, and the only place under the source it writes."""
    return Path(source) / RECONCILE_DIRNAME / replay_id


def _excluded_from_the_corpus(bundle: Path) -> CorpusExclusion:
    """A discovered bundle carrying no ``task.yaml``, excluded by name.

    Only a trial the substrate killed before it ran is excused the file: it records
    no pack to resolve a migration against, and a corpus is not defective for
    holding one. A task-less bundle that recorded a real episode lost what it was
    graded against, which is a defect, so it is raised into ``unreadable_trials``
    and keeps blocking the exit code.
    """
    termination = aborted_without_a_task_snapshot(bundle)
    if termination is None:
        raise MissingTraceReplayInputError(
            f"{bundle / 'task.yaml'} is missing, so nothing names the task whose "
            "migration this trial could speak to"
        )
    return CorpusExclusion(
        bundle=str(bundle),
        reason=(
            "the trial was aborted before it was measured "
            f"(termination_reason: {termination}), so it recorded no task.yaml and "
            "speaks to no pack's migration"
        ),
    )


def reconcile_corpus(
    source: Path,
    *,
    replay_id: str,
    packs: Sequence[Path] | None = None,
    dry_run: bool = False,
    corpus_base: Path | None = None,
) -> ReconcileReport:
    """Reconcile every migration the packs behind ``source``'s trials declare.

    Reads the corpus and writes only its own report — no bundle is opened for write and no
    pack is edited, whatever the verdict. ``dry_run`` withholds the report artifact; the
    reconciliation itself is performed either way, because the verdict is the thing worth
    having for free.

    ``corpus_base`` is the directory each declaration's ``corpus`` is read against, defaulting
    to the declaration's own directory. It is a parameter rather than an ambient read so this
    module resolves nothing off the working directory; the CLI supplies one.

    Raises:
        ReconcileError: If ``source`` holds no bundle, if every bundle it holds is
            excluded from the corpus, if no pack behind it declares a migration, if a
            ``task_id`` resolves to no pack or to several, if a pack's block cannot be
            graded against what a bundle recorded, or if one criterion is pooled across
            tasks declaring different things.
    """
    source = Path(source)
    roots = tuple(Path(root) for root in (packs or (DEFAULT_PACKS_ROOT,)))
    reading = _read_the_corpus(source)
    declaring = {
        task_id: pack
        for task_id in sorted(reading.by_task)
        if (pack := _resolve_pack(task_id, roots, corpus_base=corpus_base)) is not None
    }
    if not declaring:
        raise ReconcileError(
            f"no pack behind the trials under {source} declares a migration "
            f"(task ids: {sorted(reading.by_task)}; "
            f"searched {', '.join(str(r) for r in roots)}). "
            "There is nothing to reconcile: write a migration.yaml beside the pack's "
            "grading.yaml, or point --packs at the tree that carries one"
        )

    report = _reconciled(
        source,
        reading,
        declaring,
        replay_id=replay_id,
        roots=roots,
        over_the_declared_corpus=False,
    )
    if not dry_run:
        emit_reconcile_report(report, source=source, replay_id=replay_id)
    return report


@dataclass(frozen=True)
class _CorpusReading:
    """What one corpus directory holds: its bundles by task, and what could not be filed."""

    bundles: tuple[Path, ...]
    by_task: Mapping[str, Sequence[Path]]
    unreadable: tuple[UnreadableTrial, ...]
    excluded: tuple[CorpusExclusion, ...]


def _read_the_corpus(source: Path) -> _CorpusReading:
    """Discover ``source``'s bundles and file each under the task it recorded.

    Raises:
        ReconcileError: If ``source`` holds no bundle, or if every bundle it holds records no
            task and is therefore excluded from the corpus.
    """
    bundles = discover_trial_bundles(source)
    if not bundles:
        raise ReconcileError(
            f"no trial bundle under {source} — a corpus holding nothing reconciles nothing. A "
            "bundle is a directory holding trajectory.yaml"
        )

    by_task: dict[str, list[Path]] = {}
    unreadable: list[UnreadableTrial] = []
    excluded: list[CorpusExclusion] = []
    for bundle in bundles:
        try:
            if not (bundle / "task.yaml").exists():
                excluded.append(_excluded_from_the_corpus(bundle))
                continue
            task_id = recorded_task_id(bundle, recorded_task(bundle))
        except MissingTraceReplayInputError as exc:
            unreadable.append(UnreadableTrial(trial=str(bundle), reason=str(exc)))
            continue
        by_task.setdefault(task_id, []).append(bundle)

    if not by_task and excluded:
        raise ReconcileError(
            f"no trial under {source} names a task whose migration could be reconciled: "
            f"{len(excluded)} of {len(bundles)} discovered bundles are excluded from the "
            "corpus, recording no task.yaml because the trial never ran. Reconcile a corpus "
            "whose trials reached the agent"
        )
    return _CorpusReading(
        bundles=tuple(bundles),
        by_task=by_task,
        unreadable=tuple(unreadable),
        excluded=tuple(excluded),
    )


def _reconciled(
    source: Path,
    reading: _CorpusReading,
    declaring: Mapping[str, _ResolvedPack],
    *,
    replay_id: str | None,
    roots: Sequence[Path],
    over_the_declared_corpus: bool,
) -> ReconcileReport:
    """Weigh every entry ``declaring`` holds against the trials ``reading`` filed for it."""
    pools, read_failures = _pooled_evidence(declaring, reading.by_task)
    _refuse_pooling_two_different_claims(pools)
    return ReconcileReport(
        source=str(source),
        replay_id=replay_id,
        packs_searched=[str(root) for root in roots],
        reference_labeller=REFERENCE_LABELLER,
        candidate_labeller=CANDIDATE_LABELLER,
        trials_read=len(reading.bundles),
        entries=[
            reconcile_entry(
                pool.entry,
                task_ids=pool.task_ids,
                trials=pool.trials,
                over_the_declared_corpus=over_the_declared_corpus,
            )
            for _, pool in sorted(pools.items())
        ],
        unreadable_trials=[*reading.unreadable, *read_failures],
        excluded_bundles=list(reading.excluded),
    )


def reconcile_declared_corpora(
    *, packs: Sequence[Path] | None = None, corpus_base: Path | None = None
) -> tuple[ReconcileReport, ...]:
    """Reconcile every migration declared under ``packs``, each over the corpus it names.

    One report per declared corpus, ordered by its path: the entries measured over a corpus
    are the ones whose own declaration names it, so an entry is evidence about that corpus and
    about no other. Nothing is written — the corpora are committed, and a report inside one
    would dirty the tree — and because the source *is* the corpus each declaration names, the
    declared-evidence rule is an equality rather than a bound.

    ``corpus_base`` is the directory each declaration's ``corpus`` is read against, defaulting
    to the declaration's own directory. It is a parameter rather than an ambient read so this
    module resolves nothing off the working directory; the CLI supplies one.

    Raises:
        ReconcileError: If no pack under ``packs`` declares a migration, if a sidecar names no
            resolvable pack, if a declared corpus holds no trial of the task declaring it, and
            for everything :func:`reconcile_corpus` raises over one corpus.
    """
    roots = tuple(Path(root) for root in (packs or (DEFAULT_PACKS_ROOT,)))
    by_corpus = _declarations_by_corpus(roots, corpus_base=corpus_base)
    if not by_corpus:
        raise ReconcileError(
            f"no pack under {', '.join(str(root) for root in roots)} declares a migration, so "
            "there is nothing to reconcile. Write a migration.yaml beside a pack's "
            "grading.yaml, or point --packs at the tree that carries one"
        )
    return tuple(
        _reconciled_over_the_corpus_it_names(corpus, declaring, roots=roots)
        for corpus, declaring in sorted(by_corpus.items())
    )


def _reconciled_over_the_corpus_it_names(
    corpus: Path, declaring: Mapping[str, _ResolvedPack], *, roots: Sequence[Path]
) -> ReconcileReport:
    """One corpus, weighed against the entries that named it."""
    reading = _read_the_corpus(corpus)
    absent = sorted(set(declaring) - set(reading.by_task))
    if absent:
        raise ReconcileError(
            f"{corpus} is named by the migration {', '.join(absent)} declares and holds no "
            "trial of it, so that declaration is measured over nothing. A corpus is the "
            "recorded trials of the task whose criterion it is evidence about: curate one "
            "from runs of that task, or point the entry's corpus at the one that has them"
        )
    return _reconciled(
        corpus, reading, declaring, replay_id=None, roots=roots, over_the_declared_corpus=True
    )


def _declarations_by_corpus(
    roots: Sequence[Path], *, corpus_base: Path | None
) -> dict[Path, dict[str, _ResolvedPack]]:
    """Every declaring pack under ``roots``, filed under the corpus each of its entries names.

    A pack whose entries name two corpora is filed under both, carrying only the entries
    measured over each.
    """
    filed: dict[Path, dict[str, _ResolvedPack]] = {}
    for sidecar in _declared_sidecars(roots):
        task_id = _task_id_declaring(sidecar)
        pack = _resolve_pack(task_id, roots, corpus_base=corpus_base)
        if pack is None:
            raise ReconcileError(
                f"{sidecar} is not beside the grading.yaml the pack declaring task_id "
                f"{task_id!r} resolves to, so nothing reads it: a migration is declared beside "
                "the file whose rubric it migrates. Point the task's grading field at the file "
                "the sidecar sits beside, or move the sidecar"
            )
        for corpus, entries in _entries_by_corpus(pack, corpus_base).items():
            filed.setdefault(corpus, {})[task_id] = replace(pack, entries=entries)
    return filed


def _declared_sidecars(roots: Sequence[Path]) -> list[Path]:
    """Every ``migration.yaml`` under ``roots``.

    The sidecar is what is searched for rather than the packs that might carry one, because a
    tree holds packs that do not load at all and loading each to ask whether it declares a
    migration would make an unrelated defect this command's problem.
    """
    return sorted({found for root in roots for found in Path(root).rglob(MIGRATION_FILENAME)})


def _task_id_declaring(sidecar: Path) -> str:
    """The ``task_id`` of the pack ``sidecar`` sits in — the join to a recorded trial.

    Read raw for the reason :func:`_declaring_task_files` reads raw: the id is the lookup key,
    and the pack it selects is then loaded properly.
    """
    task_file = sidecar.parent / "task.yaml"
    if not task_file.exists():
        raise ReconcileError(
            f"{sidecar} declares a migration and no task.yaml sits beside it, so no task_id "
            "names the pack it migrates and no recorded trial resolves to it. A migration is "
            "declared beside the task.yaml and grading.yaml of the pack it is about"
        )
    declared = yaml.safe_load(task_file.read_text(encoding="utf-8"))
    task_id = declared.get("task_id") if isinstance(declared, Mapping) else None
    if isinstance(task_id, str) and task_id:
        return task_id
    raise ReconcileError(
        f"{task_file} declares no task_id and {sidecar} declares a migration of its rubric, so "
        "no recorded trial resolves to this pack and its claim is measured over nothing"
    )


def _entries_by_corpus(
    pack: _ResolvedPack, corpus_base: Path | None
) -> dict[Path, tuple[MigrationEntry, ...]]:
    """One pack's entries grouped by the corpus directory each of them names."""
    base = corpus_base_for(pack.grading_path, corpus_base)
    grouped: dict[Path, list[MigrationEntry]] = {}
    for entry in pack.entries:
        grouped.setdefault(base / entry.corpus, []).append(entry)
    return {corpus: tuple(entries) for corpus, entries in grouped.items()}


def emit_reconcile_report(report: ReconcileReport, *, source: Path, replay_id: str) -> Path:
    """Write the report under the subtree this reconciliation owns; return the path."""
    destination = reconcile_root(source, replay_id) / RECONCILE_REPORT_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        yaml.dump(
            report.model_dump(mode="json"),
            handle,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
    return destination
