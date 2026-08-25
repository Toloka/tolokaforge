"""Preset routing — ``z_ai_glm_5_3`` (routing half, PR #1277; effort-rule half, PR #1278).

The entry is declared BEFORE the shared ``openrouter_dict_stringify_recovery``
preset, whose ``z-ai/glm-5*`` glob would otherwise claim the route. That order
is the whole reason ``z-ai/glm-5.3`` gets ``openai_summary_replay`` instead of
the plain ``openai`` codec, and nothing else pins it: a maintainer sorting
``presets:`` alphabetically, or broadening the shared glob, would silently
revert the codec and reintroduce the 0/15 ``UNSIGNED_THINKING_REPLAY`` failure
while every offline suite stayed green. These tests lock four invariants:

1. the measured shapes (bare route + gateway-prefixed) resolve to the overlay;
2. unmeasured point releases / variants do NOT — the globs carry no trailing
   ``*`` (``fnmatch``'s ``*`` also matches ``.`` and ``-``);
3. the 5.1 / 5.2 / 5 siblings stay on the shared preset (a future
   ``glm-5*`` broadening of the overlay would poach them);
4. the overlay is a full copy of the shared recovery stack plus the codec swap
   — ``_match_preset`` does first-match-wins with no fallback merge, so the
   re-declared ``passthrough`` / ``json_coerce`` / ``dict_map_hints`` lines are
   load-bearing, not duplicates;
5. (PR #1278) ``param_value_rules`` DROP ``reasoning_effort`` when ``low`` or
   ``medium`` is asked for, so the route runs at its own default (``max``);
   ``high`` passes through; 5.2 has no such rule. Why: Z.AI's 5.3 reasoning
   API defines ``low`` / ``high`` / ``max`` only (default ``max``) and, under a
   real agentic context, its OpenRouter shim degrades ``medium`` to zero or
   near-zero reasoning tokens instead of rejecting it — the Arena v1 eval ran
   704 trials with 0 reasoning tokens (logistics pass@1 5.4% vs 65.6% at the
   provider default). Replayed 5 tasks x 3 samples: medium 0/15, high 6/15,
   no parameter 13/15 — hence ``drop``, not ``override: high``.
"""

from __future__ import annotations

import pytest
from tolokaforge_models.policies.deepseek import OpenAISummaryReplayReasoningCodec

from tolokaforge.core.llm import (
    DictMapHints,
    JsonCoerceResponse,
    OpenAIContent,
    PassthroughSchema,
    ReasoningConfig,
    build_capabilities,
)
from tolokaforge.core.llm.presets import resolve_effective_preset
from tolokaforge.core.llm.reasoning_codec import OpenAIReasoningCodec

pytestmark = pytest.mark.unit

PRESET = "z_ai_glm_5_3"
FAMILY_PRESET = "openrouter_dict_stringify_recovery"


def _adapt_kwargs(policy, effort: str) -> dict:  # type: ignore[no-untyped-def]
    """The provider kwargs the policy emits for a requested *effort*."""
    kwargs: dict = {}
    policy.adapt(
        kwargs,
        config_temperature=None,
        config_seed=None,
        config_reasoning=ReasoningConfig(mode="adaptive", effort_hint=effort),  # type: ignore[arg-type]
        temperature=None,
        seed=None,
        reasoning=None,
    )
    return kwargs


def _effort_sent(policy, effort: str) -> str | None:  # type: ignore[no-untyped-def]
    """The effort level that reaches the wire for a requested *effort*.

    The OpenRouter provider block routes reasoning through
    ``extra_body.reasoning.effort``; a bare ``GenerationParams`` emits the
    plain ``reasoning_effort`` kwarg. Read whichever the policy produced.
    """
    kwargs = _adapt_kwargs(policy, effort)
    if "reasoning_effort" in kwargs:
        return kwargs["reasoning_effort"]
    return kwargs.get("extra_body", {}).get("reasoning", {}).get("effort")


@pytest.mark.parametrize(
    "model",
    ["z-ai/glm-5.3", "openrouter/z-ai/glm-5.3", "litellm_proxy/z-ai/glm-5.3"],
)
def test_measured_shapes_route_to_the_overlay(model: str) -> None:
    """First-match-wins: the overlay is declared before the family glob."""
    assert resolve_effective_preset(model, "openrouter") == PRESET


@pytest.mark.parametrize(
    "model",
    [
        "z-ai/glm-5.3-turbo",
        "z-ai/glm-5.3-fast",
        "z-ai/glm-5.30",
        "z-ai/glm-5.3.1",
        "z-ai/glm-5.3:exacto",
        "openrouter/z-ai/glm-5.3-fast",
    ],
)
def test_unmeasured_point_releases_are_not_claimed(model: str) -> None:
    """No trailing ``*`` on the overlay globs: a release nobody reprobed must
    fall through to the shared preset, never inherit this codec silently."""
    assert resolve_effective_preset(model, "openrouter") == FAMILY_PRESET


@pytest.mark.parametrize(
    "model",
    ["z-ai/glm-5.1", "z-ai/glm-5.2", "z-ai/glm-5", "z-ai/glm-5-turbo", "z-ai/glm-5v-turbo"],
)
def test_the_rest_of_the_family_stays_on_the_shared_preset(model: str) -> None:
    """The siblings' wire shapes were never measured under the replay codec."""
    assert resolve_effective_preset(model, "openrouter") == FAMILY_PRESET
    assert type(build_capabilities(model, "openrouter").reasoning_codec) is OpenAIReasoningCodec


def test_overlay_is_the_shared_stack_plus_the_codec_swap() -> None:
    """Same adapters as 5.2 on every axis but the reasoning codec."""
    glm53 = build_capabilities("z-ai/glm-5.3", "openrouter")
    glm52 = build_capabilities("z-ai/glm-5.2", "openrouter")
    for caps in (glm53, glm52):
        assert isinstance(caps.schema_sanitizer, PassthroughSchema)
        assert isinstance(caps.response_policy, JsonCoerceResponse)
        assert isinstance(caps.prompt_policy, DictMapHints)
        assert isinstance(caps.content_policy, OpenAIContent)
    assert isinstance(glm53.reasoning_codec, OpenAISummaryReplayReasoningCodec)
    assert type(glm52.reasoning_codec) is OpenAIReasoningCodec


def test_medium_is_dropped_with_evidence() -> None:
    """Asked for ``medium``, the request carries NO reasoning parameter at all
    (extra_body.reasoning absent, no plain kwarg) — the provider default runs."""
    caps = build_capabilities("z-ai/glm-5.3", "openrouter")
    kwargs = _adapt_kwargs(caps.params_policy, "medium")
    assert "reasoning_effort" not in kwargs
    assert "reasoning" not in kwargs.get("extra_body", {})
    assert caps.params_policy.rule_for("reasoning_effort", "medium") is not None
    evidence = caps.params_policy.rule_evidence("reasoning_effort", "medium")
    assert "2026-08-24" in evidence and "z-ai/glm-5.3" in evidence and "13/15" in evidence


def test_low_is_dropped_too() -> None:
    """``low`` is documented by Z.AI yet degrades like ``medium`` under a heavy
    context (0 reasoning tokens, 2026-08-24/25), so it gets the same rule."""
    caps = build_capabilities("z-ai/glm-5.3", "openrouter")
    kwargs = _adapt_kwargs(caps.params_policy, "low")
    assert "reasoning_effort" not in kwargs
    assert "reasoning" not in kwargs.get("extra_body", {})


def test_high_passes_through_untouched() -> None:
    """The one level the route honours (126–197 reasoning tokens) is sent as asked."""
    caps = build_capabilities("z-ai/glm-5.3", "openrouter")
    assert _effort_sent(caps.params_policy, "high") == "high"


def test_glm52_has_no_effort_rule() -> None:
    """The sibling honours ``medium`` live; it must not inherit the override."""
    caps = build_capabilities("z-ai/glm-5.2", "openrouter")
    assert caps.params_policy.rule_for("reasoning_effort", "medium") is None
    assert _effort_sent(caps.params_policy, "medium") == "medium"
