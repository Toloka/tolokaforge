"""Tests for runner-side grade combination + judge config plumbing.

The legacy single-call ``evaluate_llm_judge`` (and its ``_parse_judge_json`` /
``_format_transcript_for_judge`` helpers) was deleted in Stage 4 of the
rubric-grading plan — the judge now runs runner-side on the shared
``ToolCallingLoop`` (see ``tests/unit/grading/test_judge.py`` for its behaviour).
What survives here are the *combine* helpers (which fold the judge component into
the final score) and the config wire round-trip.
"""

from __future__ import annotations

import pytest

from tolokaforge.runner.grading import (
    build_grade_reasons,
    combine_grade_components,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# combine_grade_components / build_grade_reasons — judge folded in
# ---------------------------------------------------------------------------


def test_combine_components_includes_llm_judge_when_score_set():
    components = {
        "hash_score": -1.0,
        "jsonpath_score": -1.0,
        "transcript_score": -1.0,
        "llm_judge_score": 0.6,
    }
    cfg = {
        "combine_method": "weighted",
        "weights": {"llm_judge": 1.0},
        "llm_judge": {"model_ref": "x"},
    }
    score, _ = combine_grade_components(components, cfg)
    assert score == pytest.approx(0.6)


def test_combine_excludes_llm_judge_when_score_is_sentinel():
    """An ERRORED judge leaves ``llm_judge_score`` at -1.0 — excluded, not 0.0."""
    components = {
        "hash_score": 0.8,
        "jsonpath_score": -1.0,
        "transcript_score": -1.0,
        "llm_judge_score": -1.0,
    }
    cfg = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0, "llm_judge": 1.0},
        "state_checks": {"hash_enabled": True},
    }
    score, _ = combine_grade_components(components, cfg)
    # Only state_checks is active; the errored judge does NOT drag the score to 0.
    assert score == pytest.approx(0.8)


def test_build_grade_reasons_includes_judge_text():
    components = {
        "hash_score": -1.0,
        "jsonpath_score": -1.0,
        "transcript_score": -1.0,
        "llm_judge_score": 0.92,
    }
    text = build_grade_reasons(components, judge_reasons="rubric satisfied")
    assert "Judge: score=0.92" in text
    assert "rubric satisfied" in text


# ---------------------------------------------------------------------------
# Config wire round-trip — structured rubric survives serialization
# ---------------------------------------------------------------------------


def test_native_adapter_serializes_llm_judge():
    """A structured rubric round-trips through the runner GradingConfig wire shape."""
    from tolokaforge.runner.models import GradingConfig, LLMJudgeConfig

    cfg = GradingConfig(
        combine_method="weighted",
        weights={"llm_judge": 1.0},
        llm_judge=LLMJudgeConfig(
            model_ref="openai/gpt-4o-mini",
            rubric={
                "reference": "Correct refund is $328.50.",
                "criteria": [
                    {
                        "id": "refund_amount",
                        "description": "Reply quotes the correct refund amount",
                        "expected": "$328.50",
                        "kind": "binary",
                        "required": True,
                        "weight": 1.0,
                    }
                ],
            },
        ),
    )
    payload = cfg.model_dump()
    assert payload["llm_judge"]["model_ref"] == "openai/gpt-4o-mini"
    assert payload["llm_judge"]["rubric"]["criteria"][0]["id"] == "refund_amount"

    reconstructed = GradingConfig.model_validate(payload)
    assert reconstructed.llm_judge is not None
    assert reconstructed.llm_judge.rubric.reference == "Correct refund is $328.50."
    crit = reconstructed.llm_judge.rubric.criteria[0]
    assert crit.id == "refund_amount"
    assert crit.required is True
    assert crit.expected == "$328.50"
