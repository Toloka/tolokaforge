"""Voted-rubric impl of :class:`JudgeKind` — K-sample variance reducer.

Registered under the name ``voted_rubric`` in the ``tolokaforge.judge_kinds``
entry-point group. Wraps any other registered :class:`JudgeKind` (default
``single_shot_rubric``), samples it ``n_samples`` times (default 3) against
the SAME rubric evidence, and folds the K per-criterion verdicts through a
robust aggregator (:mod:`tolokaforge.core.grading.judge_kinds.aggregators`)
to reduce judge-model self-variance on subjective/graded criteria.

Fail-loud (mirrors ``chunked.py``'s per-chunk contract, renamed to
per-sample): any sample whose ``JudgeResult.status != COMPLETED``, or whose
``criterion_results`` is missing one of the rubric's criterion ids, yields a
whole-trial :attr:`JudgeStatus.ERRORED` result naming the failing sample
index and reason. Usage is still summed across every sample that dispatched.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge_kinds import aggregators
from tolokaforge.core.grading.judge_kinds._shared import (
    CONSTRUCTION_FIELDS,
    assert_construction_fields_match,
    member_failure_reason,
    sum_usage,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus
from tolokaforge.core.grading.rubric import GRADED_MET_THRESHOLD, aggregate_rubric
from tolokaforge.runner.models import CriterionResult

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "DEFAULT_AGGREGATOR",
    "DEFAULT_N_SAMPLES",
    "DEFAULT_WRAPPED_KIND",
    "VotedRubricJudgeKind",
]

#: Default K when ``kind_config`` omits ``n_samples``.
DEFAULT_N_SAMPLES = 3

#: Default aggregator when ``kind_config`` omits ``aggregator``.
DEFAULT_AGGREGATOR = "geometric_median"

#: Default wrapped kind when ``kind_config`` omits ``wrapped_kind``.
DEFAULT_WRAPPED_KIND = "single_shot_rubric"

#: Accepted ``kind_config`` keys; every other key raises ``ValueError``.
_ACCEPTED_KIND_CONFIG_KEYS = frozenset({"n_samples", "aggregator", "wrapped_kind"})


class VotedRubricJudgeKind:
    """Grade a rubric with K samples of a wrapped :class:`JudgeKind`, robustly aggregated."""

    NAME: ClassVar[str] = "voted_rubric"

    def evaluate(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None,
        kb_search: KnowledgeSearch | None,
        workspace_dir: Path | None,
        extra_read_tools: list[Tool],
        state_diff: str | None,
        judge_model_config: ModelConfig,
        judge_model_provider: JudgeModelProvider,
        disable_knowledge_search: bool,
        custom_system_prompt: str | None,
        include_agent_system_prompt: bool,
        kind_config: Mapping[str, Any] | None,
        logger: StructuredLogger,
    ) -> JudgeResult:
        n_samples, aggregator, wrapped_kind_name = _resolve_kind_config(kind_config)
        aggregators.validate_sample_count(n_samples, aggregator=aggregator)
        if aggregator == "majority":
            aggregators.require_binary_only(rubric.criteria)

        # Lazy import: a module-level import would cycle — plugin_registry
        # imports tolokaforge.core.grading.judge_kinds (this package's
        # __init__.py) at its own module scope, and that __init__.py exports
        # VotedRubricJudgeKind, so a module-level import back into
        # plugin_registry from here is a live circular import.
        from tolokaforge.core.plugin_registry import load_judge_kind

        wrapped_kind_instance = load_judge_kind(wrapped_kind_name)()

        criterion_ids = tuple(c.id for c in rubric.criteria)
        sample_results: list[JudgeResult] = []
        for sample_index in range(n_samples):
            sample_result = wrapped_kind_instance.evaluate(
                rubric=rubric,
                agent_system_prompt=agent_system_prompt,
                transcript=transcript,
                db_reader=db_reader,
                kb_search=kb_search,
                workspace_dir=workspace_dir,
                extra_read_tools=list(extra_read_tools),
                state_diff=state_diff,
                judge_model_config=judge_model_config,
                judge_model_provider=judge_model_provider,
                disable_knowledge_search=disable_knowledge_search,
                custom_system_prompt=custom_system_prompt,
                include_agent_system_prompt=include_agent_system_prompt,
                kind_config=None,
                logger=logger,
            )
            sample_results.append(sample_result)
            failure = member_failure_reason(sample_result, criterion_ids)
            if failure is not None:
                return _errored_trial(
                    sample_results=sample_results,
                    failing_index=sample_index,
                    reason=failure,
                )

        return _merge_sample_results(
            rubric=rubric, sample_results=sample_results, aggregator=aggregator
        )


def _resolve_kind_config(kind_config: Mapping[str, Any] | None) -> tuple[int, str, str]:
    """Validate ``kind_config`` and return ``(n_samples, aggregator, wrapped_kind)``.

    Raises :class:`ValueError` on any unknown key, a non-``int``/``bool``
    ``n_samples``, or an unrecognised ``aggregator`` — before any judge
    dispatch runs. Does NOT validate ``n_samples``'s value range or
    majority/binary-rubric compatibility; the caller runs
    :func:`aggregators.validate_sample_count` / :func:`aggregators.require_binary_only`
    on the returned values.
    """
    if kind_config is None:
        return DEFAULT_N_SAMPLES, DEFAULT_AGGREGATOR, DEFAULT_WRAPPED_KIND

    unknown = set(kind_config) - _ACCEPTED_KIND_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"voted_rubric kind_config contains unknown key(s): {sorted(unknown)}. "
            f"Accepted keys: {sorted(_ACCEPTED_KIND_CONFIG_KEYS)}."
        )

    raw_n_samples = kind_config.get("n_samples", DEFAULT_N_SAMPLES)
    if not isinstance(raw_n_samples, int) or isinstance(raw_n_samples, bool):
        raise ValueError(
            f"voted_rubric n_samples must be an int; got "
            f"{type(raw_n_samples).__name__} {raw_n_samples!r}."
        )

    aggregator = kind_config.get("aggregator", DEFAULT_AGGREGATOR)
    aggregators.validate_aggregator_name(aggregator)

    wrapped_kind = kind_config.get("wrapped_kind", DEFAULT_WRAPPED_KIND)

    return raw_n_samples, aggregator, wrapped_kind


def _errored_trial(
    *,
    sample_results: list[JudgeResult],
    failing_index: int,
    reason: str,
) -> JudgeResult:
    """Compose the whole-trial ERRORED :class:`JudgeResult` for a sample failure.

    Usage is summed across every sample that dispatched so the errored trial
    still records real cost.
    """
    return JudgeResult(
        status=JudgeStatus.ERRORED,
        usage=sum_usage(sample_results),
        reasons=f"voted_rubric failed on sample {failing_index}: {reason}",
        score=None,
        binary_pass=None,
    )


def _build_criterion_justification(
    *,
    aggregator: str,
    criterion_scores: list[float],
    aggregate_score: float,
    sample_justifications: list[str],
) -> str:
    """Audit-trail justification: aggregator, K, per-sample scores/aggregate, then each sample's own text."""
    header = (
        f"voted_rubric aggregator={aggregator} K={len(criterion_scores)} "
        f"per_sample_scores={criterion_scores} aggregate={aggregate_score}"
    )
    per_sample = "\n".join(
        f"[sample {i}] {justification}" for i, justification in enumerate(sample_justifications)
    )
    return f"{header}\n{per_sample}"


def _merge_sample_results(
    *,
    rubric: Rubric,
    sample_results: list[JudgeResult],
    aggregator: str,
) -> JudgeResult:
    """Fold K per-sample :class:`JudgeResult`s into one whole-trial result.

    Every sample here is COMPLETED and covers every rubric criterion id (the
    fail-loud guard ran before this call). The construction-time fields
    listed in :data:`CONSTRUCTION_FIELDS` MUST match across samples — a
    mismatch raises :class:`RuntimeError` naming the field and the divergent
    values.
    """
    assert_construction_fields_match(
        sample_results, CONSTRUCTION_FIELDS, kind_label="voted_rubric", unit_noun="sample"
    )

    by_sample_by_id: list[dict[str, CriterionResult]] = [
        {cr.id: cr for cr in sample_result.criterion_results} for sample_result in sample_results
    ]

    per_sample_scores = [
        [by_id[criterion.id].score for criterion in rubric.criteria] for by_id in by_sample_by_id
    ]
    aggregate_scores = aggregators.aggregate_scores(
        aggregator=aggregator, per_sample_scores=per_sample_scores
    )

    merged_results: list[CriterionResult] = []
    for criterion_index, criterion in enumerate(rubric.criteria):
        aggregate_score = aggregate_scores[criterion_index]
        criterion_scores = [row[criterion_index] for row in per_sample_scores]
        sample_justifications = [by_id[criterion.id].justification for by_id in by_sample_by_id]
        merged_results.append(
            CriterionResult(
                id=criterion.id,
                met=aggregate_score >= GRADED_MET_THRESHOLD,
                score=aggregate_score,
                justification=_build_criterion_justification(
                    aggregator=aggregator,
                    criterion_scores=criterion_scores,
                    aggregate_score=aggregate_score,
                    sample_justifications=sample_justifications,
                ),
            )
        )

    aggregate = aggregate_rubric(rubric, merged_results)
    head = sample_results[0]
    return JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=sum_usage(sample_results),
        reasons="\n\n".join(cr.reasons for cr in sample_results),
        score=aggregate.score,
        binary_pass=aggregate.binary_pass,
        gate_failed=aggregate.gate_failed,
        criterion_results=tuple(merged_results),
        failed_required_ids=aggregate.failed_required_ids,
        kb_tools_offered=head.kb_tools_offered,
        kb_tools_withheld=head.kb_tools_withheld,
        knowledge_search_disabled=head.knowledge_search_disabled,
        custom_system_prompt=head.custom_system_prompt,
        include_agent_system_prompt=head.include_agent_system_prompt,
        read_tools_offered=head.read_tools_offered,
        state_diff=head.state_diff,
        transcript=tuple(turn for cr in sample_results for turn in cr.transcript),
        chunk_boundaries=(),
    )
