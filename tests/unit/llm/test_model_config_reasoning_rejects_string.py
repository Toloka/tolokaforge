"""Regression guard — ``ModelConfig(reasoning="medium")`` must raise.

Stage 0 migration rejects the legacy bare-string form for
:class:`~tolokaforge.core.models.ModelConfig.reasoning`. See plan § Stage 0
(``ReasoningConfig`` loader) and :mod:`docs/CONFIG.md`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.llm.reasoning import ReasoningConfig
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


class TestModelConfigRejectsBareStringReasoning:
    def test_bare_string_reasoning_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7", reasoning="medium")
        message = str(exc_info.value)
        assert "struct" in message
        assert "medium" in message

    def test_default_reasoning_is_off_struct(self) -> None:
        cfg = ModelConfig(provider="openrouter", name="foo/bar")
        assert isinstance(cfg.reasoning, ReasoningConfig)
        assert cfg.reasoning.mode == "off"

    def test_dict_form_is_parsed(self) -> None:
        cfg = ModelConfig(
            provider="openrouter",
            name="foo/bar",
            reasoning={"mode": "adaptive", "effort_hint": "high"},
        )
        assert cfg.reasoning.mode == "adaptive"
        assert cfg.reasoning.effort_hint == "high"

    def test_struct_instance_passes_through(self) -> None:
        r = ReasoningConfig(mode="budget", budget_tokens=8000)
        cfg = ModelConfig(provider="openrouter", name="foo/bar", reasoning=r)
        assert cfg.reasoning is r


class TestMessageRejectsBareStringReasoning:
    def test_bare_string_reasoning_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            Message(role=MessageRole.ASSISTANT, content="ok", reasoning="thinking text")
        message = str(exc_info.value)
        assert "StructuredReasoning" in message

    def test_none_reasoning_ok(self) -> None:
        msg = Message(role=MessageRole.ASSISTANT, content="ok", reasoning=None)
        assert msg.reasoning is None

    def test_dict_form_accepted(self) -> None:
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="ok",
            reasoning={
                "blocks": [{"type": "thinking", "text": "step 1"}],
                "summary": None,
            },
        )
        assert msg.reasoning is not None
        assert msg.reasoning.blocks[0].text == "step 1"
