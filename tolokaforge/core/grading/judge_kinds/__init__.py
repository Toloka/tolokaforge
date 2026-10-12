"""``tolokaforge.judge_kinds`` — typed judge-kind package.

Every entry in ``[project.entry-points."tolokaforge.judge_kinds"]``
resolves to a class satisfying :class:`JudgeKind`, whose ``evaluate``
receives the trial's evidence and its :class:`JudgeTrialOptions`. Five kinds ship
under this group — three user-facing (:class:`SingleShotRubricJudgeKind`,
:class:`MultiTurnRubricJudgeKind`, :class:`AutoRubricJudgeKind`) plus
two internal building blocks (:class:`VotedRubricJudgeKind`,
:class:`AutoAnchoredRubricJudgeKind`) that the composite user-facing
kinds compose and that user code should not select via ``judge_kind:``
directly.

Downstream packages register alternative kinds (agentic, downstream-
specific) alongside the shipping reference impls without a framework PR
— see ``docs/GRADER_SERVICE.md`` § Extension points.

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
from tolokaforge.core.grading.judge_kinds.auto import AutoRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.auto_anchored import AutoAnchoredRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.multi_turn import MultiTurnRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.options import (
    JudgeTrialOptions,
    resolve_judge_trial_options,
)
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
    "DEFAULT_WRAPPED_KIND",
    "AutoAnchoredRubricJudgeKind",
    "AutoRubricJudgeKind",
    "JudgeKind",
    "JudgeTrialOptions",
    "MultiTurnRubricJudgeKind",
    "SingleShotRubricJudgeKind",
    "VotedRubricJudgeKind",
    "resolve_judge_trial_options",
]
