"""Budget-mode reasoning routing in ``params_policy``.

Two transport shapes for reasoning, picked by the preset + provider overlay:

* **OpenRouter routing** (``reasoning_via_extra_body=True``, set by the
  ``openrouter`` provider overlay): every reasoning request rides in
  ``extra_body.reasoning``. For ``mode="budget"`` this is
  ``{"max_tokens": N, "enabled": True}``; for ``mode="adaptive"`` it is
  ``{"effort": <hint>, "enabled": True}``. Verified against the OpenRouter
  docs (https://openrouter.ai/docs/use-cases/reasoning-tokens) and a live
  probe of Claude Opus 4.7 (2026-04-27): the top-level ``thinking={…}``
  kwarg that litellm exposes for direct-Anthropic routing is silently
  dropped by OpenRouter, surfacing as ``reasoning_tokens=0`` and an empty
  ``thinking_blocks`` list.

* **Direct Anthropic API** (``reasoning_via_thinking_kwarg=True`` only,
  no OpenRouter overlay): emits litellm's canonical top-level
  ``thinking={"type":"enabled","budget_tokens":N}`` kwarg.

When both flags are set (OpenRouter overlay applied on top of an
``anthropic_claude_4_7`` preset that declares thinking-kwarg native),
``reasoning_via_extra_body`` wins because the actual transport is
OpenRouter. ``drop_sampling_when_thinking`` still applies whenever
budget-mode reasoning is *active* — Anthropic strips sampling regardless
of which transport carried the request.

Fail-loud rules:

* Budget mode on the direct-Anthropic transport with no ``budget_tokens``
  and no preset default → ``ValueError``.
* Mis-configured preset (thinking-kwarg declared, ``mode="adaptive"``) →
  ``ValueError``.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.reasoning import ReasoningConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _adapt(
    caps,
    reasoning: ReasoningConfig | None,
    *,
    config_reasoning: ReasoningConfig | None = None,
    temperature: float | None = 0.6,
    top_p: float | None = None,
) -> dict:
    kwargs: dict = {"model": "m", "messages": []}
    if top_p is not None:
        kwargs["top_p"] = top_p
    return caps.params_policy.adapt(
        kwargs=kwargs,
        config_temperature=temperature,
        config_seed=None,
        config_reasoning=config_reasoning or ReasoningConfig(),
        temperature=None,
        seed=None,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Claude 4.7 via OpenRouter — budget mode routes via extra_body.reasoning
# ---------------------------------------------------------------------------


def test_claude_47_via_openrouter_emits_extra_body_reasoning_with_budget() -> None:
    """Claude 4.7 via OpenRouter must use ``extra_body.reasoning.max_tokens``
    — OpenRouter silently drops the top-level ``thinking`` kwarg, so we
    cannot rely on it here. Sampling params are still dropped because
    Anthropic itself strips them whenever thinking is active."""
    caps = build_capabilities("anthropic/claude-opus-4.7", "openrouter")
    kwargs = _adapt(
        caps,
        ReasoningConfig(mode="budget", budget_tokens=8000),
        temperature=0.6,
        top_p=0.9,
    )

    assert kwargs.get("extra_body", {}).get("reasoning") == {
        "max_tokens": 8000,
        "enabled": True,
    }
    # Top-level ``thinking`` kwarg must NOT be sent — OpenRouter would drop it.
    assert "thinking" not in kwargs
    # Sampling dropped when reasoning is active
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "reasoning_effort" not in kwargs


def test_claude_47_via_openrouter_uses_preset_budget_default_when_config_omits() -> None:
    """With preset default ``reasoning_budget_default: 8000`` the user can pass
    ``ReasoningConfig(mode="budget")`` bare and still get a concrete budget
    routed through ``extra_body.reasoning``."""
    caps = build_capabilities("anthropic/claude-opus-4.7", "openrouter")
    kwargs = _adapt(caps, ReasoningConfig(mode="budget"))

    assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 8000, "enabled": True}


def test_claude_47_via_openrouter_budget_tokens_override_preset_default() -> None:
    caps = build_capabilities("anthropic/claude-opus-4.7", "openrouter")
    kwargs = _adapt(caps, ReasoningConfig(mode="budget", budget_tokens=3000))

    assert kwargs["extra_body"]["reasoning"]["max_tokens"] == 3000


# ---------------------------------------------------------------------------
# Direct Anthropic API (no openrouter overlay) — keeps thinking kwarg
# ---------------------------------------------------------------------------


def test_claude_47_direct_anthropic_emits_top_level_thinking_kwarg() -> None:
    """Without the OpenRouter overlay (direct Anthropic transport),
    ``reasoning_via_thinking_kwarg`` wins and emits the litellm canonical
    top-level ``thinking={…}`` kwarg."""
    caps = build_capabilities("anthropic/claude-opus-4.7", provider="anthropic")
    kwargs = _adapt(
        caps,
        ReasoningConfig(mode="budget", budget_tokens=8000),
        temperature=0.6,
        top_p=0.9,
    )

    assert kwargs.get("thinking") == {"type": "enabled", "budget_tokens": 8000}
    assert "reasoning" not in kwargs.get("extra_body", {})
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs


# ---------------------------------------------------------------------------
# Adaptive mode preserved for older Claude (non-4.7)
# ---------------------------------------------------------------------------


def test_claude_46_adaptive_mode_keeps_extra_body_path() -> None:
    """Claude 4.5/4.6 (generic ``anthropic`` preset) routes via the existing
    ``extra_body.reasoning`` path — Stage 0 behaviour preserved."""
    caps = build_capabilities("anthropic/claude-opus-4.6", "openrouter")
    kwargs = _adapt(
        caps,
        ReasoningConfig(mode="adaptive", effort_hint="medium"),
        temperature=0.6,
        top_p=0.9,
    )

    assert kwargs.get("extra_body", {}).get("reasoning") == {
        "effort": "medium",
        "enabled": True,
    }
    # No thinking kwarg — this preset is not thinking-kwarg-native
    assert "thinking" not in kwargs
    # temperature + top_p preserved (no drop-sampling flag on this preset)
    assert kwargs.get("temperature") == 0.6
    assert kwargs.get("top_p") == 0.9


# ---------------------------------------------------------------------------
# Off mode emits nothing regardless of preset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-4.7",
        "anthropic/claude-opus-4.6",
        "openai/gpt-5.5",
    ],
)
def test_off_mode_emits_no_reasoning_kwargs(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    kwargs = _adapt(caps, ReasoningConfig(mode="off"))

    assert "thinking" not in kwargs
    assert "reasoning_effort" not in kwargs
    assert "reasoning" not in kwargs.get("extra_body", {})


# ---------------------------------------------------------------------------
# Non-Anthropic budget mode → fallback to effort
# ---------------------------------------------------------------------------


def test_gpt5_budget_mode_falls_back_to_reasoning_effort() -> None:
    """Budget mode on a non-Anthropic preset emits ``reasoning_effort`` (or
    ``extra_body.reasoning.effort`` on OpenRouter) — NOT the ``thinking`` kwarg."""
    # Use provider="" to avoid the openrouter overlay that would route via
    # extra_body; this isolates the non-Anthropic routing to top-level
    # ``reasoning_effort``.
    caps = build_capabilities("openai/gpt-5.5", provider="")
    kwargs = _adapt(
        caps,
        ReasoningConfig(mode="budget", budget_tokens=8000, effort_hint="high"),
        temperature=None,
        top_p=None,
    )

    assert "thinking" not in kwargs
    assert kwargs.get("reasoning_effort") == "high"


def test_gpt5_budget_mode_without_effort_hint_emits_nothing() -> None:
    """Budget mode on a non-Anthropic preset with no ``effort_hint`` emits no
    reasoning kwargs — we refuse to ship an undefined request shape."""
    caps = build_capabilities("openai/gpt-5.5", provider="")
    kwargs = _adapt(
        caps,
        ReasoningConfig(mode="budget", budget_tokens=8000),
        temperature=None,
        top_p=None,
    )

    assert "thinking" not in kwargs
    assert "reasoning_effort" not in kwargs


# ---------------------------------------------------------------------------
# Fail-loud rules
# ---------------------------------------------------------------------------


def test_claude_47_direct_anthropic_adaptive_mode_raises_misconfiguration() -> None:
    """Direct-Anthropic transport: ``reasoning_via_thinking_kwarg=True`` +
    ``mode=adaptive`` is a preset misconfiguration — Anthropic's API has no
    notion of adaptive effort. Surface it instead of silently stripping the knob.

    (Via OpenRouter the same preset works because OpenRouter accepts
    ``extra_body.reasoning.effort`` for any model — see
    ``test_claude_47_via_openrouter_emits_extra_body_reasoning_with_budget``.)
    """
    caps = build_capabilities("anthropic/claude-opus-4.7", provider="anthropic")
    with pytest.raises(ValueError, match="reasoning_via_thinking_kwarg"):
        _adapt(caps, ReasoningConfig(mode="adaptive", effort_hint="medium"))


def test_budget_mode_without_budget_tokens_and_no_default_raises() -> None:
    """Budget mode on an Anthropic-thinking-native preset with no
    ``budget_tokens`` and no preset default must raise — AGENTS.md
    "surface failures" rule."""
    # Build a minimal thinking-kwarg-native GenerationParams directly so we
    # can simulate a preset that omits ``reasoning_budget_default``.
    from tolokaforge.core.llm.params_policy import GenerationParams

    params = GenerationParams(
        supports_seed=False,
        reasoning_via_thinking_kwarg=True,
        drop_sampling_when_thinking=True,
        reasoning_budget_default=None,
    )
    with pytest.raises(ValueError, match="budget_tokens"):
        params.adapt(
            kwargs={"model": "m", "messages": []},
            config_temperature=None,
            config_seed=None,
            config_reasoning=ReasoningConfig(),
            temperature=None,
            seed=None,
            reasoning=ReasoningConfig(mode="budget"),
        )


# ---------------------------------------------------------------------------
# drop_sampling_when_thinking — only drops when thinking is actually active
# ---------------------------------------------------------------------------


def test_drop_sampling_only_triggers_when_thinking_emitted() -> None:
    """On Claude 4.7 preset with ``mode="off"``, sampling params must be kept
    (no thinking active → no drop)."""
    caps = build_capabilities("anthropic/claude-opus-4.7", "openrouter")
    kwargs = _adapt(
        caps,
        ReasoningConfig(mode="off"),
        temperature=0.6,
        top_p=0.9,
    )

    assert kwargs.get("temperature") == 0.6
    assert kwargs.get("top_p") == 0.9
    assert "thinking" not in kwargs


def test_drop_sampling_wins_over_explicit_top_p_on_claude_47() -> None:
    """A user explicitly setting ``top_p=0.5`` on Claude 4.7 + budget mode
    still loses to ``drop_sampling_when_thinking`` — the preset owns the
    final decision, matching Anthropic's server-side rule. (OpenRouter
    transport: budget rides ``extra_body.reasoning.max_tokens``.)"""
    caps = build_capabilities("anthropic/claude-opus-4.7", "openrouter")
    kwargs = _adapt(
        caps,
        ReasoningConfig(mode="budget", budget_tokens=4000),
        temperature=0.3,
        top_p=0.5,
    )

    assert kwargs["extra_body"]["reasoning"]["max_tokens"] == 4000
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
