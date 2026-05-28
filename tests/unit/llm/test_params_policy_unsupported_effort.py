"""``unsupported_effort_levels`` — fail-loud when a preset declares a
specific effort level as known-broken upstream.

The 2026-05-21 investigation isolated a litellm direct-``gemini/*``
provider bug: ``reasoning_effort='medium'`` combined with tool_calls
produces empty responses for Gemini 3.1 Pro
(BerriAI/litellm#19403-class). Rather than silently mapping ``medium``
to ``high`` (which would hide the upstream bug from callers who set
``medium`` deliberately), AGENTS.md rule #1 dictates surfacing the
failure explicitly. The caller decides whether to retry with ``low`` /
``high``, switch to the OpenRouter transport, or wait for an upstream
fix.

The ``gemini`` provider overlay in ``model_presets.yaml`` is the only
production declaration of ``unsupported_effort_levels`` today. Direct
``gemini/*`` callers see the error; the OpenRouter route is unaffected
because its provider overlay flips ``reasoning_via_extra_body=True`` and
sends effort under ``extra_body.reasoning.effort``, which OpenRouter
translates correctly upstream.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.params_policy import GenerationParams
from tolokaforge.core.llm.reasoning import ReasoningConfig

pytestmark = pytest.mark.unit


def _adapt(caps, reasoning: ReasoningConfig) -> dict:
    return caps.params_policy.adapt(
        kwargs={"model": "m", "messages": []},
        config_temperature=0.6,
        config_seed=42,
        config_reasoning=ReasoningConfig(mode="off"),
        temperature=0.6,
        seed=42,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Unit: GenerationParams in isolation
# ---------------------------------------------------------------------------


def test_unsupported_effort_raises_clear_value_error() -> None:
    """The error message identifies the bad level, the supported set, and
    points the caller at where the declaration lives."""
    params = GenerationParams(
        reasoning_via_extra_body=False,
        unsupported_effort_levels=("medium",),
    )
    with pytest.raises(ValueError, match=r"medium.*unsupported") as exc_info:
        params.adapt(
            kwargs={"model": "m", "messages": []},
            config_temperature=None,
            config_seed=None,
            config_reasoning=ReasoningConfig(mode="off"),
            temperature=None,
            seed=None,
            reasoning=ReasoningConfig(mode="adaptive", effort_hint="medium"),
        )
    msg = str(exc_info.value)
    # The error must name the supported alternatives so callers don't have
    # to guess.
    assert "'low'" in msg and "'high'" in msg
    # And point at where the declaration lives.
    assert "model_presets.yaml" in msg


def test_supported_effort_levels_still_emit_normally() -> None:
    """Low and high pass through to ``reasoning_effort`` as before; the
    guard only fires on the declared-unsupported level."""
    params = GenerationParams(
        reasoning_via_extra_body=False,
        unsupported_effort_levels=("medium",),
    )
    for ok_effort in ("low", "high"):
        kwargs = params.adapt(
            kwargs={"model": "m", "messages": []},
            config_temperature=None,
            config_seed=None,
            config_reasoning=ReasoningConfig(mode="off"),
            temperature=None,
            seed=None,
            reasoning=ReasoningConfig(mode="adaptive", effort_hint=ok_effort),
        )
        assert kwargs.get("reasoning_effort") == ok_effort, (
            f"effort={ok_effort} should emit reasoning_effort={ok_effort}, " f"got {kwargs!r}"
        )


def test_empty_unsupported_set_is_default_behaviour() -> None:
    """When no unsupported levels are declared, ``medium`` works fine."""
    params = GenerationParams(
        reasoning_via_extra_body=False,
        unsupported_effort_levels=None,
    )
    kwargs = params.adapt(
        kwargs={"model": "m", "messages": []},
        config_temperature=None,
        config_seed=None,
        config_reasoning=ReasoningConfig(mode="off"),
        temperature=None,
        seed=None,
        reasoning=ReasoningConfig(mode="adaptive", effort_hint="medium"),
    )
    assert kwargs.get("reasoning_effort") == "medium"


def test_unsupported_check_is_case_insensitive() -> None:
    """Effort hint is lower-cased before membership check, matching the
    rest of the effort-handling code."""
    params = GenerationParams(
        reasoning_via_extra_body=False,
        unsupported_effort_levels=("MEDIUM",),  # declared upper, queried lower
    )
    with pytest.raises(ValueError, match=r"medium"):
        params.adapt(
            kwargs={"model": "m", "messages": []},
            config_temperature=None,
            config_seed=None,
            config_reasoning=ReasoningConfig(mode="off"),
            temperature=None,
            seed=None,
            reasoning=ReasoningConfig(mode="adaptive", effort_hint="medium"),
        )


# ---------------------------------------------------------------------------
# Integration: provider overlay in model_presets.yaml flows through
# ---------------------------------------------------------------------------


def test_direct_gemini_with_medium_effort_raises_per_provider_overlay() -> None:
    """The ``gemini`` provider overlay in ``model_presets.yaml`` declares
    ``medium`` unsupported. Building a capabilities for ``provider=gemini``
    must inherit that and raise loud when the caller sends medium."""
    caps = build_capabilities("gemini-3.1-pro-preview", provider="gemini")
    with pytest.raises(ValueError, match=r"medium.*unsupported"):
        _adapt(caps, ReasoningConfig(mode="adaptive", effort_hint="medium"))


def test_direct_gemini_with_high_effort_still_works() -> None:
    """The same preset+overlay allows ``high`` and ``low`` cleanly —
    only medium is gated."""
    caps = build_capabilities("gemini-3.1-pro-preview", provider="gemini")
    out = _adapt(caps, ReasoningConfig(mode="adaptive", effort_hint="high"))
    assert out.get("reasoning_effort") == "high"


def test_openrouter_gemini_route_is_unaffected_by_direct_overlay() -> None:
    """The bug is on the direct litellm ``gemini/*`` path; OpenRouter sends
    effort via ``extra_body.reasoning.effort`` which OpenRouter translates
    correctly upstream. The ``unsupported_effort_levels`` overlay must NOT
    apply when the route is OpenRouter."""
    caps = build_capabilities("google/gemini-3.1-pro-preview", provider="openrouter")
    # Should not raise:
    out = _adapt(caps, ReasoningConfig(mode="adaptive", effort_hint="medium"))
    # And the routing must hit the OpenRouter extra-body path:
    assert out.get("extra_body", {}).get("reasoning") == {
        "effort": "medium",
        "enabled": True,
    }


def test_non_gemini_direct_routes_unaffected() -> None:
    """The overlay is keyed on ``provider == 'gemini'``; other direct
    providers don't inherit it."""
    caps = build_capabilities("gpt-4o", provider="openai")
    out = _adapt(caps, ReasoningConfig(mode="adaptive", effort_hint="medium"))
    assert out.get("reasoning_effort") == "medium"
