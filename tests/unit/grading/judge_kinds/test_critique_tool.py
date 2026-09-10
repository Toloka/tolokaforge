"""Unit tests for the ``critique`` tool's schema and evidence dataclasses.

Pins:
- ``build_critique_tool_schema``'s inner ``verdict_draft`` shape is
  byte-identical to ``build_submit_report_tool``'s ``parameters`` (the "same
  shape as submit_report" decision).
- a flat, ``submit_report``-shaped call (no ``verdict_draft`` wrapper) fails
  ``jsonschema`` validation against the critique schema — the structural
  rejection ``ToolExecutor`` relies on before ``CritiqueTool.execute`` ever runs.
- ``serialize_critique_context`` round-trips a mixed zero/non-zero-pointer
  ``CritiqueContext`` into valid, correctly-grouped JSON.
"""

import json

import pytest
from jsonschema import ValidationError, validate

from tolokaforge.core.grading.judge_kinds.critique import (
    CritiqueContext,
    EvidencePointer,
    EvidenceSource,
    JudgeDraft,
    serialize_critique_context,
)
from tolokaforge.core.grading.rubric import (
    CRITIQUE_TOOL_NAME,
    build_critique_tool_schema,
    build_submit_report_tool,
)
from tolokaforge.runner.models import Criterion, CriterionResult, Rubric

pytestmark = pytest.mark.unit


def _mixed_rubric() -> Rubric:
    return Rubric(
        criteria=[
            Criterion(
                id="refund_amount",
                description="Reply quotes the correct refund amount",
                expected="$328.50",
                kind="binary",
                required=True,
            ),
            Criterion(id="tone", description="Reply is polite and professional", kind="graded"),
        ]
    )


def _valid_submit_args() -> dict:
    return {
        "refund_amount": True,
        "refund_amount_justification": "Quoted $328.50.\nVERDICT: MET",
        "tone": 0.9,
        "tone_justification": "Courteous throughout.\nSCORE: 0.9",
        "reasons": "Overall good.",
    }


# ===================================================================
# build_critique_tool_schema
# ===================================================================


class TestBuildCritiqueToolSchema:
    def test_wraps_submit_report_shape_under_verdict_draft(self) -> None:
        rubric = _mixed_rubric()
        critique_schema = build_critique_tool_schema(rubric)
        submit_params = build_submit_report_tool(rubric)["function"]["parameters"]

        assert critique_schema["type"] == "function"
        assert critique_schema["function"]["name"] == CRITIQUE_TOOL_NAME
        params = critique_schema["function"]["parameters"]
        assert params["required"] == ["verdict_draft"]
        assert params["properties"]["verdict_draft"] == submit_params

    def test_flat_submit_report_shaped_call_fails_validation(self) -> None:
        rubric = _mixed_rubric()
        schema = build_critique_tool_schema(rubric)["function"]["parameters"]

        with pytest.raises(ValidationError):
            validate(instance=_valid_submit_args(), schema=schema)

    def test_wrapped_call_passes_validation(self) -> None:
        rubric = _mixed_rubric()
        schema = build_critique_tool_schema(rubric)["function"]["parameters"]

        validate(instance={"verdict_draft": _valid_submit_args()}, schema=schema)


# ===================================================================
# Dataclasses
# ===================================================================


class TestDataclasses:
    def test_judge_draft_holds_criterion_results(self) -> None:
        result = CriterionResult(id="tone", met=True, score=0.9, justification="ok")
        draft = JudgeDraft(verdicts=(result,))
        assert draft.verdicts == (result,)

    def test_evidence_pointer_kb_doc_id_defaults_to_none(self) -> None:
        pointer = EvidencePointer(
            criterion_id="tone", source=EvidenceSource.TRANSCRIPT, text="agent: refund issued"
        )
        assert pointer.kb_doc_id is None

    def test_dataclasses_are_frozen(self) -> None:
        pointer = EvidencePointer(
            criterion_id="tone", source=EvidenceSource.TRANSCRIPT, text="agent: refund issued"
        )
        with pytest.raises(AttributeError):
            pointer.text = "mutated"  # type: ignore[misc]


# ===================================================================
# serialize_critique_context
# ===================================================================


class TestSerializeCritiqueContext:
    def test_round_trips_mixed_zero_and_nonzero_pointer_criteria(self) -> None:
        ctx = CritiqueContext(
            pointers=(
                EvidencePointer(
                    criterion_id="refund_amount",
                    source=EvidenceSource.TRANSCRIPT,
                    text="agent: refunded $328.50",
                ),
                EvidencePointer(
                    criterion_id="refund_amount",
                    source=EvidenceSource.KB,
                    text="policy: refunds within 30 days",
                    kb_doc_id="doc-42",
                ),
            )
        )

        decoded = json.loads(serialize_critique_context(ctx))

        assert decoded == {
            "refund_amount": [
                {"source": "transcript", "text": "agent: refunded $328.50", "kb_doc_id": None},
                {
                    "source": "kb",
                    "text": "policy: refunds within 30 days",
                    "kb_doc_id": "doc-42",
                },
            ]
        }
        # "tone" got no evidence: it is absent entirely, never a placeholder entry.
        assert "tone" not in decoded

    def test_empty_context_serializes_to_empty_object(self) -> None:
        assert json.loads(serialize_critique_context(CritiqueContext(pointers=()))) == {}
