"""Judge model provider seam — Protocol + factory alias.

A :class:`JudgeModel` is the LLM callable surface an :class:`LLMJudge`
consumes. It composes :class:`~tolokaforge.core.loop.LoopLLMClient` — the
``.generate`` seam the shared :class:`~tolokaforge.core.loop.ToolCallingLoop`
already drives — with the ``.classify_loop_error`` method the judge passes
as the loop's ``classify_error=`` argument (see
:meth:`~tolokaforge.core.grading.judge.LLMJudge.run`). Any provider that
implements those two methods is a valid judge model; the shipping
:class:`~tolokaforge.core.llm.client.LLMClient` structurally satisfies the
composed shape.

A :class:`JudgeModelProvider` builds a :class:`JudgeModel` from a
:class:`~tolokaforge.core.models.ModelConfig`. Discovery goes through
:func:`~tolokaforge.core.plugin_registry.load_judge_model_provider` over
the ``tolokaforge.judge_model_providers`` entry-point group; a downstream
package registers an ``openai_direct`` or ``vertex_ai`` provider alongside
the shipping ``litellm`` one without a framework PR.

The reference impl lives in
:mod:`tolokaforge.core.grading.default_judge_model_provider` — this
Protocol module carries no behaviour so the composite dispatch can name
:class:`JudgeModel` / :class:`JudgeModelProvider` without ever reaching
the reference impl through it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.loop import LoopLLMClient, TerminationDecision
from tolokaforge.core.models import ModelConfig

__all__ = [
    "JudgeModel",
    "JudgeModelProvider",
    "JudgeModelProviderFactory",
]


@runtime_checkable
class JudgeModel(LoopLLMClient, Protocol):
    """The LLM callable surface an :class:`LLMJudge` consumes.

    Inherits :class:`~tolokaforge.core.loop.LoopLLMClient` for the
    ``.generate(...)`` seam the shared :class:`ToolCallingLoop` drives, adds
    :meth:`classify_loop_error` because ``LLMJudge.run`` binds
    ``client.classify_loop_error`` as the loop's ``classify_error=``
    argument, and adds :meth:`sanitize_tools_for_execution` because
    ``LLMJudge.run`` also builds the loop's ``validation_schemas_by_tool``
    map from it (so the tool executor validates against the schema the
    judge model was shown, not the schema the tool declares). A downstream
    provider fronts a different LLM engine by implementing these three
    methods; :class:`~tolokaforge.core.llm.client.LLMClient` already
    satisfies the composed shape structurally.
    """

    def classify_loop_error(self, exc: Exception) -> TerminationDecision: ...

    def sanitize_tools_for_execution(
        self, tools: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]: ...


@runtime_checkable
class JudgeModelProvider(Protocol):
    """Build a :class:`JudgeModel` from a
    :class:`~tolokaforge.core.models.ModelConfig`.

    Providers front different LLM transports (``litellm`` today; ``openai_direct``
    or ``vertex_ai`` alongside a downstream provider) without leaking the
    transport into the judge.
    """

    def build(self, model_config: ModelConfig) -> JudgeModel: ...


JudgeModelProviderFactory = Callable[[], JudgeModelProvider]
