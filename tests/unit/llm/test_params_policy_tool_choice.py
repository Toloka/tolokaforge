"""``supports_tool_choice_auto`` — omit a VALUE the provider has no word for.

Cohere's Chat API accepts only ``REQUIRED`` and ``NONE`` for ``tool_choice``; there
is no ``AUTO`` (https://docs.cohere.com/reference/chat). Omitting the parameter is
its documented way to express "the model is free to choose whether to use the
specified tools or not" — which is exactly what ``auto`` means, and ``auto`` is the
only value this codebase ever sends.

So this flag is not a workaround to be removed later: it reproduces the intended
semantics in the provider's own vocabulary, and a model measured under it stays
comparable with one measured without it.

It is deliberately about the VALUE, not the parameter — Cohere DOES support
``tool_choice``, just not ``auto``. An explicit ``REQUIRED``/``NONE`` therefore still
goes through: dropping a caller's explicit forcing would change what the model was
asked to do, which is a behaviour change rather than a no-op. That, and the scoping,
are what the tests below pin.

The visible symptom is a transport refusing the parameter first (litellm:
``UnsupportedParamsError: azure_ai does not support parameters: ['tool_choice']``),
which reads as a missing capability rather than a missing enum value.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.params_policy import GenerationParams
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit

_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def _kwargs(model: str, tool_choice: str | None = "auto") -> dict[str, Any]:
    client = LLMClient(ModelConfig(provider="openrouter", name=model))
    return client._build_kwargs(
        system="s",
        messages=[Message(role=MessageRole.USER, content="x")],
        tools=[_TOOL],
        tool_choice=tool_choice,
        temperature=None,
        seed=None,
        reasoning=None,
        top_p=None,
        max_tokens=None,
    )


class TestFlagDefault:
    def test_default_is_true_so_no_model_changes_by_accident(self) -> None:
        assert GenerationParams().supports_tool_choice_auto is True

    def test_flag_is_settable_from_a_preset_params_block(self) -> None:
        """``params:`` keys are GenerationParams kwargs, so this is the whole wiring."""
        assert GenerationParams(supports_tool_choice_auto=False).supports_tool_choice_auto is False


class TestBuildKwargs:
    def test_auto_is_omitted_when_the_provider_has_no_such_value(self) -> None:
        kwargs = _kwargs("azure_ai/cohere-command-a-plus-05-2026")
        assert "tool_choice" not in kwargs
        # The tools themselves must still go — omitting those would be a real
        # capability change rather than a parameter one.
        assert kwargs["tools"] == [_TOOL]

    @pytest.mark.parametrize("forcing", ["required", "none"])
    def test_an_explicit_forcing_still_goes_through(self, forcing: str) -> None:
        """Cohere honours REQUIRED/NONE; silently dropping them would change intent."""
        kwargs = _kwargs("azure_ai/cohere-command-a-plus-05-2026", tool_choice=forcing)
        assert kwargs["tool_choice"] == forcing

    def test_every_other_model_still_sends_it(self) -> None:
        assert _kwargs("anthropic/claude-opus-4.7")["tool_choice"] == "auto"

    def test_no_tool_choice_requested_stays_absent(self) -> None:
        assert "tool_choice" not in _kwargs("anthropic/claude-opus-4.7", tool_choice=None)


class TestPresetScope:
    """The flag must reach exactly the model that needs it, and no sibling."""

    @pytest.mark.parametrize(
        "model",
        [
            "azure_ai/cohere-command-a-plus-05-2026",
            "cohere-command-a-plus-05-2026",
        ],
    )
    def test_cohere_a_plus_matches_under_either_naming(self, model: str) -> None:
        caps = build_capabilities(model, "openrouter")
        assert caps.params_policy.supports_tool_choice_auto is False

    @pytest.mark.parametrize(
        "model",
        [
            "cohere/command-a",
            "anthropic/claude-opus-4.7",
            "openai/gpt-5.6",
            "x-ai/grok-4.5",
        ],
    )
    def test_siblings_are_untouched(self, model: str) -> None:
        """Including the older Cohere Command A, which is a different route."""
        caps = build_capabilities(model, "openrouter")
        assert caps.params_policy.supports_tool_choice_auto is True
