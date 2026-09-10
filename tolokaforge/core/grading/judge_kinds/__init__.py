"""``tolokaforge.judge_kinds`` — typed judge-kind package.

Every entry in ``[project.entry-points."tolokaforge.judge_kinds"]``
resolves to a class satisfying :class:`JudgeKind`. Two built-ins ship:
:class:`SingleShotRubricJudgeKind` (wraps today's :class:`LLMJudge`
invocation byte-identically) and :class:`ChunkedRubricJudgeKind` (one
:class:`LLMJudge` invocation per fixed-K chunk of the rubric's criteria,
removing the truncation failure class on 30+ criterion rubrics).

Downstream packages register alternative kinds (agentic, jury,
downstream-specific) alongside the shipping reference impls without a
framework PR — see ``docs/GRADER_SERVICE.md`` § Extension points.

The κ-parity surface (``ParityCorpusEntry``, ``ParityGateThresholds``,
``PerCriterionVerdict``, ``ParityGateDecision``,
``measure_cross_kind_agreement``, ``measure_self_consistency``,
``decide_parity_gate``) lives in
:mod:`tolokaforge.core.grading.judge_kinds.parity` and is imported from
there directly by test-time callers — it depends on
:mod:`tolokaforge.core.grading.agreement`, which is orchestrator-only
and not part of the runner subset.
"""

from tolokaforge.core.grading.judge_kinds._protocol import JudgeKind
from tolokaforge.core.grading.judge_kinds.chunked import (
    DEFAULT_CHUNK_SIZE,
    ChunkedRubricJudgeKind,
)
from tolokaforge.core.grading.judge_kinds.single_shot import SingleShotRubricJudgeKind

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "ChunkedRubricJudgeKind",
    "JudgeKind",
    "SingleShotRubricJudgeKind",
]
