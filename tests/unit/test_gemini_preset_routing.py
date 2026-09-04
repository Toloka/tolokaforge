"""Unit guard for the ``gemini`` preset family's match-glob routing.

Asserts every Google Gemini model identifier under integration-test
coverage is routed through the named preset that owns it:

* ``gemini_31_pro_preview`` — the model-specific overlay for
  ``google/gemini-3.1-pro-preview`` (and its OpenRouter-prefixed
  variant). Carries the generic ``gemini`` policy trio verbatim plus
  ``default_max_turns: 90``. Exact-match globs only.
* ``gemini_35_flash_recursive`` / ``gemini_36_flash_recursive`` /
  ``gemini_37_flash_recursive`` — Flash-lineage overlays for the
  recursive-schema fix. Exact-match globs only.
* ``gemini`` — the generic fallback that captures every other Gemini
  identifier (family globs).

The first-match-wins discipline matters here: each model-specific
overlay must sit BEFORE the generic ``gemini:`` block, and the
generic block must sit AFTER any preset whose match globs could
shadow it. If a future preset is added with overlapping globs, this
test catches the silent reroute.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.presets import resolve_effective_preset

pytestmark = pytest.mark.canonical


_GEMINI_MODELS = (
    # Both variants under live integration test (see registry.py).
    "google/gemini-3-flash-preview",
    # Older family members — the preset must capture them too so we
    # don't leave stale fallthrough on existing eval configs.
    "google/gemini-3.0-flash",
    "google/gemini-3.1-pro",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    # OpenRouter-flavoured prefix (litellm sometimes sees the doubled form).
    "openrouter/google/gemini-3-flash-preview",
)


_GEMINI_31_PRO_PREVIEW_MODELS = (
    "google/gemini-3.1-pro-preview",
    "openrouter/google/gemini-3.1-pro-preview",
)


@pytest.mark.parametrize("model", _GEMINI_MODELS)
def test_gemini_models_route_to_gemini_preset(model: str) -> None:
    """Every Gemini-shaped model name resolves to the named preset."""
    name = resolve_effective_preset(model, "openrouter")
    assert name == "gemini", (
        f"Expected {model!r} to route to 'gemini' preset, got {name!r}. "
        "If a new preset was added with overlapping match globs, restore "
        "first-match-wins by moving the ``gemini:`` block earlier in "
        "model_presets.yaml."
    )


@pytest.mark.parametrize("model", _GEMINI_31_PRO_PREVIEW_MODELS)
def test_gemini_31_pro_preview_routes_to_dedicated_preset(model: str) -> None:
    """The exact ``google/gemini-3.1-pro-preview`` slug (bundled and
    OpenRouter-prefixed) resolves to the dedicated overlay, not the
    generic ``gemini`` preset. The overlay sits BEFORE the generic block
    in ``model_presets.yaml`` so first-match-wins picks it up.
    """
    name = resolve_effective_preset(model, "openrouter")
    assert name == "gemini_31_pro_preview", (
        f"Expected {model!r} to route to 'gemini_31_pro_preview' preset, "
        f"got {name!r}. Restore first-match-wins by keeping the "
        "``gemini_31_pro_preview:`` block BEFORE the generic ``gemini:`` "
        "block in model_presets.yaml."
    )


def test_non_gemini_models_do_not_route_to_gemini_preset() -> None:
    """The new preset must not shadow other vendors' routing."""
    sample_non_gemini = (
        ("anthropic/claude-opus-4.7", "anthropic_claude_4_7"),
        ("anthropic/claude-sonnet-4.6", "anthropic"),
        ("openai/gpt-5.5", "openai_gpt5"),
        ("x-ai/grok-4", "xai_grok"),
        ("qwen/qwen3.6-plus", "qwen"),
        ("openai/gpt-4o", "default"),
    )
    for model, expected in sample_non_gemini:
        name = resolve_effective_preset(model, "openrouter")
        assert name == expected, f"{model!r} should resolve to {expected!r}, not {name!r}"
