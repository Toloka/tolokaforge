"""``JudgeKind`` — typed judge-kind Protocol.

Every entry registered under ``tolokaforge.judge_kinds`` resolves to a
class satisfying :class:`JudgeKind`. The class carries a ``NAME`` matching
its entry-point name (so a pyproject typo surfaces at discovery, not at
judge time) and an ``evaluate`` method that produces a
:class:`~tolokaforge.core.grading.judge_result.JudgeResult` from the
per-trial rubric evidence.

``evaluate`` is kwargs-only. Callers bind arguments at dispatch time so a
future field lands mid-list without positional drift on downstream
adapters. The signature mirrors :meth:`LLMJudge.run` verbatim on the
per-trial evidence surface (``rubric`` + ``agent_system_prompt`` +
``transcript`` + ``db_reader`` + ``kb_search`` + ``workspace_dir`` +
``extra_read_tools`` + ``state_diff``) plus construction inputs the kind
must have to build its own judge instance
(``judge_model_config`` + ``judge_model_provider``), plus per-trial
customization (``disable_knowledge_search`` + ``custom_system_prompt`` +
``include_agent_system_prompt``) and a ``kind_config`` handle downstream
kinds read from.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.judge_result import JudgeResult
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "JudgeKind",
]


@runtime_checkable
class JudgeKind(Protocol):
    """Marker + evaluator Protocol every ``tolokaforge.judge_kinds`` entry resolves to.

    ``NAME`` MUST equal the entry-point name so a downstream typo in
    ``pyproject.toml`` surfaces at discovery, not at judge time.

    ``evaluate`` produces a :class:`JudgeResult`; a judge malfunction
    (malformed ``submit_report`` past retries, budget/turn exhaustion,
    a loop-terminal exception) surfaces as
    :attr:`JudgeStatus.ERRORED` with ``score is None`` — never a
    ``0.0`` / ``0.5`` fallback.
    """

    NAME: ClassVar[str]

    def evaluate(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None,
        kb_search: KnowledgeSearch | None,
        workspace_dir: Path | None,
        extra_read_tools: list[Tool],
        state_diff: str | None,
        judge_model_config: ModelConfig,
        judge_model_provider: JudgeModelProvider,
        disable_knowledge_search: bool,
        custom_system_prompt: str | None,
        include_agent_system_prompt: bool,
        kind_config: Mapping[str, Any] | None,
        logger: StructuredLogger,
    ) -> JudgeResult: ...
