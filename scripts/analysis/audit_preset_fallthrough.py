#!/usr/bin/env python3
"""Audit which models fall through to a preset that carries no per-model budgets.

Read-only. Touches no preset data.

``tolokaforge.core.llm.presets`` routes a model slug to exactly one preset
entry: the FIRST ``presets:`` block whose ``match`` / ``match_provider`` globs
match, whole-entry, no field-level merge across entries. A slug that lands on a
broad multi-vendor preset therefore inherits that preset's policy axes AND its
silence on every budget knob, even when a near-identical sibling slug routes to
a narrower preset that declares four of them. Nothing in the engine warns about
the asymmetry — both resolutions are "successful".

This script makes that asymmetry visible before a sweep pays for it. For every
audited slug it prints the resolved preset name and which budget knobs that
preset declares, then reports two structural hazards:

``SUSPECT``
    The slug resolves to a preset whose matched slugs span more than one
    vendor, while a slug in the same family resolves to a different preset
    that declares budget knobs this one does not. The missing knobs are the
    sibling's declared set minus this slug's.

``CEILING``
    A preset's ``max_context_tokens + context_watermark`` disagrees with the
    SMALLEST OpenRouter context window among the slugs its globs match — above
    it (overshoot: the pre-turn summarize check never arms on the small-window
    slug, so the trial terminates on ``ContextWindowExceededError`` instead of
    handing off) or far below it (undershoot: the handoff fires on prompts the
    provider would still have accepted).

Both are detected from the shape of the preset table and the slug strings, not
from a list of known-bad models.

Usage:
  uv run python scripts/analysis/audit_preset_fallthrough.py
  uv run python scripts/analysis/audit_preset_fallthrough.py moonshotai/kimi-k2.6 moonshotai/kimi-k3
  uv run python scripts/analysis/audit_preset_fallthrough.py --models-file slugs.txt --json
  uv run python scripts/analysis/audit_preset_fallthrough.py --offline
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tolokaforge.core.llm.presets import (
    build_capabilities,
    get_resolved_presets,
    resolve_effective_preset,
)

#: Budget / recovery knobs on :class:`~tolokaforge.core.llm.capabilities.ModelCapabilities`.
#: Each maps to a two-letter column code in the compact table.
KNOBS: dict[str, str] = {
    "default_max_turns": "MT",
    "default_agent_prompt_contract": "PC",
    "empty_retry_count": "ER",
    "max_context_tokens": "CT",
    "context_watermark": "CW",
    "tool_output_max_chars": "TO",
    "message_assembly_policy": "MA",
    "parser_error_retry_count": "PR",
    "output_length_retry_count": "OL",
}

#: Why each knob's absence costs a trial. Quoted in the SUSPECT detail block.
KNOB_IMPACT: dict[str, str] = {
    "default_max_turns": (
        "turn budget falls back to the engine-wide DEFAULT_MAX_TURNS for tasks "
        "that declare none, so a granular-edit model runs out of turns mid-task"
    ),
    "default_agent_prompt_contract": (
        "a solo agent gets no reply contract, so a model that does not narrate "
        "unprompted loses its running picture of the task between turns"
    ),
    "empty_retry_count": (
        "a provider-side empty completion terminates the trial on the first "
        "occurrence instead of resampling"
    ),
    "max_context_tokens": (
        "the pre-turn summarize check and the reactive summarize are both "
        "disabled, so a long trajectory terminates on CONTEXT_WINDOW_EXCEEDED"
    ),
    "context_watermark": (
        "the pre-turn summarize check never arms, so the handoff only ever "
        "fires reactively (if at all)"
    ),
    "tool_output_max_chars": (
        "unbounded tool output accumulates verbatim on every later turn and "
        "pushes the prompt into the context / reasoning-budget exhaustion zone"
    ),
    "message_assembly_policy": (
        "an empty assistant ``content`` alongside ``tool_calls`` is re-sent "
        "verbatim, which strict provider APIs reject with HTTP 400"
    ),
    "parser_error_retry_count": (
        "an undecodable tool-call arguments string is accepted as {} instead "
        "of being resampled, so the turn is spent on a no-op call"
    ),
    "output_length_retry_count": (
        "a max-tokens truncation is accepted as the assistant turn instead of "
        "being resampled with a split-it-up nudge"
    ),
}

#: Slugs the audit always covers, on top of every concrete slug harvested from
#: the preset match globs. These are the sweep lineup; several are deliberately
#: NOT named by any glob of their own and exist here to prove where they land.
DEFAULT_EXTRA_SLUGS: tuple[str, ...] = (
    "openai/gpt-5.6-sol",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-5",
    "moonshotai/kimi-k2.7-code",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k3",
    "google/gemini-3.1-pro-preview",
    "x-ai/grok-4",
    "deepseek/deepseek-v4",
    "z-ai/glm-5.1",
    "qwen/qwen3-coder",
)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_MAX_AGE_S = 24 * 60 * 60
_GLOB_METACHARS = "*?["


# ---------------------------------------------------------------------------
# Slug harvesting and family grouping
# ---------------------------------------------------------------------------


def concrete_slug(pattern: str) -> str | None:
    """Return the concrete slug a match glob names, or ``None`` if it is a wildcard.

    A glob is concrete when removing at most one TRAILING ``*`` leaves a string
    with no remaining glob metacharacter, a vendor separator, and a non-empty
    model part that does not end on a separator — ``"moonshotai/kimi-k3*"`` →
    ``"moonshotai/kimi-k3"``. Patterns like ``"*kimi-k2*"``, ``"qwen/*"``,
    ``"google/gemini-*"`` and ``"grok*"`` name no single slug and are skipped.
    """
    candidate = pattern[:-1] if pattern.endswith("*") else pattern
    if any(ch in candidate for ch in _GLOB_METACHARS):
        return None
    if "/" not in candidate:
        return None
    if candidate.endswith(("/", "-", ".", "_")):
        return None
    if not candidate.rsplit("/", 1)[-1]:
        return None
    return candidate


def harvest_slugs(presets: dict[str, Any]) -> list[str]:
    """Every concrete slug named by any ``match`` glob in the preset table."""
    found: list[str] = []
    for preset in (presets.get("presets") or {}).values():
        for pattern in preset.get("match") or []:
            slug = concrete_slug(pattern)
            if slug is not None and slug not in found:
                found.append(slug)
    return found


def vendor_of(slug: str) -> str:
    """Vendor namespace — the segment before the LAST ``/``.

    Gateway-prefixed slugs (``openrouter/google/gemini-3.5-flash``) keep their
    real vendor this way rather than collapsing onto the gateway name.
    """
    return slug.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in slug else ""


def family_of(slug: str) -> str:
    """Vendor plus model-line stem, with the version token and everything after it dropped.

    The stem is the leading run of ``-``-separated tokens that carry no digit,
    which is what makes ``kimi-k2.7-code`` and ``kimi-k3`` one family
    (``moonshotai/kimi``) without naming either. A slug whose first token
    already carries a digit keeps that token, so ``openai/gpt-5.6-sol`` is
    ``openai/gpt`` and ``minimax/minimax-m3`` is ``minimax/minimax``.
    """
    vendor = vendor_of(slug)
    model = slug.rsplit("/", 1)[-1]
    tokens = model.split("-")
    stem = _takewhile_no_digit(tokens) or [tokens[0]]
    return f"{vendor}/{'-'.join(stem)}"


def normalized_vendor(vendor: str) -> str:
    """Vendor key with separators dropped, so ``x-ai`` and ``xai`` are one vendor.

    A preset that lists both spellings of one vendor's namespace is not shared
    across vendors, and must not be reported as though it were.
    """
    return "".join(ch for ch in vendor.lower() if ch.isalnum())


def glob_vendor(pattern: str) -> str | None:
    """The vendor namespace a match glob names, or ``None`` if it names none.

    Leading wildcard segments are stripped first, so ``"*/z-ai/glm-5.3"`` and
    ``"*amazon/nova*"`` still name a vendor, while ``"*kimi-k2*"`` and
    ``"*claude*"`` — which any vendor's slug could satisfy — name none.
    """
    stripped = pattern.lstrip("*/")
    if "/" not in stripped:
        return None
    vendor = stripped.rsplit("/", 1)[0].rsplit("/", 1)[-1]
    if not vendor or any(ch in vendor for ch in _GLOB_METACHARS):
        return None
    return vendor


def preset_vendor_map(presets: dict[str, Any]) -> dict[str, set[str]]:
    """``{preset_name: {normalized vendor, …}}`` from each preset's own globs.

    Sharedness is a property of the preset, not of whichever slugs happen to be
    in one audit run: a two-slug run over one vendor's models must still see
    that the preset it lands on answers to six vendors.
    """
    out: dict[str, set[str]] = {}
    for name, block in (presets.get("presets") or {}).items():
        vendors = {
            normalized_vendor(v)
            for pattern in (block.get("match") or [])
            if (v := glob_vendor(pattern)) is not None
        }
        out[name] = vendors
    return out


def _takewhile_no_digit(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for token in tokens:
        if any(ch.isdigit() for ch in token):
            break
        out.append(token)
    return out


# ---------------------------------------------------------------------------
# Glob matching — mirrors presets._iter_preset_matches
# ---------------------------------------------------------------------------


def preset_matches(preset: dict[str, Any], slug: str, provider: str) -> bool:
    """Whether *preset*'s globs match ``(slug, provider)``.

    Mirrors the matcher inside :func:`tolokaforge.core.llm.presets._iter_preset_matches`
    so the audit can ask "which slugs does this preset's globs cover?" — a
    question the engine's own first-match-wins accessors cannot answer, because
    they stop at the winner. ``test_matcher_agrees_with_engine_resolution``
    pins this mirror against :func:`resolve_effective_preset`.
    """
    name_lower = slug.lower()
    provider_lower = (provider or "").lower()
    patterns = preset.get("match") or []
    if any(fnmatch.fnmatch(name_lower, p) for p in patterns):
        return True
    provider_patterns = preset.get("match_provider") or []
    return bool(provider_patterns) and any(
        fnmatch.fnmatch(provider_lower, p) for p in provider_patterns
    )


# ---------------------------------------------------------------------------
# OpenRouter context windows
# ---------------------------------------------------------------------------


def fetch_openrouter_models(timeout: float = 30.0) -> list[dict[str, Any]]:
    """Raw model objects from OpenRouter's public model list."""
    request = urllib.request.Request(  # noqa: S310 (fixed https URL)
        OPENROUTER_MODELS_URL, headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("data") or []


def load_openrouter_models(
    cache_path: Path,
    *,
    offline: bool = False,
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Return ``(models, source)`` — cached, freshly fetched, or empty.

    Degrades to ``([], "unavailable: …")`` rather than raising: the preset-shape
    half of the audit is the load-bearing half and must still run offline. A
    cache older than :data:`CACHE_MAX_AGE_S` is refreshed when the network is
    allowed, and kept (stale) when it is not.
    """
    cached: list[dict[str, Any]] | None = None
    cache_age: float | None = None
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())["data"]
            cache_age = time.time() - cache_path.stat().st_mtime
        except (OSError, ValueError, KeyError, TypeError):
            cached = None

    fresh_enough = cached is not None and cache_age is not None and cache_age < CACHE_MAX_AGE_S
    if cached is not None and fresh_enough and not refresh:
        return cached, f"cache {cache_path} ({int((cache_age or 0) / 60)} min old)"
    if offline:
        if cached is not None:
            return cached, f"cache {cache_path} (stale, --offline)"
        return [], "unavailable: --offline and no cache"

    try:
        models = fetch_openrouter_models()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if cached is not None:
            return cached, f"cache {cache_path} (stale; fetch failed: {exc})"
        return [], f"unavailable: fetch failed: {exc}"

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"data": models}))
    except OSError:
        pass
    return models, f"live fetch, cached to {cache_path}"


def context_windows(models: list[dict[str, Any]]) -> dict[str, int]:
    """``{openrouter_model_id: context_length}`` for every model that declares one."""
    out: dict[str, int] = {}
    for model in models:
        model_id = model.get("id")
        length = model.get("context_length")
        if isinstance(model_id, str) and isinstance(length, int) and length > 0:
            out[model_id] = length
    return out


@dataclass(frozen=True)
class WindowLookup:
    """Resolved OpenRouter context window for one slug."""

    tokens: int | None
    exact: bool
    matched_ids: tuple[str, ...] = ()

    def render(self) -> str:
        if self.tokens is None:
            return "-"
        return f"{self.tokens:,}" if self.exact else f"~{self.tokens:,}"


def lookup_window(slug: str, windows: dict[str, int]) -> WindowLookup:
    """Context window for *slug*, falling back to the smallest prefix-match.

    An exact id wins. Otherwise every OpenRouter id that extends the slug at a
    separator is a candidate (a slug harvested from a prefix glob such as
    ``nvidia/nemotron`` names a line, not a model) and the SMALLEST window among
    them is taken — the ceiling check must not be talked out of a finding by an
    optimistic sibling.
    """
    normalized = slug.split("/", 1)[1] if slug.startswith("openrouter/") else slug
    if normalized in windows:
        return WindowLookup(windows[normalized], exact=True, matched_ids=(normalized,))
    prefixed = {
        model_id: length
        for model_id, length in windows.items()
        if model_id.startswith(normalized) and model_id[len(normalized) :][:1] in ("-", ".", ":")
    }
    if not prefixed:
        return WindowLookup(None, exact=False)
    smallest = min(prefixed, key=lambda k: prefixed[k])
    return WindowLookup(prefixed[smallest], exact=False, matched_ids=tuple(sorted(prefixed)))


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@dataclass
class ModelRow:
    slug: str
    preset: str
    family: str
    vendor: str
    declared: dict[str, Any]
    """Knob name → the value the winning preset block declares."""
    effective: dict[str, Any]
    """Knob name → the value the built capabilities actually carry."""
    window: WindowLookup

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "preset": self.preset,
            "family": self.family,
            "vendor": self.vendor,
            "knobs_set": {k: _jsonable(v) for k, v in self.declared.items()},
            "knobs_unset": [k for k in KNOBS if k not in self.declared],
            "effective": {k: _jsonable(v) for k, v in self.effective.items()},
            "openrouter_context_window": self.window.tokens,
            "openrouter_window_exact": self.window.exact,
        }


@dataclass
class Suspect:
    slug: str
    preset: str
    family: str
    shared_with_vendors: tuple[str, ...]
    sibling_slug: str
    sibling_preset: str
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "preset": self.preset,
            "family": self.family,
            "preset_shared_with_vendors": list(self.shared_with_vendors),
            "sibling_slug": self.sibling_slug,
            "sibling_preset": self.sibling_preset,
            "missing_knobs": list(self.missing),
        }


@dataclass
class Drift:
    """Same-family knob divergence on a preset that is NOT shared across vendors.

    Weaker than a :class:`Suspect` — nobody else's model dragged the preset's
    shape around — but it is still one sibling carrying a budget another does
    not, which is the same failure mode at a smaller blast radius.
    """

    slug: str
    preset: str
    family: str
    sibling_slug: str
    sibling_preset: str
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "preset": self.preset,
            "family": self.family,
            "sibling_slug": self.sibling_slug,
            "sibling_preset": self.sibling_preset,
            "missing_knobs": list(self.missing),
        }


#: A ceiling below this fraction of the smallest real window is reported as an
#: UNDERSHOOT: the summarize handoff fires on a prompt the provider would still
#: have accepted, so the trial pays a compression round-trip and loses history
#: for nothing. Half the real window is the point where that stops being
#: ordinary headroom and starts being a stale number.
UNDERSHOOT_RATIO = 0.5


@dataclass
class CeilingFinding:
    preset: str
    kind: str
    """``"overshoot"`` (hazard) or ``"undershoot"`` (stale ceiling)."""
    ceiling: int
    max_context_tokens: int
    context_watermark: int
    smallest_slug: str
    smallest_window: int
    covered_slugs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "kind": self.kind,
            "ceiling": self.ceiling,
            "max_context_tokens": self.max_context_tokens,
            "context_watermark": self.context_watermark,
            "smallest_slug": self.smallest_slug,
            "smallest_window": self.smallest_window,
            "covered_slugs": list(self.covered_slugs),
        }


@dataclass
class AuditReport:
    rows: list[ModelRow] = field(default_factory=list)
    suspects: list[Suspect] = field(default_factory=list)
    drifts: list[Drift] = field(default_factory=list)
    ceilings: list[CeilingFinding] = field(default_factory=list)
    window_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "openrouter_window_source": self.window_source,
            "models": [r.as_dict() for r in self.rows],
            "suspects": [s.as_dict() for s in self.suspects],
            "family_knob_drift": [d.as_dict() for d in self.drifts],
            "context_ceilings": [c.as_dict() for c in self.ceilings],
        }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return type(value).__name__


def declared_knobs(preset_block: dict[str, Any]) -> dict[str, Any]:
    """Knobs the preset block names itself — not what it inherits."""
    return {knob: preset_block[knob] for knob in KNOBS if knob in preset_block}


def effective_knobs(slug: str, provider: str) -> dict[str, Any]:
    """Knob values on the capabilities the engine would actually build."""
    caps = build_capabilities(slug, provider)
    out: dict[str, Any] = {}
    for knob in KNOBS:
        value = getattr(caps, knob)
        out[knob] = type(value).__name__ if knob == "message_assembly_policy" else value
    return out


def build_rows(slugs: list[str], provider: str, windows: dict[str, int]) -> list[ModelRow]:
    presets = get_resolved_presets()
    blocks = presets.get("presets") or {}
    rows: list[ModelRow] = []
    for slug in slugs:
        preset = resolve_effective_preset(slug, provider)
        block = blocks.get(preset) or {}
        rows.append(
            ModelRow(
                slug=slug,
                preset=preset,
                family=family_of(slug),
                vendor=vendor_of(slug),
                declared=declared_knobs(block),
                effective=effective_knobs(slug, provider),
                window=lookup_window(slug, windows),
            )
        )
    return rows


def find_suspects(
    rows: list[ModelRow],
    preset_vendors: dict[str, set[str]] | None = None,
) -> tuple[list[Suspect], list[Drift]]:
    """Split same-family knob asymmetries into cross-vendor SUSPECTs and same-vendor drift.

    A model is SUSPECT when both hold:

    1. its resolved preset is shared — its match globs, plus the audited slugs
       landing on it, span more than one vendor, so the preset's shape answers
       to no single model;
    2. a slug in the same family resolves to a DIFFERENT preset that declares
       budget knobs this model's preset does not.

    Condition 1 alone is normal (a wire-shape preset is meant to be shared);
    condition 2 alone is same-vendor drift, reported separately. Together they
    are the shape that let a model inherit a generic preset's silence while its
    near-twin carried four protective knobs.
    """
    vendors_per_preset: dict[str, set[str]] = defaultdict(set)
    for preset, vendors in (preset_vendors or {}).items():
        vendors_per_preset[preset] |= vendors
    for row in rows:
        vendors_per_preset[row.preset].add(normalized_vendor(row.vendor))

    by_family: dict[str, list[ModelRow]] = defaultdict(list)
    for row in rows:
        by_family[row.family].append(row)

    suspects: list[Suspect] = []
    drifts: list[Drift] = []
    for family, members in sorted(by_family.items()):
        for row in members:
            own = set(row.declared)
            best: tuple[int, ModelRow, frozenset[str]] | None = None
            for sibling in members:
                if sibling.preset == row.preset:
                    continue
                missing = frozenset(sibling.declared) - own
                if missing and (best is None or len(missing) > best[0]):
                    best = (len(missing), sibling, missing)
            if best is None:
                continue
            _, sibling, missing_set = best
            missing = tuple(k for k in KNOBS if k in missing_set)
            shared = tuple(sorted(vendors_per_preset[row.preset]))
            if len(shared) > 1:
                suspects.append(
                    Suspect(
                        slug=row.slug,
                        preset=row.preset,
                        family=family,
                        shared_with_vendors=shared,
                        sibling_slug=sibling.slug,
                        sibling_preset=sibling.preset,
                        missing=missing,
                    )
                )
            else:
                drifts.append(
                    Drift(
                        slug=row.slug,
                        preset=row.preset,
                        family=family,
                        sibling_slug=sibling.slug,
                        sibling_preset=sibling.preset,
                        missing=missing,
                    )
                )
    suspects.sort(key=lambda s: (-len(s.missing), s.slug))
    drifts.sort(key=lambda d: (-len(d.missing), d.slug))
    return suspects, drifts


def find_ceilings(slugs: list[str], provider: str, windows: dict[str, int]) -> list[CeilingFinding]:
    """Presets whose declared context ceiling disagrees with their covered windows.

    Scope is every slug a preset's globs MATCH, not only the slugs it wins:
    the ceiling is a property of the glob set, and a slug an earlier preset
    currently claims becomes exposed the moment that earlier entry is narrowed
    or removed.

    Two disagreements are reported against the SMALLEST real window among those
    slugs. ``overshoot`` — the ceiling sits above it, so the summarize check
    never arms on the small-window slug and the trial terminates on
    ``CONTEXT_WINDOW_EXCEEDED``. ``undershoot`` — the ceiling sits below
    :data:`UNDERSHOOT_RATIO` of it, so the handoff fires on prompts the
    provider would still have accepted.
    """
    findings: list[CeilingFinding] = []
    blocks = (get_resolved_presets().get("presets") or {}).items()
    for name, block in blocks:
        max_context = block.get("max_context_tokens")
        watermark = block.get("context_watermark")
        if not isinstance(max_context, int) or not isinstance(watermark, int):
            continue
        ceiling = max_context + watermark
        covered: list[tuple[str, int]] = []
        for slug in slugs:
            if not preset_matches(block, slug, provider):
                continue
            window = lookup_window(slug, windows)
            if window.tokens is not None:
                covered.append((slug, window.tokens))
        if not covered:
            continue
        smallest_slug, smallest_window = min(covered, key=lambda pair: pair[1])
        if ceiling > smallest_window:
            kind = "overshoot"
        elif ceiling < smallest_window * UNDERSHOOT_RATIO:
            kind = "undershoot"
        else:
            continue
        findings.append(
            CeilingFinding(
                preset=name,
                kind=kind,
                ceiling=ceiling,
                max_context_tokens=max_context,
                context_watermark=watermark,
                smallest_slug=smallest_slug,
                smallest_window=smallest_window,
                covered_slugs=tuple(slug for slug, _ in covered),
            )
        )
    findings.sort(key=lambda f: (f.kind != "overshoot", f.smallest_window - f.ceiling))
    return findings


def overshoots(findings: list[CeilingFinding]) -> list[CeilingFinding]:
    """The subset that is a hazard rather than a stale number."""
    return [f for f in findings if f.kind == "overshoot"]


def run_audit(
    slugs: list[str], provider: str, windows: dict[str, int], window_source: str
) -> AuditReport:
    rows = build_rows(slugs, provider, windows)
    suspects, drifts = find_suspects(rows, preset_vendor_map(get_resolved_presets()))
    return AuditReport(
        rows=rows,
        suspects=suspects,
        drifts=drifts,
        ceilings=find_ceilings(slugs, provider, windows),
        window_source=window_source,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(report: AuditReport, provider: str) -> str:
    lines: list[str] = []
    suspect_slugs = {s.slug for s in report.suspects}

    lines.append("=" * 118)
    lines.append(f"PRESET FALL-THROUGH AUDIT — provider={provider!r}, {len(report.rows)} models")
    lines.append(f"OpenRouter context windows: {report.window_source}")
    lines.append("=" * 118)
    lines.append("")
    lines.append("Knob columns: " + "  ".join(f"{code}={knob}" for knob, code in KNOBS.items()))
    lines.append("'x' = declared by the resolved preset, '.' = unset (engine default applies)")
    lines.append("")

    slug_w = max((len(r.slug) for r in report.rows), default=20) + 2
    preset_w = max((len(r.preset) for r in report.rows), default=20) + 2
    header = (
        f"{'MODEL':<{slug_w}}{'RESOLVED PRESET':<{preset_w}}"
        + "".join(f"{code:>4}" for code in KNOBS.values())
        + f"{'OR CTX':>12}  "
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in sorted(report.rows, key=lambda r: (r.family, r.slug)):
        marks = "".join(f"{'x' if knob in row.declared else '.':>4}" for knob in KNOBS)
        flag = "  SUSPECT" if row.slug in suspect_slugs else ""
        lines.append(
            f"{row.slug:<{slug_w}}{row.preset:<{preset_w}}{marks}{row.window.render():>12}  {flag}"
        )
    lines.append("")

    lines.append("=" * 118)
    lines.append(
        f"SUSPECT — shared multi-vendor preset, sibling carries more ({len(report.suspects)})"
    )
    lines.append("=" * 118)
    if not report.suspects:
        lines.append("  none")
    for rank, suspect in enumerate(report.suspects, start=1):
        lines.append("")
        lines.append(f"{rank}. {suspect.slug}  →  {suspect.preset}")
        lines.append(f"   preset shared across vendors: {', '.join(suspect.shared_with_vendors)}")
        lines.append(
            f"   sibling {suspect.sibling_slug}  →  {suspect.sibling_preset} "
            f"(same family {suspect.family})"
        )
        lines.append(f"   missing {len(suspect.missing)} knob(s):")
        for knob in suspect.missing:
            lines.append(f"     - {knob}: {KNOB_IMPACT[knob]}")
    lines.append("")

    lines.append("=" * 118)
    lines.append(f"FAMILY KNOB DRIFT — same vendor, sibling carries more ({len(report.drifts)})")
    lines.append("=" * 118)
    if not report.drifts:
        lines.append("  none")
    for drift in report.drifts:
        lines.append(
            f"  {drift.slug} → {drift.preset} | sibling {drift.sibling_slug} → "
            f"{drift.sibling_preset} | missing: {', '.join(drift.missing)}"
        )
    lines.append("")

    over = overshoots(report.ceilings)
    lines.append("=" * 118)
    lines.append(
        f"CONTEXT CEILING vs REAL WINDOW — {len(over)} overshoot, "
        f"{len(report.ceilings) - len(over)} undershoot"
    )
    lines.append("=" * 118)
    if not report.ceilings:
        lines.append("  none")
    for finding in report.ceilings:
        lines.append("")
        lines.append(
            f"  [{finding.kind.upper()}] {finding.preset}: max_context_tokens "
            f"{finding.max_context_tokens:,} + context_watermark "
            f"{finding.context_watermark:,} = {finding.ceiling:,}"
        )
        lines.append(
            f"    smallest window among matched slugs: {finding.smallest_window:,} "
            f"({finding.smallest_slug})"
        )
        lines.append(f"    matched slugs: {', '.join(finding.covered_slugs)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def read_models_file(path: Path) -> list[str]:
    slugs: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            slugs.append(line)
    return slugs


def default_slugs() -> list[str]:
    harvested = harvest_slugs(get_resolved_presets())
    out = list(harvested)
    for slug in DEFAULT_EXTRA_SLUGS:
        if slug not in out:
            out.append(slug)
    return out


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit_preset_fallthrough",
        description=(
            "Read-only audit of model → preset resolution: which models fall "
            "through to a shared preset that carries no per-model budgets."
        ),
    )
    parser.add_argument("slugs", nargs="*", help="Model slugs to audit (default: see --help)")
    parser.add_argument(
        "--models-file", type=Path, help="File with one slug per line ('#' comments)"
    )
    parser.add_argument(
        "--provider",
        default="openrouter",
        help="Provider passed to preset resolution (default: openrouter)",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    parser.add_argument("--offline", action="store_true", help="Never fetch; use cache or degrade")
    parser.add_argument("--refresh", action="store_true", help="Force a fresh OpenRouter fetch")
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="OpenRouter response cache (default: <repo>/.cache/openrouter_models.json)",
    )
    parser.add_argument(
        "--fail-on-suspect",
        action="store_true",
        help="Exit 1 on any SUSPECT or ceiling OVERSHOOT (undershoots do not fail)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    slugs: list[str] = list(args.slugs)
    if args.models_file:
        slugs.extend(s for s in read_models_file(args.models_file) if s not in slugs)
    if not slugs:
        slugs = default_slugs()

    cache_path = args.cache or (repo_root() / ".cache" / "openrouter_models.json")
    models, source = load_openrouter_models(cache_path, offline=args.offline, refresh=args.refresh)
    report = run_audit(slugs, args.provider, context_windows(models), source)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render(report, args.provider))

    if args.fail_on_suspect and (report.suspects or overshoots(report.ceilings)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
