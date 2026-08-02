"""``supports_tool_choice`` — omit a kwarg a transport rejects outright.

litellm's ``azure_ai`` provider 400s on ``tool_choice`` rather than ignoring it
(``UnsupportedParamsError: azure_ai does not support parameters:
['tool_choice']``), so every tool-calling request fails before the model sees it
and the model reads as incapable when it is not.

The reason this is safe to omit rather than a measurement compromise is pinned
here too: ``"auto"`` is the only value this codebase ever sends, and per the
OpenAI spec ``auto`` is the default when ``tools`` are present. Dropping it
leaves the decision with the model, so a model measured under this flag stays
comparable with one measured without it.
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
        assert GenerationParams().supports_tool_choice is True

    def test_flag_is_settable_from_a_preset_params_block(self) -> None:
        """``params:`` keys are GenerationParams kwargs, so this is the whole wiring."""
        assert GenerationParams(supports_tool_choice=False).supports_tool_choice is False


class TestBuildKwargs:
    def test_tool_choice_is_omitted_when_the_transport_rejects_it(self) -> None:
        kwargs = _kwargs("azure_ai/cohere-command-a-plus-05-2026")
        assert "tool_choice" not in kwargs
        # The tools themselves must still go — omitting those would be a real
        # capability change rather than a parameter one.
        assert kwargs["tools"] == [_TOOL]

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
        assert caps.params_policy.supports_tool_choice is False

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
        assert caps.params_policy.supports_tool_choice is True
