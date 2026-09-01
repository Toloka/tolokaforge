"""Topology-neutral composite grading dispatch.

Every deployment shape (aggregate image, independent grader container,
future trajectory-storage callback, future snapshot, future shared-mount)
runs one composite grade against a :class:`GradingSubstrate`. This package
carries the per-concern helpers so they can be reused verbatim by every
topology.

Sub-component seams. Every sub-component evaluator reaches the composite
through its Protocol via a resolved instance passed as a kwarg — never a
direct import of the reference impl. The six shipped seams
(:class:`~tolokaforge.core.grading.check_runner.CheckExecutor`,
:class:`~tolokaforge.core.grading.judge_model_provider.JudgeModelProvider`,
:class:`~tolokaforge.core.grading.rubric_evaluator.RubricEvaluator`,
:class:`~tolokaforge.core.grading.transcript_rule_matcher.TranscriptRuleMatcher`,
:class:`~tolokaforge.core.grading.trace_check_operator.TraceCheckOperator`,
:class:`~tolokaforge.core.grading.state_check_backend.StateCheckBackend`)
resolve through :mod:`tolokaforge.core.plugin_registry` at the runner
boundary and are threaded here as constructor args. The ``.importlinter``
``composite-sub-component-seams`` contract enforces the negative-space of
this rule by forbidding module-level imports of the six reference-impl
modules from any module in this package.

See ``docs/GRADING.md`` § "Composite dispatch" for the substrate-neutral
role of the five composite functions and ``docs/GRADER_SERVICE.md`` §
"Sub-component plug-in seams" for the resolved-instance seams the
composite reaches through kwargs.
"""

from tolokaforge.core.grading.composite.custom_checks import (
    _build_runner_check_transcript,
    _check_result_to_wire,
    _executor_error_to_wire,
    grade_custom_checks,
)
from tolokaforge.core.grading.composite.llm_judge import (
    build_judge_state_diff,
    grade_llm_judge,
)
from tolokaforge.core.grading.composite.state_checks import (
    StateChecksReadResult,
    grade_state_checks_reads,
)
from tolokaforge.core.grading.composite.trace_checks import grade_trace_checks
from tolokaforge.core.grading.composite.transcript_rules import grade_transcript_rules

__all__ = [
    "StateChecksReadResult",
    "_build_runner_check_transcript",
    "_check_result_to_wire",
    "_executor_error_to_wire",
    "build_judge_state_diff",
    "grade_custom_checks",
    "grade_llm_judge",
    "grade_state_checks_reads",
    "grade_trace_checks",
    "grade_transcript_rules",
]
