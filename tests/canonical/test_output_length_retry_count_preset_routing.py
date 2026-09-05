"""Canonical test — preset → ``output_length_retry_count`` routing.

Every currently-registered preset resolves to the default
``output_length_retry_count == 0``. Opting a preset in requires per-model
+ per-workload observed evidence for the visible-truncation shape, recorded
in the preset comment; the opt-in doubles reasoning spend on the failing
sample, so a blanket enable is inappropriate.

An unintentional preset opt-in fails this test. The right move on a
failure is to remove the offending overlay entry, not to mute the
assertion — matching the discipline ``test_empty_retry_count_preset_routing.py``
establishes for the sibling retry class.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities

pytestmark = pytest.mark.canonical


_OUTPUT_LENGTH_RETRY_ZERO_MODELS = [
    ("openai/gpt-4o", "openrouter"),
    ("anthropic/claude-opus-4.7", "openrouter"),
    ("anthropic/claude-sonnet-4.7", "openrouter"),
    ("openai/gpt-5.5", "openrouter"),
    ("x-ai/grok-4", "openrouter"),
    ("qwen/qwen3-coder", "openrouter"),
    ("google/gemini-3.1-pro-preview", "openrouter"),
    ("moonshotai/kimi-k3", "openrouter"),
    ("moonshotai/kimi-k2.6", "openrouter"),
    ("claude-opus-5", "anthropic"),
]


@pytest.mark.parametrize(("model", "provider"), _OUTPUT_LENGTH_RETRY_ZERO_MODELS)
def test_preset_leaves_output_length_retry_count_zero(model: str, provider: str) -> None:
    caps = build_capabilities(model, provider)
    assert caps.output_length_retry_count == 0, (
        f"{model!r} must resolve to output_length_retry_count=0. Opt-ins "
        "require per-workload truncation-rate evidence recorded in the "
        "preset comment; if you intended to opt this preset in, add the "
        f"evidence there rather than mute this assertion. Got: {caps.output_length_retry_count}."
    )
