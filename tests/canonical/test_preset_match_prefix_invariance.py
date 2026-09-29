"""Canonical test — a preset must match the model name a run config actually writes.

A run config names an OpenRouter model as ``openrouter/<vendor>/<slug>``; the
same model appears bare as ``<vendor>/<slug>`` in certificates, pricing and the
audit tooling. Preset matching is ``fnmatch`` over the model string, so a
preset whose globs are all anchored at the vendor (``qwen/*``, ``x-ai/*``)
matches the bare form and **not** the prefixed one — and a model that matches
no preset silently falls through to ``default``, losing its schema sanitizer,
its response policy and its reasoning codec in one step.

Measured when this test was written: four of thirteen live slugs resolved to a
different preset under the two spellings, three of them landing on ``default``
with ``NoReasoningCodec`` while the model was returning reasoning on every
turn. Nothing failed; the trials just quietly got a worse engine.

The fix for a failure here is a ``"*<stem>*"`` glob beside the anchored one —
the shape ``moonshot_kimi_k2`` already uses (``moonshotai/kimi-k2*`` **and**
``*kimi-k2*``) — not an entry in the exemption list below.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.presets import resolve_effective_preset

pytestmark = pytest.mark.canonical


#: Live OpenRouter slugs, one per preset family the engine ships an opinion
#: about. Bare form, exactly as a certificate or the pricing table spells it.
_SLUGS: tuple[str, ...] = (
    "openai/gpt-5.6-sol",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-5",
    "google/gemini-3.1-pro-preview",
    "moonshotai/kimi-k2.7-code",
    "moonshotai/kimi-k3",
    "x-ai/grok-4.5",
    "x-ai/grok-4.6",
    "qwen/qwen3.8-max-0902",
    "qwen/qwen3.8-flash",
    "z-ai/glm-5.3",
    "deepseek/deepseek-v4-pro-0813",
    "xiaomi/mimo-v2.6-pro",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "minimax/minimax-m3",
    "thinkingmachines/inkling",
)


@pytest.mark.parametrize("slug", _SLUGS)
def test_a_preset_matches_both_spellings_of_the_same_model(slug: str) -> None:
    bare = resolve_effective_preset(slug, "openrouter")
    prefixed = resolve_effective_preset(f"openrouter/{slug}", "openrouter")

    assert bare == prefixed, (
        f"{slug!r} resolves to {bare!r} bare and {prefixed!r} as "
        f"'openrouter/{slug}'. A run config writes the prefixed form, so that "
        f"is the one that reaches a trial. Add a '*<stem>*' glob beside the "
        f"vendor-anchored one on the {bare!r} preset — the shape "
        f"moonshot_kimi_k2 uses — rather than relying on the caller to strip "
        f"the prefix."
    )


def test_no_shipped_model_falls_through_to_the_default_preset() -> None:
    """``default`` is the shape for a model the engine has no opinion about.

    Every slug above is one the engine *does* have an opinion about, so
    landing on ``default`` means the opinion was written and then missed.
    """
    fell_through = [
        slug
        for slug in _SLUGS
        if resolve_effective_preset(f"openrouter/{slug}", "openrouter") == "default"
    ]

    assert not fell_through, (
        f"{fell_through} match no preset under the spelling a run config uses, "
        "so they run on the engine-wide defaults: passthrough schema, no "
        "response policy and NoReasoningCodec. Widen the owning preset's globs."
    )
