"""Unit tests for the ``critique`` tool's schema, evidence resolution, and execute().

Pins:
- ``build_critique_tool_schema``'s inner ``verdict_draft`` shape is
  byte-identical to ``build_submit_report_tool``'s ``parameters`` (the "same
  shape as submit_report" decision).
- a flat, ``submit_report``-shaped call (no ``verdict_draft`` wrapper) fails
  ``jsonschema`` validation against the critique schema — the structural
  rejection ``ToolExecutor`` relies on before ``CritiqueTool.execute`` ever runs.
- ``serialize_critique_context`` round-trips a mixed zero/non-zero-pointer
  ``CritiqueContext`` into valid, correctly-grouped JSON.
- ``CritiqueTool.execute`` resolves transcript, state-diff, and replayed
  ``search_kb`` evidence for a valid draft, rejects a flat/malformed draft,
  and never invents evidence for an unmatched criterion.
"""

import json

import pytest
from jsonschema import ValidationError, validate

from tolokaforge.core.grading.judge_kinds.critique import (
    CritiqueContext,
    CritiqueTool,
    EvidencePointer,
    EvidenceSource,
    JudgeDraft,
    serialize_critique_context,
)
from tolokaforge.core.grading.judge_tools import SearchKbTool
from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.grading.rubric import (
    CRITIQUE_TOOL_NAME,
    build_critique_tool_schema,
    build_submit_report_tool,
)
from tolokaforge.core.models import Message, MessageRole, ToolCall
from tolokaforge.runner.models import Criterion, CriterionResult, Rubric
from tolokaforge.tools.registry import ToolExecutionStatus

pytestmark = pytest.mark.unit


class _FakeKnowledgeSearch:
    """A real ``KnowledgeSearch``-conforming stub — no mock, a genuine object."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        return self._hits[:top_k]


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


# ===================================================================
# CritiqueTool.execute
# ===================================================================


def _search_kb_call_and_result(query: str, hits: list[SearchHit]) -> tuple[Message, Message]:
    """Build a real (call, result) message pair by actually running ``SearchKbTool``."""
    tool = SearchKbTool(_FakeKnowledgeSearch(hits))
    result = tool.execute(query=query)
    call = ToolCall(id="call-1", name="search_kb", arguments={"query": query})
    return (
        Message(role=MessageRole.ASSISTANT, tool_calls=[call]),
        Message(role=MessageRole.TOOL, tool_call_id=call.id, content=result.output),
    )


class TestCritiqueToolExecute:
    def test_matching_transcript_evidence_yields_pointers(self) -> None:
        tool = CritiqueTool(
            rubric=_mixed_rubric(),
            transcript=[{"role": "assistant", "content": "I refunded $328.50 to the customer."}],
            state_diff=None,
            messages=[],
        )

        result = tool.execute(verdict_draft=_valid_submit_args())

        assert result.success
        assert result.status == ToolExecutionStatus.SUCCESS
        decoded = json.loads(result.output)
        refund_pointers = decoded["refund_amount"]
        assert any(p["source"] == "transcript" and "328.50" in p["text"] for p in refund_pointers)

    def test_criterion_with_no_matching_text_gets_zero_pointers(self) -> None:
        tool = CritiqueTool(
            rubric=_mixed_rubric(),
            transcript=[
                {"role": "assistant", "content": "Unrelated small talk about the weather."}
            ],
            state_diff=None,
            messages=[],
        )

        result = tool.execute(verdict_draft=_valid_submit_args())

        decoded = json.loads(result.output)
        assert "tone" not in decoded
        assert "refund_amount" not in decoded

    def test_flat_non_wrapped_payload_fails(self) -> None:
        tool = CritiqueTool(rubric=_mixed_rubric(), transcript=[], state_diff=None, messages=[])

        result = tool.execute(**_valid_submit_args())

        assert not result.success
        assert result.status == ToolExecutionStatus.INVALID_ARGUMENTS
        assert "verdict_draft" in result.error

    def test_verdict_draft_missing_required_criterion_fails(self) -> None:
        tool = CritiqueTool(rubric=_mixed_rubric(), transcript=[], state_diff=None, messages=[])
        incomplete = _valid_submit_args()
        del incomplete["refund_amount"]

        result = tool.execute(verdict_draft=incomplete)

        assert not result.success
        assert result.status == ToolExecutionStatus.INVALID_ARGUMENTS

    def test_replayed_search_kb_hit_yields_kb_pointer_without_new_search(self) -> None:
        hit = SearchHit(
            doc_id="refund-policy",
            source="kb",
            score=0.9,
            text="Refunds must match the correct refund amount quoted to the customer.",
        )
        call_msg, result_msg = _search_kb_call_and_result("refund amount", [hit])

        tool = CritiqueTool(
            rubric=_mixed_rubric(),
            transcript=[],
            state_diff=None,
            messages=[call_msg, result_msg],
        )
        assert not hasattr(tool, "_kb")  # structural: no KnowledgeSearch dependency at all

        result = tool.execute(verdict_draft=_valid_submit_args())

        decoded = json.loads(result.output)
        kb_pointers = [p for p in decoded["refund_amount"] if p["source"] == "kb"]
        assert kb_pointers
        assert kb_pointers[0]["kb_doc_id"] == "refund-policy"
