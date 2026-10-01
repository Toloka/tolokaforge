"""``JudgeKind`` — typed judge-kind Protocol.

Every entry registered under ``tolokaforge.judge_kinds`` resolves to a
class satisfying :class:`JudgeKind`. The class carries a ``NAME`` matching
its entry-point name (so a pyproject typo surfaces at discovery, not at
judge time) and an ``evaluate`` method that produces a
:class:`~tolokaforge.core.grading.judge_result.JudgeResult` from the
per-trial rubric evidence.

``evaluate`` is kwargs-only. It takes the per-trial evidence surface, which
mirrors :meth:`LLMJudge.run` (``rubric`` + ``agent_system_prompt`` +
``transcript`` + ``db_reader`` + ``kb_search`` + ``workspace_dir`` +
``extra_read_tools`` + ``state_diff``), the construction inputs the kind
builds its own judge from (``judge_model_config`` + ``judge_model_provider``),
the trial's :class:`~tolokaforge.core.grading.judge_kinds.options.JudgeTrialOptions`
(``options``), and the ``kind_config`` bag a kind reads its own settings from.

Per-trial customization is one object so this signature stays fixed: adding a
knob is a field on :class:`JudgeTrialOptions` whose default is the behaviour
without the knob, and a kind that does not read the field grades under that
default.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_kinds.options import JudgeTrialOptions
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

    ``evaluate`` honours every field of ``options`` as :class:`LLMJudge` does
    — a kind that wraps another passes ``options`` on unchanged — and
    produces a :class:`JudgeResult`; a judge malfunction
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
        options: JudgeTrialOptions,
        kind_config: Mapping[str, Any] | None,
        logger: StructuredLogger,
    ) -> JudgeResult: ...
