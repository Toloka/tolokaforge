"""``tolokaforge.judge_kinds`` — typed judge-kind package.

Every entry in ``[project.entry-points."tolokaforge.judge_kinds"]``
resolves to a class satisfying :class:`JudgeKind`. Six built-ins ship:
:class:`SingleShotRubricJudgeKind` (wraps today's :class:`LLMJudge`
invocation byte-identically), :class:`ChunkedRubricJudgeKind` (one
:class:`LLMJudge` invocation per chunk of the rubric's criteria,
removing the truncation failure class on 30+ criterion rubrics),
:class:`VotedRubricJudgeKind` (wraps any registered kind and samples it
K times, folding the per-criterion verdicts through a robust aggregator
to reduce judge-model self-variance), :class:`JuryRubricJudgeKind`
(wraps any registered kind and dispatches to a cross-family panel of N
different judge models, folding the per-criterion verdicts through the
same robust-aggregator module ``voted_rubric`` uses),
:class:`PerCriterionRubricJudgeKind` (specialisation of
:class:`ChunkedRubricJudgeKind` with ``chunk_size=1`` — one
:class:`LLMJudge` call per criterion, the strictest isolation), and
:class:`AutoAnchoredRubricJudgeKind` (wraps any registered kind; runs
one cached warm-up judge call per unique rubric to auto-generate
``expected:`` anchors for graded criteria the author left unanchored,
then delegates to the wrapped kind with the synthetic anchored rubric).

Downstream packages register alternative kinds (agentic, downstream-specific)
alongside the shipping reference impls without a framework PR — see
``docs/GRADER_SERVICE.md`` § Extension points.

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
from tolokaforge.core.grading.judge_kinds.auto_anchored import AutoAnchoredRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.chunked import ChunkedRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.jury import DEFAULT_PANEL, JuryRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.per_criterion import PerCriterionRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.single_shot import SingleShotRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.voted import (
    DEFAULT_AGGREGATOR,
    DEFAULT_N_SAMPLES,
    DEFAULT_WRAPPED_KIND,
    VotedRubricJudgeKind,
)

__all__ = [
    "DEFAULT_AGGREGATOR",
    "DEFAULT_N_SAMPLES",
    "DEFAULT_PANEL",
    "DEFAULT_WRAPPED_KIND",
    "AutoAnchoredRubricJudgeKind",
    "ChunkedRubricJudgeKind",
    "JudgeKind",
    "JuryRubricJudgeKind",
    "PerCriterionRubricJudgeKind",
    "SingleShotRubricJudgeKind",
    "VotedRubricJudgeKind",
]
