"""Jury-rubric impl of :class:`JudgeKind` — cross-family panel variance reducer.

Registered under the name ``jury_rubric`` in the ``tolokaforge.judge_kinds``
entry-point group. Wraps any other registered :class:`JudgeKind` (default
``single_shot_rubric``), dispatches it once per panel member — each member
supplying its OWN :class:`~tolokaforge.core.models.ModelConfig` (a different
provider/model, not just a different sample) — against the SAME rubric
evidence, and folds the per-member per-criterion verdicts through the same
robust aggregator (:mod:`tolokaforge.core.grading.judge_kinds.aggregators`)
``voted_rubric`` uses. Where ``voted_rubric`` reduces one model's own
self-variance via K samples, ``jury_rubric`` trades that for cross-family
(PoLL) diversity at the same K-dispatch cost.

Fail-loud (mirrors ``voted.py``'s per-sample contract, renamed to
per-panel-member): any member whose ``JudgeResult.status != COMPLETED``, or
whose ``criterion_results`` is missing one of the rubric's criterion ids,
yields a whole-trial :attr:`JudgeStatus.ERRORED` result naming the failing
member's index AND its provider/name — panel members are heterogeneous, so
naming which model failed is the point. Usage is still summed across every
member that dispatched.

Credential preflight: before any panel member is dispatched, every DISTINCT
provider across the panel is checked against
:func:`tolokaforge.core.llm.providers.credential_env_names` and
:class:`~tolokaforge.secrets.SecretManager`.has_secret — any provider with
no matching secret raises, naming EVERY missing provider in one error, not
just the first.
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
from tolokaforge.core.llm.providers import credential_env_names
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import CriterionResult
from tolokaforge.secrets import get_default

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "DEFAULT_AGGREGATOR",
    "DEFAULT_PANEL",
    "DEFAULT_WRAPPED_KIND",
    "JuryRubricJudgeKind",
]

#: Default aggregator when ``kind_config`` omits ``aggregator``.
DEFAULT_AGGREGATOR = "geometric_median"

#: Default wrapped kind when ``kind_config`` omits ``wrapped_kind``.
DEFAULT_WRAPPED_KIND = "single_shot_rubric"

#: Default cross-family panel when ``kind_config`` omits ``panel`` — three
#: cheap, different-vendor models, all routed via OpenRouter.
DEFAULT_PANEL: tuple[Mapping[str, Any], ...] = (
    {"provider": "openrouter", "name": "openai/gpt-4o-mini", "temperature": 0.0},
    {"provider": "openrouter", "name": "anthropic/claude-3-haiku", "temperature": 0.0},
    {"provider": "openrouter", "name": "google/gemini-2.0-flash", "temperature": 0.0},
)

#: Accepted ``kind_config`` keys; every other key raises ``ValueError``.
_ACCEPTED_KIND_CONFIG_KEYS = frozenset(
    {"panel", "aggregator", "wrapped_kind", "wrapped_kind_config"}
)

#: Accepted keys per ``panel`` entry; every other key raises ``ValueError``.
_ACCEPTED_PANEL_ENTRY_KEYS = frozenset({"provider", "name", "temperature"})


class JuryRubricJudgeKind:
    """Grade a rubric with a cross-family panel of N judge models, robustly aggregated."""

    NAME: ClassVar[str] = "jury_rubric"

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
        panel, aggregator, wrapped_kind_name, wrapped_kind_config = _resolve_kind_config(
            kind_config
        )
        aggregators.validate_sample_count(len(panel), aggregator=aggregator)
        if aggregator == "majority":
            aggregators.require_binary_only(rubric.criteria)
        _preflight_panel_credentials(panel)

        # Lazy import: a module-level import would cycle — plugin_registry
        # imports tolokaforge.core.grading.judge_kinds (this package's
        # __init__.py) at its own module scope, and that __init__.py exports
        # JuryRubricJudgeKind, so a module-level import back into
        # plugin_registry from here is a live circular import.
        from tolokaforge.core.plugin_registry import load_judge_kind

        wrapped_kind_instance = load_judge_kind(wrapped_kind_name)()

        criterion_ids = tuple(c.id for c in rubric.criteria)
        member_results: list[JudgeResult] = []
        for member_index, member in enumerate(panel):
            member_model_config = _build_panel_model_config(judge_model_config, member)
            member_result = wrapped_kind_instance.evaluate(
                rubric=rubric,
                agent_system_prompt=agent_system_prompt,
                transcript=transcript,
                db_reader=db_reader,
                kb_search=kb_search,
                workspace_dir=workspace_dir,
                extra_read_tools=list(extra_read_tools),
                state_diff=state_diff,
                judge_model_config=member_model_config,
                judge_model_provider=judge_model_provider,
                disable_knowledge_search=disable_knowledge_search,
                custom_system_prompt=custom_system_prompt,
                include_agent_system_prompt=include_agent_system_prompt,
                kind_config=wrapped_kind_config,
                logger=logger,
            )
            member_results.append(member_result)
            failure = member_failure_reason(member_result, criterion_ids)
            if failure is not None:
                return _errored_trial(
                    member_results=member_results,
                    panel=panel,
                    failing_index=member_index,
                    reason=failure,
                )

        return _merge_member_results(
            rubric=rubric, member_results=member_results, panel=panel, aggregator=aggregator
        )


def _resolve_kind_config(
    kind_config: Mapping[str, Any] | None,
) -> tuple[tuple[Mapping[str, Any], ...], str, str, Mapping[str, Any] | None]:
    """Validate ``kind_config`` and return
    ``(panel, aggregator, wrapped_kind, wrapped_kind_config)``.

    Raises :class:`ValueError` on any unknown top-level or panel-entry key, a
    malformed panel entry, a non-``str``/empty ``wrapped_kind``, a
    non-mapping ``wrapped_kind_config``, or an unrecognised ``aggregator`` —
    before any judge dispatch runs. ``wrapped_kind_config`` is forwarded
    verbatim to the wrapped kind so a caller can tune the inner kind (e.g.
    an explicit ``chunk_size`` when wrapping ``chunked_rubric``).
    """
    if kind_config is None:
        return DEFAULT_PANEL, DEFAULT_AGGREGATOR, DEFAULT_WRAPPED_KIND, None

    unknown = set(kind_config) - _ACCEPTED_KIND_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"jury_rubric kind_config contains unknown key(s): {sorted(unknown)}. "
            f"Accepted keys: {sorted(_ACCEPTED_KIND_CONFIG_KEYS)}."
        )

    panel = _validate_panel(kind_config.get("panel", DEFAULT_PANEL))

    aggregator = kind_config.get("aggregator", DEFAULT_AGGREGATOR)
    aggregators.validate_aggregator_name(aggregator)

    wrapped_kind = kind_config.get("wrapped_kind", DEFAULT_WRAPPED_KIND)
    if not isinstance(wrapped_kind, str) or not wrapped_kind:
        raise ValueError(
            f"jury_rubric wrapped_kind must be a non-empty str; got "
            f"{type(wrapped_kind).__name__} {wrapped_kind!r}."
        )

    wrapped_kind_config = kind_config.get("wrapped_kind_config")
    if wrapped_kind_config is not None and not isinstance(wrapped_kind_config, Mapping):
        raise ValueError(
            f"jury_rubric wrapped_kind_config must be a mapping or None; got "
            f"{type(wrapped_kind_config).__name__} {wrapped_kind_config!r}."
        )

    return panel, aggregator, wrapped_kind, wrapped_kind_config


def _validate_panel(raw_panel: Any) -> tuple[Mapping[str, Any], ...]:
    """Validate ``panel``'s shape and every entry's schema.

    Raises :class:`ValueError` naming the entry index and the offending
    field for any malformed entry, before any judge dispatch runs.
    """
    if not isinstance(raw_panel, (list, tuple)):
        raise ValueError(
            f"jury_rubric panel must be a list or tuple of mappings; "
            f"got {type(raw_panel).__name__}."
        )
    validated: list[Mapping[str, Any]] = []
    for index, entry in enumerate(raw_panel):
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"jury_rubric panel entry {index} must be a mapping; got {type(entry).__name__}."
            )
        unknown = set(entry) - _ACCEPTED_PANEL_ENTRY_KEYS
        if unknown:
            raise ValueError(
                f"jury_rubric panel entry {index} contains unknown key(s): {sorted(unknown)}. "
                f"Accepted keys: {sorted(_ACCEPTED_PANEL_ENTRY_KEYS)}."
            )
        for field in ("provider", "name"):
            value = entry.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"jury_rubric panel entry {index} must have a non-empty str {field!r}; "
                    f"got {value!r}."
                )
        if "temperature" in entry:
            temperature = entry["temperature"]
            if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
                raise ValueError(
                    f"jury_rubric panel entry {index} temperature must be an int or float; "
                    f"got {type(temperature).__name__} {temperature!r}."
                )
        validated.append(entry)
    return tuple(validated)


def _preflight_panel_credentials(panel: tuple[Mapping[str, Any], ...]) -> None:
    """Raise :class:`ValueError` naming every panel provider missing a credential.

    Reads only via :class:`~tolokaforge.secrets.SecretManager`.has_secret — no
    ``os.environ`` access. A provider with no known credential-name mapping
    (:func:`credential_env_names` returns ``()``) cannot be preflighted and is
    skipped, mirroring today's behaviour of failing loud at the LLM call
    itself instead.
    """
    secret_manager = get_default()
    seen_providers: list[str] = []
    missing: list[str] = []
    for member in panel:
        provider = member["provider"]
        if provider in seen_providers:
            continue
        seen_providers.append(provider)
        candidates = credential_env_names(provider)
        if not candidates:
            continue
        if not any(secret_manager.has_secret(name) for name in candidates):
            missing.append(f"panel provider={provider!r} needs one of {candidates}")
    if missing:
        raise ValueError("jury_rubric panel credential preflight failed: " + "; ".join(missing))


def _build_panel_model_config(base: ModelConfig, member: Mapping[str, Any]) -> ModelConfig:
    """Build one panel member's :class:`ModelConfig` from ``base`` plus its overrides.

    Re-runs :meth:`ModelConfig.model_validate` (not ``model_copy``, which
    skips validators) so ``base``'s own ``openrouter:`` routing block re-fires
    ``ModelConfig``'s ``_reject_openrouter_on_other_providers`` validator
    against the panel member's ``provider`` — a base config carrying an
    OpenRouter routing block combined with a non-OpenRouter panel member is a
    real, reachable misconfiguration that must surface, not silently drop.
    """
    overrides: dict[str, Any] = {"provider": member["provider"], "name": member["name"]}
    if "temperature" in member:
        overrides["temperature"] = member["temperature"]
    return ModelConfig.model_validate({**base.model_dump(mode="python"), **overrides})


def _errored_trial(
    *,
    member_results: list[JudgeResult],
    panel: tuple[Mapping[str, Any], ...],
    failing_index: int,
    reason: str,
) -> JudgeResult:
    """Compose the whole-trial ERRORED :class:`JudgeResult` for a panel-member failure.

    Names the failing member's provider/name alongside its index — panel
    members are heterogeneous, so naming which model failed is the point.
    Usage is summed across every member that dispatched.
    """
    failing_member = panel[failing_index]
    return JudgeResult(
        status=JudgeStatus.ERRORED,
        usage=sum_usage(member_results),
        reasons=(
            f"jury_rubric failed on panel member {failing_index} "
            f"({failing_member['provider']}/{failing_member['name']}): {reason}"
        ),
        score=None,
        binary_pass=None,
    )


def _build_panel_justification(
    *,
    aggregator: str,
    criterion_scores: list[float],
    aggregate_score: float,
    panel: tuple[Mapping[str, Any], ...],
    member_justifications: list[str],
) -> str:
    """Audit-trail justification: aggregator, N, per-member scores/aggregate, then
    each member's own text labelled by its panel provider/model."""
    header = (
        f"jury_rubric aggregator={aggregator} N={len(criterion_scores)} "
        f"per_member_scores={criterion_scores} aggregate={aggregate_score}"
    )
    per_member = "\n".join(
        f"[panel {i}: {member['provider']}/{member['name']}] {justification}"
        for i, (member, justification) in enumerate(zip(panel, member_justifications, strict=True))
    )
    return f"{header}\n{per_member}"


def _merge_member_results(
    *,
    rubric: Rubric,
    member_results: list[JudgeResult],
    panel: tuple[Mapping[str, Any], ...],
    aggregator: str,
) -> JudgeResult:
    """Fold N per-panel-member :class:`JudgeResult`s into one whole-trial result.

    Every member here is COMPLETED and covers every rubric criterion id (the
    fail-loud guard ran before this call). The construction-time fields
    listed in :data:`CONSTRUCTION_FIELDS` MUST match across members — a
    mismatch raises :class:`RuntimeError` naming the field and the divergent
    values.
    """
    assert_construction_fields_match(
        member_results, CONSTRUCTION_FIELDS, kind_label="jury_rubric", unit_noun="panel member"
    )

    by_member_by_id: list[dict[str, CriterionResult]] = [
        {cr.id: cr for cr in member_result.criterion_results} for member_result in member_results
    ]

    per_member_scores = [
        [by_id[criterion.id].score for criterion in rubric.criteria] for by_id in by_member_by_id
    ]
    aggregate_scores = aggregators.aggregate_scores(
        aggregator=aggregator, per_sample_scores=per_member_scores
    )

    merged_results: list[CriterionResult] = []
    for criterion_index, criterion in enumerate(rubric.criteria):
        aggregate_score = aggregate_scores[criterion_index]
        criterion_scores = [row[criterion_index] for row in per_member_scores]
        member_justifications = [by_id[criterion.id].justification for by_id in by_member_by_id]
        merged_results.append(
            CriterionResult(
                id=criterion.id,
                met=aggregate_score >= GRADED_MET_THRESHOLD,
                score=aggregate_score,
                justification=_build_panel_justification(
                    aggregator=aggregator,
                    criterion_scores=criterion_scores,
                    aggregate_score=aggregate_score,
                    panel=panel,
                    member_justifications=member_justifications,
                ),
            )
        )

    aggregate = aggregate_rubric(rubric, merged_results)
    head = member_results[0]
    return JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=sum_usage(member_results),
        reasons="\n\n".join(cr.reasons for cr in member_results),
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
        transcript=tuple(turn for cr in member_results for turn in cr.transcript),
        # Preserve chunk_boundaries from the head member when the wrapped
        # kind is a chunking kind — every panel member sees the same rubric,
        # so the boundaries are identical. Downstream offline-replay routes
        # on this field; erasing it would strip the signal on composed
        # (jury+chunked) configurations.
        chunk_boundaries=head.chunk_boundaries,
    )
