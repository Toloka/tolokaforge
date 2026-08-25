"""Reference impl of :class:`JudgeModelProvider` wrapping
:class:`~tolokaforge.core.llm.client.LLMClient`.

Registered under the name ``litellm`` in the
``tolokaforge.judge_model_providers`` entry-point group. The name matches
the underlying transport (LiteLLM); a downstream ``openai_direct`` or
``vertex_ai`` provider registers alongside without touching this module.

This module holds the ONLY concrete impl of :class:`JudgeModelProvider` in
the shipping distribution, so the ``.importlinter`` contract can forbid
composite from importing it without also forbidding the Protocol module.
"""

from __future__ import annotations

from tolokaforge.core.grading.judge_model_provider import (
    JudgeModel,
    JudgeModelProvider,
)
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import ModelConfig

__all__ = [
    "LiteLLMJudgeModelProvider",
]


class LiteLLMJudgeModelProvider:
    """Build a :class:`JudgeModel` backed by
    :class:`~tolokaforge.core.llm.client.LLMClient`.

    :class:`LLMClient` structurally satisfies :class:`JudgeModel` (it
    already carries ``.generate`` per :class:`LoopLLMClient` and
    ``.classify_loop_error`` bound to its provider's rate-limit
    patterns), so ``build`` just constructs the client and returns it.
    """

    def build(self, model_config: ModelConfig) -> JudgeModel:
        return LLMClient(model_config)


def _litellm_judge_model_provider_factory() -> JudgeModelProvider:
    """Entry-point factory. Arg-less; returns a fresh provider instance."""
    return LiteLLMJudgeModelProvider()
