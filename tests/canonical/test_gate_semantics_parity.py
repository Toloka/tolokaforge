"""The judge's required criterion and a trace check's ``severity: gate`` are one gate.

Two vocabularies express it and two implementations score it — ``aggregate_rubric``
over a rubric's criteria, ``evaluate_trace_checks`` over a block's constraints — and
they are deliberately not one shared helper. ``_weighted_fraction`` is held by an AST
lock to a single division and no branch, because every trace-constraint weight is
positive by construction; the judge's fold must *raise* on a non-positive denominator,
because ``Criterion.weight`` is unvalidated (#771). One function cannot satisfy both.

What holds the two together is the table below: one answer per cell, both
implementations driven against it, so a divergence names the cell instead of reaching
an author as a pack that grades differently depending on which component expressed
the gate. It also covers the two integrations a shared helper would not — the judge's
component is zeroed by the runner, the trace component by the fold itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.utils.doc_anchors import anchor, section
from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import build_timeline
from tolokaforge.core.grading.composite_fold import compose_trial_verdict
from tolokaforge.core.grading.rubric import (
    RubricAggregate,
    aggregate_rubric,
    parse_submit_report,
)
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.models import TraceChecksConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.canonical

_CONDITION = "the condition the author wrote"
_CALLED = "billing_api_get_payment"
_NEVER_CALLED = "servicenow_csm_update_case"
_MISREAD_CELL = "a vocabulary must answer the cell it was handed"
_HONEST_AGGREGATE = "a shut gate zeroes the component at the integration, not the judge's score"
_TIMELINE = build_timeline(
    turns=(("user", "Handle the refund."), ("assistant", "On it.")),
    recorded=[recorded_call(_CALLED)],
)


@dataclass(frozen=True)
class _Member:
    """One member of a decision set: a gate or a scored check, and whether it holds."""

    id: str
    gate: bool
    holds: bool


@dataclass(frozen=True)
class _Verdict:
    """What one vocabulary publishes as its component for a cell."""

    component: float
    gate_failed: bool


@dataclass(frozen=True)
class _Cell:
    """One row of the shared answer table, written once and answered twice.

    ``judge_aggregate`` is the honest weighted number *before* a shut gate zeroes
    anything. Only the judge publishes it — the runner owns the ``llm_judge``
    component, so ``RubricAggregate.score`` stays honest and the zeroing happens at
    the integration. The trace fold's counterpart is a route's own ``paths[i].score``,
    locked by ``tests/unit/grading/test_trace_checks_constraints.py``
    ``::test_a_gate_on_the_winning_route_fails_the_trial_rather_than_falling_back``.
    """

    name: str
    members: tuple[_Member, ...]
    component: float
    gate_failed: bool
    judge_aggregate: float


_CELLS = (
    _Cell(
        name="mixed, gate open",
        members=(
            _Member("g_the_record_was_read", gate=True, holds=True),
            _Member("s_the_case_was_denied", gate=False, holds=True),
            _Member("s_the_action_was_logged", gate=False, holds=True),
        ),
        component=1.0,
        gate_failed=False,
        judge_aggregate=1.0,
    ),
    _Cell(
        name="mixed, gate open, scored partial",
        members=(
            _Member("g_the_record_was_read", gate=True, holds=True),
            _Member("s_the_case_was_denied", gate=False, holds=True),
            _Member("s_the_action_was_logged", gate=False, holds=False),
        ),
        # A gate that holds contributes nothing: counted in the fold this cell reads
        # 2/3 on both sides, which is the double-counting severity exists to avoid.
        component=0.5,
        gate_failed=False,
        judge_aggregate=0.5,
    ),
    _Cell(
        name="mixed, gate shut",
        members=(
            _Member("g_the_record_was_read", gate=True, holds=False),
            _Member("s_the_case_was_denied", gate=False, holds=True),
            _Member("s_the_action_was_logged", gate=False, holds=True),
        ),
        component=0.0,
        gate_failed=True,
        judge_aggregate=1.0,
    ),
    _Cell(
        name="all gates, open",
        members=(
            _Member("g_the_record_was_read", gate=True, holds=True),
            _Member("g_no_order_was_mutated", gate=True, holds=True),
        ),
        component=1.0,
        gate_failed=False,
        judge_aggregate=1.0,
    ),
    _Cell(
        name="all gates, shut",
        members=(
            _Member("g_the_record_was_read", gate=True, holds=False),
            _Member("g_no_order_was_mutated", gate=True, holds=True),
        ),
        component=0.0,
        gate_failed=True,
        judge_aggregate=0.0,
    ),
)


def _holds(members: Sequence[_Member]) -> list[bool]:
    """The verdict pattern the cell declares, in declaration order."""
    return [member.holds for member in members]


def _judge_aggregate(members: Sequence[_Member]) -> RubricAggregate:
    """``members`` as a rubric, folded through the judge's own parse and aggregate.

    Every criterion is ``binary`` — the trace fold's members contribute a boolean
    ``passed`` while a ``graded`` criterion contributes a 0-1 gradient, so a graded
    table would have the partial cell disagreeing for a reason that has nothing to do
    with the gate, and the cell would get tuned until it agreed again. Driving the
    verdicts through :func:`parse_submit_report` rather than writing the scores here
    is what makes ``binary`` load-bearing: the ``{0.0, 1.0}`` derivation is
    production's, not the table's.
    """
    rubric = Rubric(
        criteria=[
            Criterion(id=member.id, description=_CONDITION, kind="binary", required=member.gate)
            for member in members
        ]
    )
    submitted: dict[str, object] = {}
    for member in members:
        submitted[member.id] = member.holds
        verdict = "MET" if member.holds else "NOT MET"
        submitted[f"{member.id}_justification"] = f"The evidence says so.\nVERDICT: {verdict}"

    results = parse_submit_report(submitted, rubric)
    assert [result.met for result in results] == _holds(members), _MISREAD_CELL
    return aggregate_rubric(rubric, results)


def _judge_component(aggregate: RubricAggregate) -> _Verdict:
    """The judge's aggregate as the runner publishes it.

    Driven through the runner's own composition rather than restating its zeroing, so this
    parity table cannot agree with a rule the runner has stopped applying.
    """
    verdict = compose_trial_verdict(
        {"llm_judge_score": aggregate.score},
        {"combine_method": "weighted", "weights": {"llm_judge": 1.0}, "llm_judge": {}},
        judge_gate_failed=aggregate.gate_failed,
        trace_gate_failed=False,
    )
    return _Verdict(component=verdict.judge_component, gate_failed=aggregate.gate_failed)


def _trace_component(members: Sequence[_Member]) -> _Verdict:
    """``members`` as a ``trace_checks`` block, scored over a timeline that made one call.

    A member holds by naming the call the trial made and fails by naming one it did
    not, so every verdict is the shipped evaluator's over a real timeline.
    """
    config = TraceChecksConfig(
        constraints=[
            {
                "id": member.id,
                "description": _CONDITION,
                "severity": "gate" if member.gate else "scored",
                "require": {
                    "present": {
                        "match": {
                            "kind": "tool_call",
                            "tool": {"equals": _CALLED if member.holds else _NEVER_CALLED},
                        }
                    }
                },
            }
            for member in members
        ]
    )

    result = evaluate_trace_checks(_TIMELINE, config)
    assert [item.passed for item in result.constraints] == _holds(members), _MISREAD_CELL
    return _Verdict(component=result.score, gate_failed=result.gate_failed)


@pytest.mark.parametrize("cell", _CELLS, ids=lambda cell: cell.name)
def test_the_judge_gate_and_the_trace_gate_answer_a_cell_alike(cell: _Cell):
    """One cell, two implementations, one answer — and the answer is pinned.

    The agreement assertion alone would pass on two implementations that drifted
    together, so the cell's own numbers are asserted beside it.
    """
    aggregate = _judge_aggregate(cell.members)
    judge = _judge_component(aggregate)
    trace = _trace_component(cell.members)

    assert judge == trace, "the judge's required gate and severity: gate answer differently"
    assert judge == _Verdict(component=cell.component, gate_failed=cell.gate_failed)
    assert aggregate.score == pytest.approx(cell.judge_aggregate), _HONEST_AGGREGATE


_GRADING_DOC = Path(__file__).resolve().parents[2] / "docs" / "GRADING.md"
_SEVERITY_HEADING = "### `severity` — a check that must hold"
_REQUIRED_GATE_HEADING = "### Required-gate semantics"


def _unpaired(heading: str) -> str:
    return f"{heading} must send the reader to the other gate section, by anchor"


def test_the_two_gate_vocabularies_are_a_documented_pair():
    """Each section sends the reader to the other, by an anchor that resolves.

    One behaviour spelled two ways is a trap unless an author reading either spelling
    is told about the other, which is why the semantics are reused and the spelling is
    not. A renamed heading breaks the link rather than the grep, so the anchors are
    resolved against the headings actually in the file.
    """
    lines = _GRADING_DOC.read_text(encoding="utf-8").splitlines()
    severity = section(lines, _SEVERITY_HEADING, _GRADING_DOC.name)
    required_gate = section(lines, _REQUIRED_GATE_HEADING, _GRADING_DOC.name)
    severity_link = f"(#{anchor(_SEVERITY_HEADING)})"
    required_gate_link = f"(#{anchor(_REQUIRED_GATE_HEADING)})"

    assert required_gate_link in severity, _unpaired(_SEVERITY_HEADING)
    assert severity_link in required_gate, _unpaired(_REQUIRED_GATE_HEADING)
