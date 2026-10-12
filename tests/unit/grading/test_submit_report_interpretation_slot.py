"""Unit tests for the submit_report `<id>_interpretation` schema slot.

Locks the M51 Layer 1 contract:

- Emitted iff `kind: graded` AND `expected is None`.
- Ordered BEFORE `<id>_justification` in `properties` insertion order, so a
  provider that fills tool arguments in schema order commits to an
  interpretation before writing the reasoning that leads to a score.
- Absent for binary criteria and for anchored graded criteria.
- Deliberately NOT in the `required` list — legacy cassettes and weaker
  models that omit it degrade to today's behaviour instead of failing whole
  trials on a shape they were never authored against.
- Parser accepts the field when present and folds its text into the
  audit-trail justification without changing the ``CriterionResult`` wire
  schema; parser tolerates its absence.
- Parser fails loud when the interpretation is present but the wrong type.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.rubric import (
    SubmitReportValidationError,
    build_submit_report_tool,
    parse_submit_report,
)
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


def _rubric_with_three_shapes() -> Rubric:
    """One binary + one anchored graded + one unanchored graded criterion."""
    return Rubric(
        criteria=[
            Criterion(id="mentions_id", description="Mentions the id.", kind="binary"),
            Criterion(
                id="matches_expected_shape",
                description="Matches the expected output shape.",
                kind="graded",
                expected="A single JSON object with keys id, name, and status.",
            ),
            Criterion(
                id="clarity",
                description="Reads clearly for an on-call teammate.",
                kind="graded",
            ),
        ]
    )


def test_interpretation_slot_only_emitted_for_unanchored_graded() -> None:
    schema = build_submit_report_tool(_rubric_with_three_shapes())
    properties = schema["function"]["parameters"]["properties"]
    assert "mentions_id_interpretation" not in properties
    assert "matches_expected_shape_interpretation" not in properties
    assert "clarity_interpretation" in properties


def test_interpretation_slot_ordered_before_justification_and_verdict() -> None:
    schema = build_submit_report_tool(_rubric_with_three_shapes())
    properties = schema["function"]["parameters"]["properties"]
    keys = list(properties)
    interp_idx = keys.index("clarity_interpretation")
    justif_idx = keys.index("clarity_justification")
    verdict_idx = keys.index("clarity")
    assert interp_idx < justif_idx < verdict_idx


def test_interpretation_slot_is_not_required() -> None:
    """Optional in the schema so legacy cassettes / weaker models degrade cleanly."""
    schema = build_submit_report_tool(_rubric_with_three_shapes())
    required = schema["function"]["parameters"]["required"]
    assert "clarity_interpretation" not in required
    # The justification and verdict are still required, and their ordering is
    # unaffected (justification before verdict).
    assert required.index("clarity_justification") < required.index("clarity")


def test_parse_folds_interpretation_into_justification_audit_trail() -> None:
    rubric = _rubric_with_three_shapes()
    tool_args = {
        "mentions_id_justification": "The reply names the id. VERDICT: MET",
        "mentions_id": True,
        "matches_expected_shape_justification": (
            "Response is a JSON object with the three named keys. SCORE: 1.0"
        ),
        "matches_expected_shape": 1.0,
        "clarity_interpretation": (
            "A clear reply is written in complete sentences and names each fact once."
        ),
        "clarity_justification": ("The reply is a single paragraph with no repetition. SCORE: 0.8"),
        "clarity": 0.8,
        "reasons": "All three checks pass.",
    }
    results = parse_submit_report(tool_args, rubric)
    clarity = next(r for r in results if r.id == "clarity")
    assert clarity.justification.startswith("Interpretation: A clear reply is written")
    assert "SCORE: 0.8" in clarity.justification
    # Non-interpretation criteria untouched.
    mentions = next(r for r in results if r.id == "mentions_id")
    assert not mentions.justification.startswith("Interpretation:")


def test_parse_tolerates_absent_interpretation_and_falls_back_to_justification() -> None:
    """A judge that skips the optional interpretation slot degrades cleanly."""
    rubric = _rubric_with_three_shapes()
    tool_args = {
        "mentions_id_justification": "ok. VERDICT: MET",
        "mentions_id": True,
        "matches_expected_shape_justification": "ok. SCORE: 1.0",
        "matches_expected_shape": 1.0,
        # clarity_interpretation intentionally omitted — parser must not raise.
        "clarity_justification": "clear. SCORE: 0.8",
        "clarity": 0.8,
        "reasons": "ok",
    }
    results = parse_submit_report(tool_args, rubric)
    clarity = next(r for r in results if r.id == "clarity")
    assert clarity.justification == "clear. SCORE: 0.8"
    assert not clarity.justification.startswith("Interpretation:")


def test_parse_fails_loud_when_interpretation_wrong_type() -> None:
    rubric = _rubric_with_three_shapes()
    tool_args = {
        "mentions_id_justification": "ok. VERDICT: MET",
        "mentions_id": True,
        "matches_expected_shape_justification": "ok. SCORE: 1.0",
        "matches_expected_shape": 1.0,
        "clarity_interpretation": 42,  # not a string
        "clarity_justification": "clear. SCORE: 0.8",
        "clarity": 0.8,
        "reasons": "ok",
    }
    with pytest.raises(SubmitReportValidationError) as exc_info:
        parse_submit_report(tool_args, rubric)
    message = str(exc_info.value)
    assert "clarity" in message
    assert "interpretation" in message
    assert "string" in message


def test_rubric_construction_refuses_interpretation_suffix_id() -> None:
    """A criterion id ending in `_interpretation` is refused at construction."""
    with pytest.raises(ValueError) as exc_info:
        Rubric(
            criteria=[
                Criterion(id="ok_interpretation", description="x", kind="binary"),
            ]
        )
    assert "_interpretation" in str(exc_info.value)


def test_rubric_construction_refuses_id_colliding_with_interpretation_key() -> None:
    """An id that collides with another criterion's derived `<id>_interpretation`."""
    with pytest.raises(ValueError) as exc_info:
        Rubric(
            criteria=[
                Criterion(id="clarity", description="x", kind="graded"),
                Criterion(id="clarity_interpretation", description="y", kind="binary"),
            ]
        )
    # The first-hit check is the suffix rule; the collision rule fires when
    # neither id ends in the suffix but one derives to the other. This test
    # accepts either path.
    assert "clarity_interpretation" in str(exc_info.value)
