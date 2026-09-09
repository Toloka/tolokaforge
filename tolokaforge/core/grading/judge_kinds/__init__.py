"""``tolokaforge.judge_kinds`` — typed judge-kind package.

Every entry in ``[project.entry-points."tolokaforge.judge_kinds"]``
resolves to a class satisfying :class:`JudgeKind`. One built-in ships:
:class:`SingleShotRubricJudgeKind`, wrapping today's :class:`LLMJudge`
invocation byte-identically.

Downstream packages register alternative kinds (chunked, agentic, jury,
downstream-specific) alongside the shipping reference impl without a
framework PR — see ``docs/GRADER_SERVICE.md`` § Extension points.
"""

from tolokaforge.core.grading.judge_kinds._protocol import JudgeKind
from tolokaforge.core.grading.judge_kinds.single_shot import SingleShotRubricJudgeKind

__all__ = [
    "JudgeKind",
    "SingleShotRubricJudgeKind",
]
