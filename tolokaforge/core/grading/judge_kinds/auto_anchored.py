"""Auto-anchored-rubric impl of :class:`JudgeKind` — cached judge-authored anchors.

Registered under the name ``auto_anchored_rubric`` in the
``tolokaforge.judge_kinds`` entry-point group. Wraps any other registered
:class:`JudgeKind` (default ``single_shot_rubric``). Before delegating to the
wrapped kind, this kind runs a **single warm-up judge call per unique
(rubric, judge_model) tuple** to produce a one-sentence anchor for every
``kind: graded`` criterion the author left with ``expected: None``, caches
those anchors in-process, and then dispatches the wrapped kind with a
synthetic :class:`Rubric` whose ``expected:`` fields are filled with the
judge-authored anchors.

The design attacks the "fuzzy criterion wording" class of judge drift the
M50 A/B (2026-09-21) identified: on that eval, ``voted_rubric`` absorbed
sampling-noise flapping but could not fix ``addressed_to_user`` (κ=0)
because the criterion's description alone under-specifies "met" and the
judge inferred it differently on each sample. Baking one shared anchor
into the rubric for the whole flight makes every subsequent grade score
against the SAME reading of the criterion.

Fail-loud: any warm-up-call failure (non-2xx, truncated JSON, missing
criterion in the anchor map, non-string anchor value) raises
:class:`RuntimeError` with the underlying reason before any wrapped-kind
dispatch runs — matching ``voted_rubric``'s eager-validation stance.
Author-written ``expected:`` anchors are passed through unchanged; only
``expected is None`` graded criteria are auto-anchored.

Cost: one warm-up call per unique (rubric, judge_model). On a 50-trial
flight using one rubric with 5 unanchored graded criteria that is 1 extra
call amortised across 50 grades — effectively free.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge_result import JudgeResult, JudgeUsage
from tolokaforge.core.models.trajectory import Message, MessageRole
from tolokaforge.runner.models import Criterion, Rubric

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.tools.registry import Tool

__all__ = [
    "DEFAULT_WRAPPED_KIND",
    "PerRubricAnchorCache",
    "PerRubricAnchorCacheEntry",
    "PerRubricAnchorGeneratorError",
    "PerRubricAnchorGeneratorTimestamp",
    "AutoAnchoredRubricJudgeKind",
    "clear_anchor_cache",
]

#: Default wrapped kind when ``kind_config`` omits ``wrapped_kind``.
DEFAULT_WRAPPED_KIND = "single_shot_rubric"

#: Accepted ``kind_config`` keys; every other key raises ``ValueError``.
_ACCEPTED_KIND_CONFIG_KEYS = frozenset({"wrapped_kind"})

#: System prompt used for the warm-up call that produces anchors. Kept short
#: and format-strict — the judge must emit a single JSON object mapping each
#: unanchored criterion id to a one-sentence anchor. Free-form prose around
#: the JSON is discarded.
_ANCHOR_SYSTEM_PROMPT = (
    "You are helping calibrate a rubric-based evaluation. For each criterion "
    "listed below, write ONE sentence describing what a fully-satisfying "
    "response would demonstrate for THAT criterion — a specific, observable "
    "anchor that a downstream judge will score against. Do NOT judge any "
    "response, do NOT invent unrelated requirements, and do NOT hedge. "
    "Respond with a single JSON object on one line whose keys are the "
    "criterion ids given below and whose values are the one-sentence "
    "anchors. Emit nothing before or after that JSON object."
)

_AnchorMap = dict[str, str]

_CacheKey = tuple[str, str]

# In-process cache: {(rubric_hash, model_hash): (anchor_map, warmup_usage)}.
# One warm-up call per unique (rubric, judge_model) tuple, amortised across
# every trial in the process. Not persisted to disk; a follow-up may add
# `grade_bundles/`-level persistence if flights show it matters.
_ANCHOR_CACHE: dict[_CacheKey, tuple[_AnchorMap, JudgeUsage]] = {}
_ANCHOR_CACHE_LOCK = threading.Lock()


class PerRubricAnchorGeneratorError(RuntimeError):
    """Warm-up call produced no usable anchor map — fail loud."""


# Vestige-safe aliases for internal callers that need to introspect the cache
# in tests. Exposed via ``__all__`` deliberately.
PerRubricAnchorCache = dict
PerRubricAnchorCacheEntry = tuple
PerRubricAnchorGeneratorTimestamp = float


def clear_anchor_cache() -> None:
    """Reset the in-process warm-up cache. Test-only; safe under a lock."""
    with _ANCHOR_CACHE_LOCK:
        _ANCHOR_CACHE.clear()


class AutoAnchoredRubricJudgeKind:
    """Grade a rubric after filling missing ``expected:`` anchors from a cached judge call."""

    NAME: ClassVar[str] = "auto_anchored_rubric"

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
        wrapped_kind_name = _resolve_kind_config(kind_config)

        unanchored_ids = tuple(
            c.id for c in rubric.criteria if c.kind == "graded" and c.expected is None
        )
        if not unanchored_ids:
            # Nothing to anchor — degenerate directly to the wrapped kind.
            return _dispatch_wrapped(
                wrapped_kind_name=wrapped_kind_name,
                rubric=rubric,
                agent_system_prompt=agent_system_prompt,
                transcript=transcript,
                db_reader=db_reader,
                kb_search=kb_search,
                workspace_dir=workspace_dir,
                extra_read_tools=extra_read_tools,
                state_diff=state_diff,
                judge_model_config=judge_model_config,
                judge_model_provider=judge_model_provider,
                disable_knowledge_search=disable_knowledge_search,
                custom_system_prompt=custom_system_prompt,
                include_agent_system_prompt=include_agent_system_prompt,
                logger=logger,
                warmup_usage=JudgeUsage(),
                anchor_map={},
            )

        anchor_map, warmup_usage = _load_or_generate_anchors(
            rubric=rubric,
            unanchored_ids=unanchored_ids,
            judge_model_config=judge_model_config,
            judge_model_provider=judge_model_provider,
        )

        anchored_rubric = _apply_anchors(rubric, anchor_map)

        return _dispatch_wrapped(
            wrapped_kind_name=wrapped_kind_name,
            rubric=anchored_rubric,
            agent_system_prompt=agent_system_prompt,
            transcript=transcript,
            db_reader=db_reader,
            kb_search=kb_search,
            workspace_dir=workspace_dir,
            extra_read_tools=extra_read_tools,
            state_diff=state_diff,
            judge_model_config=judge_model_config,
            judge_model_provider=judge_model_provider,
            disable_knowledge_search=disable_knowledge_search,
            custom_system_prompt=custom_system_prompt,
            include_agent_system_prompt=include_agent_system_prompt,
            logger=logger,
            warmup_usage=warmup_usage,
            anchor_map=anchor_map,
        )


def _resolve_kind_config(kind_config: Mapping[str, Any] | None) -> str:
    """Validate ``kind_config`` and return ``wrapped_kind``.

    Raises :class:`ValueError` on any unknown key, matching the eager-
    validation stance of the other wrapper kinds (``voted_rubric``,
    ``jury_rubric``).
    """
    if kind_config is None:
        return DEFAULT_WRAPPED_KIND
    unknown = set(kind_config) - _ACCEPTED_KIND_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"auto_anchored_rubric kind_config contains unknown key(s): "
            f"{sorted(unknown)}. Accepted keys: {sorted(_ACCEPTED_KIND_CONFIG_KEYS)}."
        )
    return kind_config.get("wrapped_kind", DEFAULT_WRAPPED_KIND)


def _cache_key(rubric: Rubric, judge_model_config: ModelConfig) -> _CacheKey:
    """Content-hash the rubric + judge model so two runs share cached anchors."""
    rubric_bytes = rubric.model_dump_json().encode("utf-8")
    model_bytes = judge_model_config.model_dump_json().encode("utf-8")
    return (
        hashlib.sha256(rubric_bytes).hexdigest(),
        hashlib.sha256(model_bytes).hexdigest(),
    )


def _load_or_generate_anchors(
    *,
    rubric: Rubric,
    unanchored_ids: tuple[str, ...],
    judge_model_config: ModelConfig,
    judge_model_provider: JudgeModelProvider,
) -> tuple[_AnchorMap, JudgeUsage]:
    """Return the cached anchor map for this (rubric, judge_model) or generate + cache one."""
    key = _cache_key(rubric, judge_model_config)
    with _ANCHOR_CACHE_LOCK:
        cached = _ANCHOR_CACHE.get(key)
    if cached is not None:
        return cached

    anchor_map, warmup_usage = _generate_anchors(
        rubric=rubric,
        unanchored_ids=unanchored_ids,
        judge_model_config=judge_model_config,
        judge_model_provider=judge_model_provider,
    )
    with _ANCHOR_CACHE_LOCK:
        _ANCHOR_CACHE[key] = (anchor_map, warmup_usage)
    return anchor_map, warmup_usage


def _generate_anchors(
    *,
    rubric: Rubric,
    unanchored_ids: tuple[str, ...],
    judge_model_config: ModelConfig,
    judge_model_provider: JudgeModelProvider,
) -> tuple[_AnchorMap, JudgeUsage]:
    """One warm-up judge call → JSON anchor map. Fail loud on any parse error."""
    by_id = {c.id: c for c in rubric.criteria}
    prompt = _anchor_prompt(unanchored_ids, by_id)
    judge_model = judge_model_provider.build(judge_model_config)
    result = judge_model.generate(
        system=_ANCHOR_SYSTEM_PROMPT,
        messages=[Message(role=MessageRole.USER, content=prompt)],
        tools=[],
        tool_choice="none",
    )
    anchor_map = _parse_anchor_response(result.text, unanchored_ids)

    usage = result.usage
    warmup_usage = JudgeUsage(
        calls=1,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        reasoning_tokens=getattr(usage, "reasoning_tokens", 0) or 0,
        cost_usd=result.cost_usd or 0.0,
    )
    return anchor_map, warmup_usage


def _anchor_prompt(unanchored_ids: tuple[str, ...], by_id: dict[str, Criterion]) -> str:
    lines = ["Criteria that need a one-sentence anchor:"]
    for cid in unanchored_ids:
        lines.append(f"  - id: {cid}")
        lines.append(f"    description: {by_id[cid].description}")
    lines.append("")
    lines.append(
        "Respond with a single JSON object mapping each id above to a one-"
        "sentence anchor. Nothing else."
    )
    return "\n".join(lines)


def _parse_anchor_response(text: str, unanchored_ids: tuple[str, ...]) -> _AnchorMap:
    """Extract a ``{id: anchor}`` map from the warm-up call's text. Fail loud on mismatch."""
    payload = text.strip()
    # Some models wrap JSON in code fences; strip a single wrapping pair.
    if payload.startswith("```"):
        parts = payload.split("```")
        if len(parts) >= 2:
            payload = parts[1]
            if payload.startswith("json"):
                payload = payload[len("json") :]
            payload = payload.strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PerRubricAnchorGeneratorError(
            f"auto_anchored_rubric warm-up call returned non-JSON output: {exc}. "
            f"Raw text: {text!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise PerRubricAnchorGeneratorError(
            f"auto_anchored_rubric warm-up call returned a {type(parsed).__name__}; "
            f"expected a JSON object. Raw text: {text!r}"
        )

    missing = [cid for cid in unanchored_ids if cid not in parsed]
    if missing:
        raise PerRubricAnchorGeneratorError(
            f"auto_anchored_rubric warm-up response is missing anchor(s) for "
            f"criterion id(s): {missing}. Received keys: {sorted(parsed)}."
        )

    anchor_map: _AnchorMap = {}
    for cid in unanchored_ids:
        value = parsed[cid]
        if not isinstance(value, str) or not value.strip():
            raise PerRubricAnchorGeneratorError(
                f"auto_anchored_rubric anchor for '{cid}' must be a non-empty "
                f"string; got {type(value).__name__} {value!r}."
            )
        anchor_map[cid] = value.strip()
    return anchor_map


_ANCHOR_PREFIX = "auto-anchor: "


def _apply_anchors(rubric: Rubric, anchor_map: _AnchorMap) -> Rubric:
    """Build a synthetic ``Rubric`` with the auto-anchors filled in.

    Author-written ``expected:`` is untouched. The auto-anchor is prefixed with
    ``auto-anchor: `` so a downstream reader who inspects the rendered rubric
    can tell the anchor came from the harness, not the author.
    """
    if not anchor_map:
        return rubric
    new_criteria: list[Criterion] = []
    for c in rubric.criteria:
        if c.id in anchor_map and c.expected is None:
            new_criteria.append(
                c.model_copy(update={"expected": f"{_ANCHOR_PREFIX}{anchor_map[c.id]}"})
            )
        else:
            new_criteria.append(c)
    return Rubric(criteria=new_criteria, reference=rubric.reference)


def _dispatch_wrapped(
    *,
    wrapped_kind_name: str,
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
    logger: StructuredLogger,
    warmup_usage: JudgeUsage,
    anchor_map: _AnchorMap,
) -> JudgeResult:
    """Delegate to the wrapped kind and fold warm-up usage + anchor audit trail into its result."""
    # Lazy import — plugin_registry imports this package's __init__, which
    # re-exports AutoAnchoredRubricJudgeKind, so a module-level import back
    # into plugin_registry is a live circular import.
    from tolokaforge.core.plugin_registry import load_judge_kind

    wrapped_kind_instance = load_judge_kind(wrapped_kind_name)()
    inner = wrapped_kind_instance.evaluate(
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

    combined_usage = JudgeUsage(
        calls=warmup_usage.calls + inner.usage.calls,
        prompt_tokens=warmup_usage.prompt_tokens + inner.usage.prompt_tokens,
        completion_tokens=warmup_usage.completion_tokens + inner.usage.completion_tokens,
        reasoning_tokens=warmup_usage.reasoning_tokens + inner.usage.reasoning_tokens,
        cost_usd=warmup_usage.cost_usd + inner.usage.cost_usd,
        tool_calls=inner.usage.tool_calls,
        consistency_rejections=inner.usage.consistency_rejections,
    )

    audit_prefix = _render_anchor_audit(anchor_map)
    combined_reasons = f"{audit_prefix}{inner.reasons}" if audit_prefix else inner.reasons

    return dataclasses.replace(inner, usage=combined_usage, reasons=combined_reasons)


def _render_anchor_audit(anchor_map: _AnchorMap) -> str:
    """One-shot audit-trail block listing every auto-anchor the warm-up produced."""
    if not anchor_map:
        return ""
    lines = ["auto_anchored_rubric warm-up anchors:"]
    for cid, anchor in anchor_map.items():
        lines.append(f"  - {cid}: {anchor}")
    lines.append("")
    return "\n".join(lines)
