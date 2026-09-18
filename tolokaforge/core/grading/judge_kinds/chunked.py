"""Chunked-rubric impl of :class:`JudgeKind` — one :class:`LLMJudge` per chunk.

Registered under the name ``chunked_rubric`` in the
``tolokaforge.judge_kinds`` entry-point group. Partitions the rubric's
criteria into chunks of at most ``chunk_size`` — first grouping criteria
that share a ``Criterion.chunk_group`` name into the same chunk (or
consecutive chunks when a group's size exceeds ``chunk_size``), then
packing every other criterion (and every complete group block that
fits) in first-appearance order. When ``kind_config`` omits
``chunk_size``, the effective size is derived from
``judge_model_config.max_tokens`` via :func:`_adaptive_chunk_size` so a
large-context judge degenerates to a single call when the whole rubric
fits in its output-token headroom. Runs one
:class:`LLMJudge` per chunk against a scoped sub-rubric (sharing the
original ``reference``) and merges the per-chunk
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

Fail-loud: ANY chunk that returns ``status != COMPLETED`` or is missing
one of its chunk's criterion ids yields a whole-trial
:attr:`JudgeStatus.ERRORED` :class:`JudgeResult` with ``score=None`` and
``criterion_results=()``. The failing chunk index and its criterion ids
plus the underlying reason are surfaced in the merged ``reasons``, and
``chunk_boundaries`` is still populated with every boundary attempted so
bundle persistence and offline replay can retry only the failing chunk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge import LLMJudge
from tolokaforge.core.grading.judge_kinds._shared import (
    CONSTRUCTION_FIELDS,
    assert_construction_fields_match,
    member_failure_reason,
    sum_usage,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus
from tolokaforge.core.grading.rubric import aggregate_rubric
from tolokaforge.runner.models import Criterion, CriterionResult, Rubric

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.tools.registry import Tool

__all__ = [
    "FALLBACK_MAX_TOKENS",
    "HEADROOM_FRACTION",
    "TOKENS_PER_CRITERION_ESTIMATE",
    "ChunkedRubricJudgeKind",
]

#: Per-criterion verdict output-token estimate. Module-level so callers can
#: monkeypatch it in tests; production code treats it as fixed.
TOKENS_PER_CRITERION_ESTIMATE = 200

#: Fraction of ``ModelConfig.max_tokens`` the adaptive heuristic packs criteria
#: into. The complement (40 %) is reserved for the judge's reasoning tokens and
#: a retry buffer.
HEADROOM_FRACTION = 0.6

#: Conservative stand-in when ``ModelConfig.max_tokens`` is ``None`` (unset).
#: Combined with the defaults above this yields six criteria per chunk on the
#: fallback path.
FALLBACK_MAX_TOKENS = 2048

#: Accepted ``kind_config`` keys; every other key raises ``ValueError``.
_ACCEPTED_KIND_CONFIG_KEYS = frozenset({"chunk_size"})


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
        chunk_size = _resolve_chunk_size(kind_config, judge_model_config)
        chunks = _chunk_boundaries(rubric.criteria, chunk_size)
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
            failure = member_failure_reason(chunk_result, chunk_boundaries[chunk_index])
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


def _resolve_chunk_size(
    kind_config: Mapping[str, Any] | None,
    judge_model_config: ModelConfig,
) -> int:
    """Validate ``kind_config`` and return the effective chunk size.

    When ``kind_config`` omits ``chunk_size`` (or is itself ``None``), the size
    is derived from the judge model's output-token headroom via
    :func:`_adaptive_chunk_size`. An explicit ``chunk_size`` always wins.
    Raises :class:`ValueError` on any unknown key or a non-positive
    ``chunk_size`` before any judge dispatch runs.
    """
    if kind_config is None:
        return _adaptive_chunk_size(judge_model_config)
    unknown = set(kind_config) - _ACCEPTED_KIND_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"chunked_rubric kind_config contains unknown key(s): {sorted(unknown)}. "
            f"Accepted keys: {sorted(_ACCEPTED_KIND_CONFIG_KEYS)}."
        )
    raw = kind_config.get("chunk_size")
    if raw is None:
        return _adaptive_chunk_size(judge_model_config)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(
            f"chunked_rubric chunk_size must be an int; got {type(raw).__name__} {raw!r}."
        )
    if raw < 1:
        raise ValueError(f"chunked_rubric chunk_size must be >= 1; got {raw}.")
    return raw


def _adaptive_chunk_size(judge_model_config: ModelConfig) -> int:
    """Derive ``chunk_size`` from the judge model's output-token headroom.

    Packs criteria into :data:`HEADROOM_FRACTION` of the model's
    ``max_tokens`` at :data:`TOKENS_PER_CRITERION_ESTIMATE` tokens per
    criterion. Falls back to :data:`FALLBACK_MAX_TOKENS` when ``max_tokens``
    is unset. The ``max(1, ...)`` floor guarantees a legal partition even
    under pathological configs (e.g. a user setting ``max_tokens=100``).
    """
    max_tokens = judge_model_config.max_tokens
    if max_tokens is None:
        max_tokens = FALLBACK_MAX_TOKENS
    return max(1, int((max_tokens * HEADROOM_FRACTION) // TOKENS_PER_CRITERION_ESTIMATE))


def _chunk_boundaries(criteria: Sequence[Criterion], chunk_size: int) -> list[list[Criterion]]:
    """Partition ``criteria`` into chunks of at most ``chunk_size`` criteria.

    Two-phase, deterministic, no I/O. First groups criteria sharing a
    ``Criterion.chunk_group`` name into one block anchored at that name's
    first-occurrence position; criteria with ``chunk_group is None`` are
    each their own singleton block. Then packs the ordered blocks into
    chunks: a block that fits in the current chunk's remaining room is
    appended; a block that does not fit but is itself ``<= chunk_size``
    flushes the current chunk and starts a new one with that block; a
    block whose own size exceeds ``chunk_size`` flushes the current
    chunk, then is sliced on its own into consecutive ``chunk_size``
    runs (never combined with another block).

    When no criterion declares ``chunk_group``, every block is a
    singleton in original order, packing degenerates to plain fixed-K
    runs, and the output is identical to
    ``criteria[i : i + chunk_size]`` slicing — the byte-parity anchor
    the κ-parity gate depends on.
    """
    blocks: list[list[Criterion]] = []
    group_block_index: dict[str, int] = {}
    for criterion in criteria:
        if criterion.chunk_group is None:
            blocks.append([criterion])
            continue
        existing_index = group_block_index.get(criterion.chunk_group)
        if existing_index is None:
            group_block_index[criterion.chunk_group] = len(blocks)
            blocks.append([criterion])
        else:
            blocks[existing_index].append(criterion)

    chunks: list[list[Criterion]] = []
    current: list[Criterion] = []
    for block in blocks:
        if len(block) > chunk_size:
            if current:
                chunks.append(current)
                current = []
            for start in range(0, len(block), chunk_size):
                chunks.append(list(block[start : start + chunk_size]))
            continue
        if len(current) + len(block) > chunk_size:
            chunks.append(current)
            current = []
        current.extend(block)
    if current:
        chunks.append(current)
    return chunks


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
        usage=sum_usage(chunk_results),
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
    :data:`CONSTRUCTION_FIELDS` MUST match across chunks — a mismatch raises
    :class:`RuntimeError` naming the field and the divergent values.
    """
    assert_construction_fields_match(
        chunk_results, CONSTRUCTION_FIELDS, kind_label="chunked_rubric", unit_noun="chunk"
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
        usage=sum_usage(chunk_results),
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
