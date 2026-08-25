"""``LiteLLMJudgeModelProvider`` — reference impl structural lock.

The shipping ``JudgeModelProvider`` builds an
:class:`~tolokaforge.core.llm.client.LLMClient` from a
:class:`~tolokaforge.core.models.ModelConfig`. This test pins the two-method
:class:`JudgeModel` shape the judge consumes (``.generate`` from
:class:`LoopLLMClient`, plus ``.classify_loop_error``) so a rename of either
method — or a swap of the reference impl for one that does not carry them —
trips before it lands. No LLM call is made; the client is not driven, only
inspected.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.default_judge_model_provider import (
    LiteLLMJudgeModelProvider,
)
from tolokaforge.core.grading.judge_model_provider import JudgeModel
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import ModelConfig

pytestmark = pytest.mark.unit


def _model_config() -> ModelConfig:
    return ModelConfig(provider="anthropic", name="claude")


def test_build_returns_an_llm_client_instance() -> None:
    """The reference provider fronts :class:`LLMClient` — locks the impl choice."""
    judge_model = LiteLLMJudgeModelProvider().build(_model_config())
    assert isinstance(judge_model, LLMClient)


def test_built_judge_model_exposes_generate_and_classify_loop_error() -> None:
    """The two-method :class:`JudgeModel` shape the judge consumes.

    Structural check via :func:`hasattr` (no LLM call — no API key needed).
    """
    judge_model = LiteLLMJudgeModelProvider().build(_model_config())
    assert callable(getattr(judge_model, "generate", None))
    assert callable(getattr(judge_model, "classify_loop_error", None))


def test_built_judge_model_satisfies_the_judge_model_protocol() -> None:
    """Runtime-checkable :class:`JudgeModel` accepts the built client."""
    judge_model = LiteLLMJudgeModelProvider().build(_model_config())
    assert isinstance(judge_model, JudgeModel)
