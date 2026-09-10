"""``LiteLLMJudgeModelProvider`` — reference impl structural lock.

The shipping ``JudgeModelProvider`` builds an
:class:`~tolokaforge.core.llm.client.LLMClient` from a
:class:`~tolokaforge.core.models.ModelConfig`. This test pins the
:class:`JudgeModel` shape the judge consumes (``.generate`` from
:class:`LoopLLMClient`, ``.classify_loop_error``, and the ``.capabilities``
attribute the ``agentic_rubric`` kind reads for capability threading) so a
rename of either method, a swap of the reference impl for one that does not
carry them, or a narrowing of the Protocol trips before it lands. No LLM
call is made; the client is not driven, only inspected.
"""

from __future__ import annotations

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.default_judge_model_provider import (
    LiteLLMJudgeModelProvider,
)
from tolokaforge.core.grading.judge_model_provider import JudgeModel
from tolokaforge.core.llm.capabilities import ModelCapabilities
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
    """Runtime-checkable :class:`JudgeModel` accepts the built client.

    Regression lock for the ``.capabilities`` widening: ``LLMClient``
    already builds it from its ``ModelConfig`` via ``build_capabilities``,
    so this is additive for the only shipping provider.
    """
    judge_model = LiteLLMJudgeModelProvider().build(_model_config())
    assert isinstance(judge_model, JudgeModel)
    assert isinstance(judge_model.capabilities, ModelCapabilities)


def test_scripted_llm_client_satisfies_the_widened_judge_model_protocol() -> None:
    """``ScriptedLLMClient`` — the unit/parity-test harness — conforms too.

    Defaults to an unmodified ``ModelCapabilities()``. Whole-object ``==``
    is unreliable here: ``ModelCapabilities``'s composed policy fields
    (``schema_sanitizer``, ``prompt_policy``, ...) are stateless classes
    with no ``__eq__`` of their own, so even two freshly constructed
    ``ModelCapabilities()`` are not ``==``. This pins the scalar knobs the
    ``agentic_rubric`` kind actually threads (Stage 3) instead.
    """
    client = ScriptedLLMClient(script=[])
    assert isinstance(client, JudgeModel)
    default = ModelCapabilities()
    assert client.capabilities.empty_retry_count == default.empty_retry_count
    assert client.capabilities.output_length_retry_count == default.output_length_retry_count
    assert client.capabilities.parser_error_retry_count == default.parser_error_retry_count
    assert client.capabilities.tool_output_max_chars == default.tool_output_max_chars
    assert client.capabilities.max_context_tokens == default.max_context_tokens
    assert client.capabilities.context_watermark == default.context_watermark


def test_scripted_llm_client_accepts_a_non_default_capabilities_override() -> None:
    """The ``capabilities`` kwarg is settable — later stages need a client
    whose capabilities opt into retry/summarization behaviour."""
    caps = ModelCapabilities(empty_retry_count=2)
    client = ScriptedLLMClient(script=[], capabilities=caps)
    assert client.capabilities is caps
