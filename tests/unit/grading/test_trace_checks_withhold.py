"""What ``on_missing: withhold`` decides, and what it refuses to decide.

The semantic: a constraint whose anchor matched nothing may declare
``on_missing: withhold`` to opt out of scoring entirely. Its weight enters
neither the numerator nor the denominator of the block's weighted average, and
a ``severity: gate`` withheld constraint does not shut the block. The verdict
rides on :attr:`TraceConstraintResult.withheld` so a reader classifies the
opt-out from the grade without matching on ``message`` prose.

Contagion under composites: a ``FALSE`` beside a ``WITHHELD`` still surfaces
the definite failure — an author writing ``all_of: [genuinely_failing,
withheld_thing]`` learns which branch failed. Every other input mix propagates
``WITHHELD`` through the ``all_of`` / ``any_of`` / ``negate`` combinators.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import Turn, build_timeline, build_turn_timeline
from tests.utils.trace_constraints import evaluate_constraint
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import TrialTimeline
from tolokaforge.core.models import (
    OnMissing,
    ToolExecutionStatus,
    TraceChecksConfig,
    TraceConstraintResult,
)

pytestmark = pytest.mark.unit

_KB = "search_kb"
_ASSISTANT = "assistant"
_USER = "user"


def _kb_timeline(status: ToolExecutionStatus) -> TrialTimeline:
    """One assistant turn that called ``search_kb`` and got back ``status``."""
    return build_timeline(
        turns=((_USER, "Answer using the knowledge base."), (_ASSISTANT, "Looking it up.")),
        recorded=[recorded_call(_KB, sequence=0, status=status)],
    )


def _kb_then_reply_timeline(status: ToolExecutionStatus) -> TrialTimeline:
    """Two assistant turns — the call, then a reply the ``before`` ordering can hold against.

    The first assistant turn hosts the ``search_kb`` call; the second is the
    author-visible answer. This is the fixture the "identity when the anchor
    matched" case needs: on a successful call, the call event lands ahead of
    the last assistant message, so the ordering resolves ``True`` and the
    withhold has nothing to trigger on.
    """
    return build_turn_timeline(
        [
            Turn(_USER, "Answer using the knowledge base."),
            Turn(
                _ASSISTANT,
                "Looking it up.",
                recorded=[recorded_call(_KB, sequence=0, status=status)],
            ),
            Turn(_ASSISTANT, "Here is the answer."),
        ]
    )


def _kb_before_reply(**fields: Any) -> dict[str, Any]:
    """A ``before`` constraint anchored on a successful ``search_kb`` before the reply.

    Mirrors the KB-search flaky-anchor pattern the ticket names — the whole
    reason this semantic exists. The left side reads ``status: {equals:
    success}``, which admits no event on an errored call, and the right side is
    the assistant turn that would have quoted the search result.
    """
    return {
        "id": "kb_before_reply",
        "description": "search_kb succeeded before the assistant answered",
        "require": {
            "before": {
                "left": {
                    "quantifier": "any",
                    "match": {
                        "kind": "tool_call",
                        "tool": {"equals": _KB},
                        "status": {"equals": "success"},
                    },
                },
                "right": {
                    "quantifier": "any",
                    "match": {"kind": "assistant_message"},
                },
            }
        },
        **fields,
    }


def _said_it() -> dict[str, Any]:
    """A scored constraint that holds against the fixture timeline."""
    return {
        "id": "the_assistant_replied",
        "description": "the assistant produced a reply",
        "weight": 10.0,
        "require": {"present": {"match": {"kind": "assistant_message"}}},
    }


def test_a_before_anchor_matched_nothing_and_the_author_opted_to_withhold_is_out_of_the_score():
    """The core semantic: withhold on an unmatched anchor lands as ``withheld=True``.

    The block's score is the weighted average of the scored sibling alone —
    the withheld constraint's weight enters neither the numerator nor the
    denominator, so removing it changes nothing an author reads.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    config = TraceChecksConfig(
        constraints=[_said_it(), _kb_before_reply(on_missing="withhold", weight=1.0)]
    )

    result = evaluate_trace_checks(timeline, config)
    withheld = next(item for item in result.constraints if item.id == "kb_before_reply")
    scored = next(item for item in result.constraints if item.id == "the_assistant_replied")

    assert withheld.passed is False
    assert withheld.undecided is False
    assert withheld.withheld is True
    assert "withheld" in withheld.message
    assert withheld.matched_positions == []
    assert scored.passed is True
    assert result.score == 1.0


def test_a_withheld_gate_does_not_shut_the_block():
    """A ``severity: gate`` withheld constraint scores its sibling alone.

    The 10.0-weight ``present`` sibling passes; the ``before`` gate's left
    anchor's ``status: success`` predicate matches nothing on an errored
    ``search_kb``; under ``on_missing: withhold`` the gate is out of the
    decision and the block resolves to 1.0.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    config = TraceChecksConfig(
        constraints=[
            _said_it(),
            _kb_before_reply(on_missing="withhold", severity="gate"),
        ]
    )

    result = evaluate_trace_checks(timeline, config)

    assert result.gate_failed is False
    assert result.failed_gate_ids == []
    assert result.score == 1.0
    assert result.passed is True


def test_a_withhold_declaration_does_not_change_behaviour_when_the_anchor_matched():
    """Opt-out is a no-op when the ordering was decidable.

    With ``search_kb`` returning success and a reply landing after the call,
    the left anchor matches and the ``before`` ordering resolves
    definitively. The withhold policy has no unmatched anchor to answer, so
    its verdict is identical to ``on_missing: fail`` — same ``passed``, same
    ``withheld=False``, same message, same block score.
    """
    timeline = _kb_then_reply_timeline(ToolExecutionStatus.SUCCESS)
    fail_config = TraceChecksConfig(constraints=[_kb_before_reply(on_missing="fail", weight=1.0)])
    withhold_config = TraceChecksConfig(
        constraints=[_kb_before_reply(on_missing="withhold", weight=1.0)]
    )

    fail_result = evaluate_trace_checks(timeline, fail_config)
    withhold_result = evaluate_trace_checks(timeline, withhold_config)

    assert fail_result.constraints[0].passed is True
    assert fail_result.constraints[0].withheld is False
    assert fail_result.constraints[0].message == withhold_result.constraints[0].message
    assert fail_result.constraints[0].passed == withhold_result.constraints[0].passed
    assert fail_result.constraints[0].withheld == withhold_result.constraints[0].withheld
    assert fail_result.score == withhold_result.score


_USER_BEFORE_ASSISTANT = {
    "before": {
        "left": {"quantifier": "any", "match": {"kind": "user_message"}},
        "right": {"quantifier": "any", "match": {"kind": "assistant_message"}},
    }
}
_ASSISTANT_BEFORE_USER = {
    "before": {
        "left": {"quantifier": "any", "match": {"kind": "assistant_message"}},
        "right": {"quantifier": "any", "match": {"kind": "user_message"}},
    }
}


def test_a_withheld_composite_branch_withholds_the_whole_all_of_when_no_branch_definitely_failed():
    """Composite contagion: WITHHELD spreads through ``all_of`` unless FALSE beats it.

    All three anchor-kind branches — the withheld one, a satisfied one, and a
    definitely-failing one — carry matched anchors (or, for the withheld
    branch, an unmatched anchor the author opted out of). Under ``all_of``,
    the ``on_missing: withhold`` threads down to every branch, so the
    withheld branch resolves to ``WITHHELD`` and the two ordering branches
    resolve to ``TRUE`` / ``FALSE`` on their own matched anchors. The
    conjunction rule: FALSE beats WITHHELD, otherwise WITHHELD wins.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    withheld_only = evaluate_constraint(
        timeline,
        {"all_of": [_kb_before_reply()["require"], _USER_BEFORE_ASSISTANT]},
        on_missing="withhold",
    )
    withheld_beside_failure = evaluate_constraint(
        timeline,
        {"all_of": [_kb_before_reply()["require"], _ASSISTANT_BEFORE_USER]},
        on_missing="withhold",
    )

    assert withheld_only.withheld is True
    assert withheld_only.passed is False
    assert withheld_only.undecided is False
    assert withheld_beside_failure.withheld is False
    assert withheld_beside_failure.passed is False
    assert withheld_beside_failure.undecided is False


def test_a_withheld_branch_withholds_an_any_of_unless_a_definite_true_beats_it():
    """Composite contagion under ``any_of``: WITHHELD is inherited unless a TRUE branch decides.

    ``any_of: [withheld, satisfied]`` — the satisfied branch is a definite
    disjunct that decides the composite ``TRUE``; the disjunction rule (TRUE
    beats WITHHELD) surfaces the pass. ``any_of: [withheld, failing]`` — no
    definite ``TRUE`` is present, so the withhold survives over the failing
    branch and the whole composite is withheld.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    withheld_beside_true = evaluate_constraint(
        timeline,
        {"any_of": [_kb_before_reply()["require"], _USER_BEFORE_ASSISTANT]},
        on_missing="withhold",
    )
    withheld_beside_false = evaluate_constraint(
        timeline,
        {"any_of": [_kb_before_reply()["require"], _ASSISTANT_BEFORE_USER]},
        on_missing="withhold",
    )

    assert withheld_beside_true.passed is True
    assert withheld_beside_true.withheld is False
    assert withheld_beside_true.undecided is False
    assert withheld_beside_false.withheld is True
    assert withheld_beside_false.passed is False
    assert withheld_beside_false.undecided is False


def test_a_withheld_branch_under_negate_is_still_withheld():
    """``_NEGATED[WITHHELD] = WITHHELD``: the fixed point under negation.

    A ``negate`` inverts ``TRUE`` and ``FALSE`` but leaves ``WITHHELD`` (and
    ``UNKNOWN``) alone. An author who wrote ``negate: withheld_thing`` is
    still opting the whole constraint out of scoring — the negation cannot
    surface a verdict the underlying branch withheld.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    negated = evaluate_constraint(
        timeline,
        {"negate": _kb_before_reply()["require"]},
        on_missing="withhold",
    )

    assert negated.withheld is True
    assert negated.passed is False
    assert negated.undecided is False


def test_withhold_and_passed_and_withhold_and_undecided_are_pair_the_model_refuses():
    """The two model-validator refusals on ``TraceConstraintResult.withheld``.

    A withheld constraint is neither a pass nor undecided: it is a third
    ``passed=False`` axis for an author-opted opt-out, and confusing the three
    would surface a verdict that has lost which of the three it is.
    """
    with pytest.raises(ValidationError, match="passed and is withheld at once"):
        TraceConstraintResult(
            id="kb_before_reply",
            kind="before",
            passed=True,
            weight=1.0,
            withheld=True,
        )
    with pytest.raises(ValidationError, match="withheld and undecided at once"):
        TraceConstraintResult(
            id="kb_before_reply",
            kind="before",
            passed=False,
            weight=1.0,
            undecided=True,
            withheld=True,
        )


def test_an_alternatives_route_with_a_withheld_gate_beside_a_scored_sibling_beats_a_clean_route_on_precedence():
    """A withheld gate is out of the decision, so the route it sits on scores its scored siblings alone.

    Route A carries two scored constraints: one holds, one fails — weighted
    fraction ``0.5``, no gates. Route B carries one scored constraint that
    holds and one ``severity: gate`` withheld constraint — the withheld gate
    is out of both ``scored`` and ``failed_gate_ids``, so the weighted
    fraction is ``1.0/1.0 = 1.0`` and no gate shuts. ``_precedence`` orders
    routes by ``(route.score, bool(route.failed_gate_ids))``, so both routes'
    tuples read as ``(score, False)`` — no gate shut on either. What this
    locks: the withheld gate keeps Route B's tuple at ``(1.0, False)`` rather
    than promoting it to ``(1.0, True)`` (where the withheld gate would count
    as failed), and the score axis picks Route B. A future refactor that let
    a withheld gate slip into ``failed_gate_ids`` would fail this assertion
    on ``route_b``'s ``gate_failed``.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    config = TraceChecksConfig(
        alternatives=[
            {
                "id": "route_a",
                "description": "one pass and one fail",
                "constraints": [
                    {
                        "id": "a_pass",
                        "description": "the assistant replied",
                        "weight": 1.0,
                        "require": {"present": {"match": {"kind": "assistant_message"}}},
                    },
                    {
                        "id": "a_fail",
                        "description": "an unrelated tool was called",
                        "weight": 1.0,
                        "require": {
                            "present": {"match": {"kind": "tool_call", "tool": {"equals": "never"}}}
                        },
                    },
                ],
            },
            {
                "id": "route_b",
                "description": "one pass beside a withheld gate",
                "constraints": [
                    {
                        "id": "b_pass",
                        "description": "the assistant replied",
                        "weight": 1.0,
                        "require": {"present": {"match": {"kind": "assistant_message"}}},
                    },
                    _kb_before_reply(on_missing="withhold", severity="gate"),
                ],
            },
        ]
    )

    result = evaluate_trace_checks(timeline, config)
    scores = {item.id: item.score for item in result.paths}
    gate_failed = {item.id: item.gate_failed for item in result.paths}

    assert scores == {"route_a": 0.5, "route_b": 1.0}
    assert gate_failed == {"route_a": False, "route_b": False}
    assert result.winning_path == "route_b"
    assert result.score == 1.0
    assert result.failed_gate_ids == []


def _kb_present(**fields: Any) -> dict[str, Any]:
    """A ``present`` constraint anchored on a successful ``search_kb`` call.

    Same matcher shape as the KB-search flaky-anchor pattern, expressed as a
    ``present`` check rather than an ``ordering`` one: the author asserts the
    call succeeded at least once, and opts to withhold when the tool errored
    at runtime so its errors do not fail the block.
    """
    return {
        "id": "kb_succeeded",
        "description": "search_kb was called and succeeded",
        "require": {
            "present": {
                "match": {
                    "kind": "tool_call",
                    "tool": {"equals": _KB},
                    "status": {"equals": "success"},
                }
            }
        },
        **fields,
    }


def _kb_count(**fields: Any) -> dict[str, Any]:
    """A ``count`` constraint bounding successful ``search_kb`` calls at 1..1."""
    return {
        "id": "kb_called_exactly_once",
        "description": "search_kb was called exactly once and succeeded",
        "require": {
            "count": {
                "match": {
                    "kind": "tool_call",
                    "tool": {"equals": _KB},
                    "status": {"equals": "success"},
                },
                "min": 1,
                "max": 1,
            }
        },
        **fields,
    }


def test_a_present_that_matched_nothing_is_withheld_when_the_author_opted_out():
    """The withhold verdict on an empty ``present`` match.

    A ``present`` constraint anchored on ``search_kb`` with ``status:
    success`` matches nothing on an errored call. Under ``on_missing:
    withhold``, the empty match yields ``withheld=True`` and the block
    scores the scored sibling alone.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    config = TraceChecksConfig(
        constraints=[_said_it(), _kb_present(on_missing="withhold", weight=1.0)]
    )

    result = evaluate_trace_checks(timeline, config)
    withheld = next(item for item in result.constraints if item.id == "kb_succeeded")

    assert withheld.passed is False
    assert withheld.undecided is False
    assert withheld.withheld is True
    assert "withheld" in withheld.message
    assert withheld.matched_positions == []
    assert result.score == 1.0


def test_a_count_whose_matcher_yielded_nothing_is_withheld_when_the_author_opted_out():
    """The withhold verdict on an empty ``count`` match.

    ``count.match`` selects no event and the author opted to withhold. The
    ``min`` / ``max`` bounds do not enter the decision — the withhold is on
    the absence of the anchor, not on whether the count falls in bounds.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    config = TraceChecksConfig(
        constraints=[_said_it(), _kb_count(on_missing="withhold", weight=1.0)]
    )

    result = evaluate_trace_checks(timeline, config)
    withheld = next(item for item in result.constraints if item.id == "kb_called_exactly_once")

    assert withheld.passed is False
    assert withheld.undecided is False
    assert withheld.withheld is True
    assert "withheld" in withheld.message
    assert withheld.matched_positions == []
    assert result.score == 1.0


def test_on_missing_withhold_on_absent_is_refused_at_load():
    """``absent``'s empty match IS its positive verdict — withholding it withholds the check.

    The load tier keeps refusing ``on_missing: withhold`` where the require
    tree carries an ``absent`` kind, at every nesting depth. The message
    names the kind and points at the positive-empty semantic.
    """
    with pytest.raises(ValidationError, match="absent"):
        TraceChecksConfig(
            constraints=[
                {
                    "id": "no_leak",
                    "description": "no audit_log_write occurred",
                    "on_missing": "withhold",
                    "require": {
                        "absent": {"match": {"kind": "tool_call", "tool": {"equals": "audit"}}}
                    },
                }
            ]
        )
    with pytest.raises(ValidationError, match="absent"):
        TraceChecksConfig(
            constraints=[
                {
                    "id": "no_leak_nested",
                    "description": "no audit_log_write occurred, wrapped in a composite",
                    "on_missing": "withhold",
                    "require": {
                        "all_of": [
                            {
                                "absent": {
                                    "match": {"kind": "tool_call", "tool": {"equals": "audit"}}
                                }
                            }
                        ]
                    },
                }
            ]
        )


def test_on_missing_pass_on_present_stays_refused_at_load():
    """``on_missing: pass`` on a ``present`` is refused at load.

    Unmatched would pass by the policy and matched by the constraint — the
    pair cannot fail — so the load error is what stops a declaration that
    cannot fail from being written.
    """
    with pytest.raises(ValidationError, match="on_missing has nothing to decide"):
        TraceChecksConfig(
            constraints=[
                {
                    "id": "the_call_happened",
                    "description": "search_kb was called",
                    "on_missing": "pass",
                    "require": {
                        "present": {"match": {"kind": "tool_call", "tool": {"equals": _KB}}}
                    },
                }
            ]
        )


def test_the_reproduction_snippet_produces_withheld_true_and_block_score_one():
    """The KB-search flaky-anchor scenario resolves to ``withheld=True`` + ``block_score=1.0``.

    Under ``on_missing: withhold`` on the gate anchoring on ``search_kb``
    ``status: success``, the gate is out of the decision, the 10.0-weight
    sibling holds, and the block scores 1.0.
    """
    timeline = _kb_timeline(ToolExecutionStatus.ERROR)
    config = TraceChecksConfig(
        constraints=[
            _said_it(),
            _kb_before_reply(on_missing="withhold", severity="gate"),
        ]
    )

    result = evaluate_trace_checks(timeline, config)
    withheld_gate = next(item for item in result.constraints if item.id == "kb_before_reply")

    assert withheld_gate.passed is False
    assert withheld_gate.withheld is True
    assert result.gate_failed is False
    assert result.score == 1.0
    assert OnMissing.WITHHOLD.value == "withhold"
