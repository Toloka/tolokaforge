"""``tolokaforge.judge_kinds`` — typed judge-kind package.

Every entry in ``[project.entry-points."tolokaforge.judge_kinds"]``
resolves to a class satisfying :class:`JudgeKind`. One built-in ships:
:class:`SingleShotRubricJudgeKind`, wrapping today's :class:`LLMJudge`
invocation byte-identically.

Downstream packages register alternative kinds (chunked, agentic, jury,
downstream-specific) alongside the shipping reference impl without a
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
from tolokaforge.core.grading.judge_kinds.single_shot import SingleShotRubricJudgeKind

__all__ = [
    "JudgeKind",
    "SingleShotRubricJudgeKind",
]
