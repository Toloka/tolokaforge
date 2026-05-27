"""Preset routing — ``openrouter_dict_stringify_recovery``.

Pins the routing for open-weights / OpenAI-API-compatible OpenRouter routes
(mimo, Kimi K2.x, DeepSeek V4.x) whose tool-call adapters stringify nested
container arguments. Same recovery recipe as the ``qwen`` preset:
``passthrough`` schema (model sees native ``additionalProperties`` shape) +
``DictMapHints`` (system-prompt nudge) + ``JsonCoerceResponse`` (decodes
``'{...}'`` strings back to native dicts before pydantic validation).

Pre-fix: these models fell through to the ``default`` preset
(``StandardResponse`` — no recovery), and trials hit 20–25 retries when
emitting ``item: '{"subject": ...}'`` on discriminated-union tool args.
Reference incident: mimo-v2.5-pro zendesk_create_item retry loop on
a logistics domain evaluation.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import (
    AnthropicContent,
    DictMapHints,
    JsonCoerceResponse,
    OpenAIContent,
    PassthroughSchema,
    StandardResponse,
    StrictSchema,
    build_capabilities,
)
from tolokaforge.core.llm.presets import resolve_effective_preset

pytestmark = pytest.mark.unit


_RECOVERY_MODELS = [
    "xiaomi/mimo-v2.5-pro",
    "xiaomi/mimo-v2.5",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.7",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4.1",
]


@pytest.mark.parametrize("model", _RECOVERY_MODELS)
def test_recovery_preset_routes_to_passthrough_trio(model: str) -> None:
    """``passthrough`` schema + ``DictMapHints`` + ``JsonCoerceResponse``."""
    caps = build_capabilities(model, "openrouter")
    assert isinstance(caps.schema_sanitizer, PassthroughSchema)
    assert isinstance(caps.prompt_policy, DictMapHints)
    assert isinstance(caps.response_policy, JsonCoerceResponse)
    assert isinstance(caps.content_policy, OpenAIContent)


@pytest.mark.parametrize("model", _RECOVERY_MODELS)
def test_recovery_preset_effective_name(model: str) -> None:
    """``resolve_effective_preset`` returns the new preset id."""
    assert resolve_effective_preset(model, "openrouter") == "openrouter_dict_stringify_recovery"


@pytest.mark.parametrize("model", _RECOVERY_MODELS)
def test_recovery_preset_not_strict_not_anthropic(model: str) -> None:
    """Must not accidentally pick up StrictSchema or the Anthropic content policy."""
    caps = build_capabilities(model, "openrouter")
    assert not isinstance(caps.schema_sanitizer, StrictSchema)
    assert not isinstance(caps.content_policy, AnthropicContent)
    assert not isinstance(caps.response_policy, StandardResponse)


def test_unrelated_model_does_not_match_recovery_preset() -> None:
    """Generic OpenAI / Anthropic / Gemini routes must NOT pick up the preset."""
    for model in ("openai/gpt-5.5", "anthropic/claude-opus-4.7", "google/gemini-3-pro"):
        assert resolve_effective_preset(model, "openrouter") != (
            "openrouter_dict_stringify_recovery"
        )


def test_qwen_route_still_resolves_to_qwen_preset() -> None:
    """``qwen`` preset is declared BEFORE the recovery preset; first-match-wins
    routing keeps Qwen on its own preset rather than the new one."""
    assert resolve_effective_preset("qwen/qwen3.6-plus", "openrouter") == "qwen"
