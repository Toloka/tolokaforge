"""Chunked-rubric impl of :class:`JudgeKind` — one :class:`LLMJudge` per chunk.

Registered under the name ``chunked_rubric`` in the
``tolokaforge.judge_kinds`` entry-point group. Splits the rubric's criteria
into fixed-size contiguous chunks of ``chunk_size`` (default 5), runs one
:class:`LLMJudge` per chunk against a scoped sub-rubric (sharing the
original ``reference``), and merges the per-chunk
:class:`~tolokaforge.runner.models.CriterionResult` maps above
:class:`~tolokaforge.core.grading.judge._SubmitReportTermination` before
folding them through :func:`aggregate_rubric` on the ORIGINAL full rubric.

The kind exists to remove the truncation failure class large rubrics
(30+ criteria) drive on ``single_shot_rubric``: a single ``submit_report``
call whose JSON args exceed the judge model's output-token ceiling,
:class:`SubmitReportValidationError` on the missing verdicts, retry
budget exhausts, whole-trial :attr:`JudgeStatus.ERRORED`. With N chunks
of ~K criteria each, no single ``submit_report`` payload is large
enough to truncate.

Fail-loud (#1471): ANY chunk that returns ``status != COMPLETED`` or is
missing one of its chunk's criterion ids yields a whole-trial
:attr:`JudgeStatus.ERRORED` :class:`JudgeResult` with ``score=None`` and
``criterion_results=()``. The failing chunk index and its criterion ids
plus the underlying reason are surfaced in the merged ``reasons``, and
``chunk_boundaries`` is still populated with every boundary attempted
(so #1569's bundle wiring can persist them and offline replay can retry
only the failing chunk).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge import LLMJudge
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.grading.rubric import aggregate_rubric
from tolokaforge.runner.models import CriterionResult, Rubric

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.tools.registry import Tool

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "ChunkedRubricJudgeKind",
]

#: Default number of criteria per chunk when ``kind_config`` omits ``chunk_size``.
#: Aligned with the source ticket's "start at 5" guidance; measurement-driven
#: tuning is deferred to follow-up #1581.
DEFAULT_CHUNK_SIZE = 5

#: Accepted ``kind_config`` keys; every other key raises ``ValueError``.
_ACCEPTED_KIND_CONFIG_KEYS = frozenset({"chunk_size"})

#: Per-chunk fields that MUST be constant across chunks (pure functions of the
#: ``evaluate`` inputs). A mismatch is a defensive lock catching a future kind
#: refactor that accidentally per-chunks one of these inputs.
_CONSTRUCTION_FIELDS = (
    "kb_tools_offered",
    "kb_tools_withheld",
    "knowledge_search_disabled",
    "custom_system_prompt",
    "include_agent_system_prompt",
    "read_tools_offered",
    "state_diff",
)


class ChunkedRubricJudgeKind:
    """Grade a rubric with one :class:`LLMJudge` invocation per fixed-K chunk."""

    NAME: ClassVar[str] = "chunked_rubric"

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
        chunk_size = _resolve_chunk_size(kind_config)
        chunks: list[list] = [
            list(rubric.criteria[i : i + chunk_size])
            for i in range(0, len(rubric.criteria), chunk_size)
        ]
        chunk_boundaries: tuple[tuple[str, ...], ...] = tuple(
            tuple(c.id for c in chunk) for chunk in chunks
        )

        chunk_results: list[JudgeResult] = []
        for chunk_index, chunk_criteria in enumerate(chunks):
            sub_rubric = Rubric(criteria=chunk_criteria, reference=rubric.reference)
            judge_model = judge_model_provider.build(judge_model_config)
            chunk_result = LLMJudge(
                judge_model_config,
                disable_knowledge_search=disable_knowledge_search,
                custom_system_prompt=custom_system_prompt,
                include_agent_system_prompt=include_agent_system_prompt,
                llm_client=judge_model,
                logger=logger,
            ).run(
                rubric=sub_rubric,
                agent_system_prompt=agent_system_prompt,
                transcript=transcript,
                db_reader=db_reader,
                kb_search=kb_search,
                extra_read_tools=list(extra_read_tools),
                workspace_dir=workspace_dir,
                state_diff=state_diff,
            )
            chunk_results.append(chunk_result)
            failure = _chunk_failure_reason(chunk_result, chunk_boundaries[chunk_index])
            if failure is not None:
                return _errored_trial(
                    chunk_results=chunk_results,
                    chunk_boundaries=chunk_boundaries,
                    failing_index=chunk_index,
                    failing_ids=chunk_boundaries[chunk_index],
                    reason=failure,
                )

        return _merge_chunk_results(
            rubric=rubric,
            chunk_results=chunk_results,
            chunk_boundaries=chunk_boundaries,
        )


def _resolve_chunk_size(kind_config: Mapping[str, Any] | None) -> int:
    """Validate ``kind_config`` and return the effective chunk size.

    Raises :class:`ValueError` on any unknown key or a non-positive
    ``chunk_size`` before any judge dispatch runs.
    """
    if kind_config is None:
        return DEFAULT_CHUNK_SIZE
    unknown = set(kind_config) - _ACCEPTED_KIND_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"chunked_rubric kind_config contains unknown key(s): {sorted(unknown)}. "
            f"Accepted keys: {sorted(_ACCEPTED_KIND_CONFIG_KEYS)}."
        )
    raw = kind_config.get("chunk_size")
    if raw is None:
        return DEFAULT_CHUNK_SIZE
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(
            f"chunked_rubric chunk_size must be an int; got {type(raw).__name__} {raw!r}."
        )
    if raw < 1:
        raise ValueError(f"chunked_rubric chunk_size must be >= 1; got {raw}.")
    return raw


def _chunk_failure_reason(chunk_result: JudgeResult, chunk_ids: tuple[str, ...]) -> str | None:
    """Return a failure reason string if the chunk did not COMPLETE cleanly."""
    if chunk_result.status is not JudgeStatus.COMPLETED:
        return f"status={chunk_result.status.value}: {chunk_result.reasons}"
    covered = {cr.id for cr in chunk_result.criterion_results}
    missing = [cid for cid in chunk_ids if cid not in covered]
    if missing:
        return f"missing verdicts for criterion ids {missing}: {chunk_result.reasons}"
    return None


def _errored_trial(
    *,
    chunk_results: list[JudgeResult],
    chunk_boundaries: tuple[tuple[str, ...], ...],
    failing_index: int,
    failing_ids: tuple[str, ...],
    reason: str,
) -> JudgeResult:
    """Compose the whole-trial ERRORED :class:`JudgeResult` for a chunk failure.

    ``chunk_boundaries`` carries every boundary attempted (including chunks
    that never ran) so #1569 can persist them and offline replay can retry
    only the failing chunk. Usage is summed across every chunk that dispatched
    so the errored trial still records real cost.
    """
    return JudgeResult(
        status=JudgeStatus.ERRORED,
        usage=_sum_usage(chunk_results),
        reasons=(
            f"chunked_rubric failed on chunk {failing_index} "
            f"(criterion ids {list(failing_ids)}): {reason}"
        ),
        score=None,
        binary_pass=None,
        chunk_boundaries=chunk_boundaries,
    )


def _merge_chunk_results(
    *,
    rubric: Rubric,
    chunk_results: list[JudgeResult],
    chunk_boundaries: tuple[tuple[str, ...], ...],
) -> JudgeResult:
    """Fold per-chunk :class:`JudgeResult`s into one whole-trial result.

    Every chunk here is COMPLETED and covers its own criterion ids (the fail-loud
    guard ran before this call). The construction-time fields listed in
    :data:`_CONSTRUCTION_FIELDS` MUST match across chunks — a mismatch raises
    :class:`RuntimeError` naming the field and the divergent values.
    """
    for field in _CONSTRUCTION_FIELDS:
        head_value = getattr(chunk_results[0], field)
        for chunk_index, chunk_result in enumerate(chunk_results[1:], start=1):
            other_value = getattr(chunk_result, field)
            if other_value != head_value:
                raise RuntimeError(
                    f"chunked_rubric construction-field mismatch across chunks: "
                    f"{field!r} on chunk 0 is {head_value!r} but chunk "
                    f"{chunk_index} is {other_value!r}. Every chunk shares the "
                    f"same evaluate inputs; a divergence signals a kind refactor "
                    f"that accidentally per-chunks a construction input."
                )

    by_id: dict[str, CriterionResult] = {}
    for chunk_result in chunk_results:
        for cr in chunk_result.criterion_results:
            by_id[cr.id] = cr
    merged_results = [by_id[c.id] for c in rubric.criteria]

    aggregate = aggregate_rubric(rubric, merged_results)
    head = chunk_results[0]
    return JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=_sum_usage(chunk_results),
        reasons="\n\n".join(cr.reasons for cr in chunk_results),
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
        transcript=tuple(turn for cr in chunk_results for turn in cr.transcript),
        chunk_boundaries=chunk_boundaries,
    )


def _sum_usage(chunk_results: list[JudgeResult]) -> JudgeUsage:
    """Field-wise sum of per-chunk :class:`JudgeUsage`."""
    return JudgeUsage(
        calls=sum(cr.usage.calls for cr in chunk_results),
        prompt_tokens=sum(cr.usage.prompt_tokens for cr in chunk_results),
        completion_tokens=sum(cr.usage.completion_tokens for cr in chunk_results),
        reasoning_tokens=sum(cr.usage.reasoning_tokens for cr in chunk_results),
        cost_usd=sum(cr.usage.cost_usd for cr in chunk_results),
        tool_calls=sum(cr.usage.tool_calls for cr in chunk_results),
        consistency_rejections=sum(cr.usage.consistency_rejections for cr in chunk_results),
    )
