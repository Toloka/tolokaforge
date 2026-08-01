"""Evaluating one trace constraint, for tests that assert about a single verdict.

``evaluate_trace_checks`` takes a whole block and returns a whole result, which is
the right shape for the component and the wrong one for a test asking what a single
constraint decided. This wraps the one-constraint case so the assertion reads
against the verdict rather than against the fold around it.
"""

from __future__ import annotations

from typing import Any

from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import TrialTimeline
from tolokaforge.core.models import TraceChecksConfig, TraceConstraintResult


def evaluate_constraint(
    timeline: TrialTimeline, require: dict[str, Any], **fields: Any
) -> TraceConstraintResult:
    """The verdict of the single constraint ``require`` states over ``timeline``.

    ``fields`` carries the constraint's own scoring fields — ``weight``,
    ``on_missing``, ``within`` — so a test varies one of them against a fixed
    condition.
    """
    config = TraceChecksConfig(
        constraints=[
            {"id": "constraint", "description": "the condition under test", "require": require}
            | fields
        ]
    )
    return evaluate_trace_checks(timeline, config).constraints[0]
