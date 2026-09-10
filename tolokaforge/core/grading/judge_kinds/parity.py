"""κ-parity measurement harness for :class:`JudgeKind` implementations.

Every :class:`JudgeKind` that ships or joins the registry has to prove
its per-criterion verdicts agree with the reference kind
(``single_shot_rubric``) on a shared corpus and with themselves across
replays. This module supplies the three functions the canonical parity
lane calls and the gate that turns per-criterion Cohen's κ into a
shippable/blocking verdict.

**Two measurements, one report shape.** Both
:func:`measure_cross_kind_agreement` and :func:`measure_self_consistency`
return a :class:`~tolokaforge.core.grading.agreement.CalibrationReport`
so the gate reads the same maths in either direction; the difference is
what fills the reference and candidate legs.

**Per-criterion, three-level.** :func:`decide_parity_gate` walks each
criterion in the report and picks one of four verdicts — ``pass`` (κ at
or above the block bar), ``warn`` (in the advisory band above the block
bar), ``block`` (below the block bar) or ``insufficient_evidence`` (κ
undefined, most often because the corpus produced fewer than two paired
observations for the criterion). The aggregate ``shippable`` roll-up is
``all(status in {pass, warn})`` — a warn is surfaced but does not block
a ship. The three-level policy is local to this module; the shared
:mod:`~tolokaforge.core.grading.agreement` gate stays a single-number
aggregate ``decide_gate`` for its other callers.

**Partial-verdict handling is loud.** An entry whose reference or
candidate leg returns a :class:`JudgeResult` with fewer
``criterion_results`` than the rubric has criteria (partial completion,
``status == ERRORED``, a chunked kind failing on one chunk) lands in the
report's ``errored_fixture_ids`` with a reason that names the entry_id,
the leg that fell short, and the missing criterion ids. Silent partial
pairs are the failure mode this contract refuses — a candidate that
grades 4 of 5 criteria is not "80 % agreeing", it is failing to grade
one criterion, which the gate reports as such.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tolokaforge.core.grading.agreement import (
    CalibrationReport,
    CriterionObservation,
    build_report,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_kinds._protocol import JudgeKind
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "ParityCorpusEntry",
    "ParityGateDecision",
    "ParityGateThresholds",
    "PerCriterionVerdict",
    "decide_parity_gate",
    "measure_cross_kind_agreement",
    "measure_self_consistency",
]


ParityGateStatus = Literal["pass", "warn", "block", "insufficient_evidence"]
ParityMeasurement = Literal["cross_kind", "self_consistency"]


@dataclass(frozen=True)
class ParityGateThresholds:
    """Three-level per-criterion thresholds. Landis-Koch anchored;
    aligns with MT-Bench / Langfuse's 80 % judge-agreement bar.

    ``block`` is the cross-kind pass bar (a candidate kind and the
    reference kind must agree at κ ≥ 0.8 per criterion). Self-consistency
    is the stricter bar of a kind agreeing with itself across replays and
    uses ``self_consistency_block`` (κ ≥ 0.7). ``warn`` sits below both
    pass bars (κ < 0.6) and surfaces criteria that pass the block bar but
    are close enough that a future drift would push them under.
    """

    block: float = 0.8
    self_consistency_block: float = 0.7
    warn: float = 0.6


@dataclass(frozen=True)
class PerCriterionVerdict:
    """One criterion's parity outcome under one measurement (cross or self).

    ``kappa`` is the observed Cohen's κ or ``None`` when it is
    undefined (fewer than two paired observations, or a label-invariant
    corpus in which κ's chance-agreement denominator collapses to zero).
    ``status`` is one of ``pass`` / ``warn`` / ``block`` /
    ``insufficient_evidence``. ``reason`` is a human-readable line the
    gate report surfaces; ``insufficient_evidence`` reasons quote the
    string ``"undefined"`` verbatim so a grep-based CI parser catches
    them without pattern acrobatics.
    """

    criterion_id: str
    observations: int
    kappa: float | None
    status: ParityGateStatus
    reason: str


@dataclass(frozen=True)
class ParityGateDecision:
    """The gate's per-criterion verdicts + a boolean shippability roll-up.

    ``shippable`` is ``True`` iff every :class:`PerCriterionVerdict`
    resolved to ``pass`` or ``warn``. ``blocking_criteria`` and
    ``warning_criteria`` list criterion ids in the ``block`` and
    ``warn`` bands respectively so a reviewer sees the whole picture,
    not just the first failing name.
    """

    per_criterion: tuple[PerCriterionVerdict, ...]
    shippable: bool
    blocking_criteria: tuple[str, ...]
    warning_criteria: tuple[str, ...]


@dataclass(frozen=True)
class ParityCorpusEntry:
    """One replayable judge-input fixture — the smallest sufficient
    shape to re-drive :meth:`JudgeKind.evaluate` under a scripted client.

    ``entry_id`` is the slug the parity lane parametrises on and the
    identifier the report uses when it surfaces a failing entry.
    ``judge_scripts`` is keyed on ``JudgeKind.NAME`` — the cassette a
    kind draws its LLM turns from when the harness runs cassette-mode.
    A missing key for a kind under test is the loader's responsibility
    to raise on (loud, not silent skip); this dataclass carries only
    the shape.
    """

    entry_id: str
    rubric: Rubric
    agent_system_prompt: str
    transcript: list[dict[str, Any]]
    state_diff: str | None
    disable_knowledge_search: bool
    custom_system_prompt: str | None
    include_agent_system_prompt: bool
    judge_scripts: Mapping[str, list[Any]]


def measure_cross_kind_agreement(
    *,
    reference_kind: JudgeKind,
    candidate_kind: JudgeKind,
    corpus: Sequence[ParityCorpusEntry],
    judge_model_config: ModelConfig,
    reference_provider: JudgeModelProvider,
    candidate_provider: JudgeModelProvider,
    db_reader: DBReader | None = None,
    kb_search: KnowledgeSearch | None = None,
    workspace_dir: Path | None = None,
    extra_read_tools: Sequence[Tool] = (),
    kind_config: Mapping[str, Any] | None = None,
    logger: StructuredLogger | None = None,
) -> CalibrationReport:
    """Drive ``reference_kind`` and ``candidate_kind`` over the same
    corpus, pair per-criterion verdicts, return the aggregated report.

    Each entry is graded twice — once by ``reference_kind`` against
    ``reference_provider`` and once by ``candidate_kind`` against
    ``candidate_provider``. An entry where either leg returns a
    :class:`JudgeResult` whose ``criterion_results`` is shorter than
    ``entry.rubric.criteria`` (partial completion, ``status ==
    ERRORED``, chunked kind failing on one chunk) contributes NO paired
    observation and is added to the returned report's
    ``errored_fixture_ids`` — with a reason naming the entry_id, the
    leg that fell short, and the missing criterion ids. Reference-short
    and candidate-short are treated symmetrically.
    """
    logger = _resolve_logger(logger)
    observations: list[CriterionObservation] = []
    errored: list[str] = []

    for entry in corpus:
        ref_result = reference_kind.evaluate(
            **_evaluate_kwargs(
                entry=entry,
                judge_model_config=judge_model_config,
                judge_model_provider=reference_provider,
                db_reader=db_reader,
                kb_search=kb_search,
                workspace_dir=workspace_dir,
                extra_read_tools=extra_read_tools,
                kind_config=kind_config,
                logger=logger,
            )
        )
        cand_result = candidate_kind.evaluate(
            **_evaluate_kwargs(
                entry=entry,
                judge_model_config=judge_model_config,
                judge_model_provider=candidate_provider,
                db_reader=db_reader,
                kb_search=kb_search,
                workspace_dir=workspace_dir,
                extra_read_tools=extra_read_tools,
                kind_config=kind_config,
                logger=logger,
            )
        )
        entry_error = _check_partial_pair(
            entry=entry,
            reference_result=ref_result,
            candidate_result=cand_result,
        )
        if entry_error is not None:
            errored.append(entry_error)
            continue
        observations.extend(_pair_observations(entry, ref_result, cand_result))

    return build_report(observations, errored)


def measure_self_consistency(
    *,
    kind_factory: Callable[[int], JudgeKind],
    corpus: Sequence[ParityCorpusEntry],
    replays: int,
    judge_model_config: ModelConfig,
    provider_factory: Callable[[int], JudgeModelProvider],
    db_reader: DBReader | None = None,
    kb_search: KnowledgeSearch | None = None,
    workspace_dir: Path | None = None,
    extra_read_tools: Sequence[Tool] = (),
    kind_config: Mapping[str, Any] | None = None,
    logger: StructuredLogger | None = None,
) -> CalibrationReport:
    """Run the kind over the corpus ``replays`` times and pair every
    later replay against replay 0.

    A fresh kind is built per replay via ``kind_factory(replay_index)``
    and a fresh provider via ``provider_factory(replay_index)``. Two
    factories rather than a shared kind and a shared provider because a
    fixture kind that models non-determinism needs its own replay index
    to vary its verdict across replays — keeping that affordance on the
    fixture kind's constructor keeps the :class:`JudgeKind` Protocol
    free of a test-only ``replay_index`` kwarg. For stateless kinds
    (``kind_factory=lambda _i: SingleShotRubricJudgeKind()``) the index
    is simply ignored.

    ``replays >= 2`` is required — Cohen's κ is undefined for fewer
    than two paired observations.
    """
    if replays < 2:
        raise ValueError(
            f"measure_self_consistency requires replays >= 2 (Cohen's kappa is "
            f"undefined for fewer than two paired observations); got replays={replays}."
        )

    logger = _resolve_logger(logger)
    replay_results: list[list[JudgeResult]] = []
    errored: list[str] = []

    for replay_index in range(replays):
        kind = kind_factory(replay_index)
        provider = provider_factory(replay_index)
        replay_results.append(
            [
                kind.evaluate(
                    **_evaluate_kwargs(
                        entry=entry,
                        judge_model_config=judge_model_config,
                        judge_model_provider=provider,
                        db_reader=db_reader,
                        kb_search=kb_search,
                        workspace_dir=workspace_dir,
                        extra_read_tools=extra_read_tools,
                        kind_config=kind_config,
                        logger=logger,
                    )
                )
                for entry in corpus
            ]
        )

    observations: list[CriterionObservation] = []
    baseline = replay_results[0]
    for later_index in range(1, replays):
        later = replay_results[later_index]
        for entry, base_result, later_result in zip(corpus, baseline, later, strict=True):
            entry_error = _check_partial_pair(
                entry=entry,
                reference_result=base_result,
                candidate_result=later_result,
                candidate_label=f"replay {later_index}",
            )
            if entry_error is not None:
                errored.append(entry_error)
                continue
            observations.extend(
                _pair_observations(
                    entry,
                    base_result,
                    later_result,
                    observation_suffix=f"replay{later_index}",
                )
            )

    return build_report(observations, errored)


def decide_parity_gate(
    report: CalibrationReport,
    *,
    thresholds: ParityGateThresholds,
    measurement: ParityMeasurement,
) -> ParityGateDecision:
    """Turn a per-criterion κ report into a per-criterion gate decision.

    Three bands per criterion, anchored on
    :class:`ParityGateThresholds`:

    - ``kappa >= pass_bar`` → ``pass`` (shippable). ``pass_bar`` is
      ``thresholds.block`` for ``cross_kind`` and
      ``thresholds.self_consistency_block`` for ``self_consistency``.
    - ``thresholds.warn <= kappa < pass_bar`` → ``warn`` (shippable,
      surfaced in ``warning_criteria``).
    - ``kappa < thresholds.warn`` → ``block`` (not shippable, surfaced
      in ``blocking_criteria``).
    - ``kappa is None`` (undefined — fewer than two paired observations,
      or a label-invariant corpus in which κ's chance denominator
      collapses to zero) → ``insufficient_evidence`` (not shippable);
      the reason quotes the string ``"undefined"`` verbatim so a
      grep-based CI parser catches it.

    A report whose ``errored_fixture_ids`` is non-empty adds those ids
    to ``blocking_criteria`` — an entry that failed to grade cleanly
    cannot ship regardless of the per-criterion κ its surviving pairs
    produced.
    """
    pass_bar = _pass_bar(thresholds, measurement)

    verdicts: list[PerCriterionVerdict] = []
    blocking: list[str] = []
    warning: list[str] = []

    for row in report.per_criterion:
        if row.kappa is None:
            reason = (
                f"kappa is undefined for criterion {row.criterion_id!r} "
                f"(n={row.n}; need >= 2 paired observations with label variation)"
            )
            verdicts.append(
                PerCriterionVerdict(
                    criterion_id=row.criterion_id,
                    observations=row.n,
                    kappa=None,
                    status="insufficient_evidence",
                    reason=reason,
                )
            )
            blocking.append(row.criterion_id)
            continue

        kappa = row.kappa
        if kappa < thresholds.warn:
            reason = (
                f"observed kappa {kappa:.3f} is below the "
                f"{measurement} block threshold {thresholds.warn:.3f} "
                f"for criterion {row.criterion_id!r}"
            )
            verdicts.append(
                PerCriterionVerdict(
                    criterion_id=row.criterion_id,
                    observations=row.n,
                    kappa=kappa,
                    status="block",
                    reason=reason,
                )
            )
            blocking.append(row.criterion_id)
            continue

        if kappa < pass_bar:
            reason = (
                f"observed kappa {kappa:.3f} is in the {measurement} warn band "
                f"[{thresholds.warn:.3f}, {pass_bar:.3f}) "
                f"for criterion {row.criterion_id!r}"
            )
            verdicts.append(
                PerCriterionVerdict(
                    criterion_id=row.criterion_id,
                    observations=row.n,
                    kappa=kappa,
                    status="warn",
                    reason=reason,
                )
            )
            warning.append(row.criterion_id)
            continue

        verdicts.append(
            PerCriterionVerdict(
                criterion_id=row.criterion_id,
                observations=row.n,
                kappa=kappa,
                status="pass",
                reason=(
                    f"observed kappa {kappa:.3f} clears the "
                    f"{measurement} pass bar {_pass_bar(thresholds, measurement):.3f}"
                ),
            )
        )

    if report.errored_fixture_ids:
        blocking.extend(report.errored_fixture_ids)

    shippable = not blocking

    return ParityGateDecision(
        per_criterion=tuple(verdicts),
        shippable=shippable,
        blocking_criteria=tuple(blocking),
        warning_criteria=tuple(warning),
    )


def _pass_bar(thresholds: ParityGateThresholds, measurement: ParityMeasurement) -> float:
    """The per-measurement pass bar: cross-kind uses ``block``, self uses ``self_consistency_block``."""
    return thresholds.block if measurement == "cross_kind" else thresholds.self_consistency_block


def _resolve_logger(logger: StructuredLogger | None):
    if logger is not None:
        return logger
    from tolokaforge.core.logging import StructuredLogger as _StructuredLogger

    return _StructuredLogger(name="judge-kind-parity")


def _evaluate_kwargs(
    *,
    entry: ParityCorpusEntry,
    judge_model_config: ModelConfig,
    judge_model_provider: JudgeModelProvider,
    db_reader: DBReader | None,
    kb_search: KnowledgeSearch | None,
    workspace_dir: Path | None,
    extra_read_tools: Sequence[Tool],
    kind_config: Mapping[str, Any] | None,
    logger: StructuredLogger,
) -> dict[str, Any]:
    return {
        "rubric": entry.rubric,
        "agent_system_prompt": entry.agent_system_prompt,
        "transcript": list(entry.transcript),
        "db_reader": db_reader,
        "kb_search": kb_search,
        "workspace_dir": workspace_dir,
        "extra_read_tools": list(extra_read_tools),
        "state_diff": entry.state_diff,
        "judge_model_config": judge_model_config,
        "judge_model_provider": judge_model_provider,
        "disable_knowledge_search": entry.disable_knowledge_search,
        "custom_system_prompt": entry.custom_system_prompt,
        "include_agent_system_prompt": entry.include_agent_system_prompt,
        "kind_config": kind_config,
        "logger": logger,
    }


def _check_partial_pair(
    *,
    entry: ParityCorpusEntry,
    reference_result: JudgeResult,
    candidate_result: JudgeResult,
    candidate_label: str = "candidate",
) -> str | None:
    """Return a diagnostic string when either leg dropped criteria, else ``None``.

    A leg qualifies as partial when its ``criterion_results`` covers
    fewer criterion ids than the rubric declares, or when its ``status``
    is not ``COMPLETED``. Callers add the returned string to the report's
    ``errored_fixture_ids`` and skip the pair.
    """
    rubric_ids = tuple(c.id for c in entry.rubric.criteria)
    ref_short = _short_reason(reference_result, rubric_ids, "reference")
    cand_short = _short_reason(candidate_result, rubric_ids, candidate_label)
    if ref_short is None and cand_short is None:
        return None
    parts = [f"entry={entry.entry_id}"]
    if ref_short is not None:
        parts.append(ref_short)
    if cand_short is not None:
        parts.append(cand_short)
    return "; ".join(parts)


def _short_reason(result: JudgeResult, rubric_ids: tuple[str, ...], leg_label: str) -> str | None:
    """Diagnose one leg — ``None`` when it graded every criterion cleanly."""
    if result.status is not JudgeStatus.COMPLETED:
        return f"{leg_label} status={result.status.value}"
    observed = {cr.id for cr in result.criterion_results}
    missing = [cid for cid in rubric_ids if cid not in observed]
    if missing:
        return f"{leg_label} missing criteria {missing}"
    return None


def _pair_observations(
    entry: ParityCorpusEntry,
    reference_result: JudgeResult,
    candidate_result: JudgeResult,
    *,
    observation_suffix: str | None = None,
) -> list[CriterionObservation]:
    """Zip the two legs' per-criterion verdicts into paired observations.

    Both legs are known to be ``COMPLETED`` with full criterion coverage
    when this runs (:func:`_check_partial_pair` filters short legs
    upstream). ``observation_suffix`` disambiguates same-entry pairs in
    self-consistency mode where every later replay pairs against the
    baseline.
    """
    obs_id = (
        f"{entry.entry_id}@{observation_suffix}"
        if observation_suffix is not None
        else entry.entry_id
    )
    ref_by_id = {cr.id: cr for cr in reference_result.criterion_results}
    cand_by_id = {cr.id: cr for cr in candidate_result.criterion_results}
    paired: list[CriterionObservation] = []
    for criterion in entry.rubric.criteria:
        ref_cr = ref_by_id[criterion.id]
        cand_cr = cand_by_id[criterion.id]
        paired.append(
            CriterionObservation(
                observation_id=obs_id,
                criterion_id=criterion.id,
                reference_met=bool(ref_cr.met),
                candidate_met=bool(cand_cr.met),
                reference_raw=ref_cr.met if criterion.kind == "binary" else ref_cr.score,
                candidate_raw=cand_cr.met if criterion.kind == "binary" else cand_cr.score,
                justification=cand_cr.justification,
            )
        )
    return paired
