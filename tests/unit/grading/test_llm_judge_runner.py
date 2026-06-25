"""Tests for the runner-side LLM judge evaluation.

Mirrors the OSS Part B contract: evaluate_llm_judge() returns
``(score, reasons)`` resolved by an LLM call via litellm. Failures
return ``(0.0, error)`` so the score still influences the weighted
grade — sentinels (-1.0) only mean "not configured".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.runner.grading import (
    _format_transcript_for_judge,
    _parse_judge_json,
    build_grade_reasons,
    combine_grade_components,
    evaluate_llm_judge,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _parse_judge_json
# ---------------------------------------------------------------------------


def test_parse_judge_json_direct():
    parsed = _parse_judge_json('{"score": 0.7, "reasons": "ok"}')
    assert parsed == {"score": 0.7, "reasons": "ok"}


def test_parse_judge_json_fenced_block():
    text = '```json\n{"score": 0.5, "reasons": "fenced"}\n```'
    parsed = _parse_judge_json(text)
    assert parsed["score"] == 0.5


def test_parse_judge_json_inline_object_with_score_key():
    text = 'preamble blah {"score": 0.25, "reasons": "inline"} trailer'
    parsed = _parse_judge_json(text)
    assert parsed["score"] == 0.25


def test_parse_judge_json_invalid_raises():
    with pytest.raises(ValueError):
        _parse_judge_json("nothing parseable here")


# ---------------------------------------------------------------------------
# _format_transcript_for_judge
# ---------------------------------------------------------------------------


def test_format_transcript_includes_role_and_content():
    text = _format_transcript_for_judge(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    )
    assert "[user]: hi" in text
    assert "[assistant]: hello" in text


def test_format_transcript_includes_tool_calls():
    text = _format_transcript_for_judge(
        [
            {
                "role": "assistant",
                "content": "calling now",
                "tool_calls": [{"name": "search", "arguments": {"q": "abc"}}],
            }
        ]
    )
    assert "Tool call: search" in text
    assert "abc" in text


def test_format_transcript_truncates_tool_results():
    big_payload = "x" * 5000
    text = _format_transcript_for_judge(
        [{"role": "tool", "tool_call_id": "t-1", "content": big_payload}]
    )
    # Truncated to 2000 chars + "..."
    assert "..." in text
    assert len(text) < 2500


# ---------------------------------------------------------------------------
# evaluate_llm_judge — mocked litellm path
# ---------------------------------------------------------------------------


def _mock_completion_response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_evaluate_llm_judge_unconfigured_returns_minus_one():
    score, reasons = evaluate_llm_judge({}, [{"role": "user", "content": "x"}])
    assert score == -1.0
    assert "not configured" in reasons.lower()


def test_evaluate_llm_judge_happy_path():
    cfg = {"model_ref": "openai/gpt-4o-mini", "rubric": "judge it"}
    transcript = [{"role": "user", "content": "hi"}]
    fake_response = _mock_completion_response('{"score": 0.85, "reasons": "looks good"}')

    with patch("litellm.completion", return_value=fake_response) as mock_completion:
        score, reasons = evaluate_llm_judge(cfg, transcript)

    assert score == 0.85
    assert reasons == "looks good"
    assert mock_completion.call_count == 1


def test_evaluate_llm_judge_clamps_to_unit_interval():
    cfg = {"model_ref": "openai/gpt-4o-mini", "rubric": "r"}
    fake_response = _mock_completion_response('{"score": 1.7, "reasons": "over"}')

    with patch("litellm.completion", return_value=fake_response):
        score, _ = evaluate_llm_judge(cfg, [{"role": "user", "content": "hi"}])

    assert score == 1.0


def test_evaluate_llm_judge_failure_returns_zero_not_minus_one():
    """Any litellm failure must surface as 0.0 so the grade penalises it."""
    cfg = {"model_ref": "openai/gpt-4o-mini", "rubric": "r"}

    with patch("litellm.completion", side_effect=RuntimeError("network down")):
        score, reasons = evaluate_llm_judge(cfg, [{"role": "user", "content": "hi"}])

    assert score == 0.0
    assert "network down" in reasons


def test_evaluate_llm_judge_empty_response_returns_zero():
    cfg = {"model_ref": "openai/gpt-4o-mini", "rubric": "r"}
    fake_response = _mock_completion_response("")

    with patch("litellm.completion", return_value=fake_response):
        score, reasons = evaluate_llm_judge(cfg, [{"role": "user", "content": "hi"}])

    assert score == 0.0
    assert "empty" in reasons.lower()


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
# Adapter serialization — llm_judge passed through to TaskDescription
# ---------------------------------------------------------------------------


def test_native_adapter_serializes_llm_judge():
    """A structured rubric round-trips through the runner GradingConfig wire shape."""
    from tolokaforge.runner.models import GradingConfig, LLMJudgeConfig

    # Direct construction of the runner-side GradingConfig with an llm_judge
    # round-trips through model_dump → adapter → model_validate, with the
    # structured rubric's criteria intact.
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

    # Reconstruct on the runner side — criteria survive the JSON wire trip.
    reconstructed = GradingConfig.model_validate(payload)
    assert reconstructed.llm_judge is not None
    assert reconstructed.llm_judge.rubric.reference == "Correct refund is $328.50."
    crit = reconstructed.llm_judge.rubric.criteria[0]
    assert crit.id == "refund_amount"
    assert crit.required is True
    assert crit.expected == "$328.50"


def test_evaluate_judge_uses_secret_manager_for_keys(monkeypatch):
    """The judge entrypoint mirrors SecretManager keys to os.environ.

    litellm reads provider keys directly off os.environ; the judge must
    call ``export_to_environ`` so the host's resolved secrets are visible.
    """
    cfg = {"model_ref": "openai/gpt-4o-mini", "rubric": "r"}

    fake_export_calls: list[list[str]] = []

    fake_sm = MagicMock()
    fake_sm.list_all_keys.return_value = ["OPENAI_API_KEY"]
    fake_sm.export_to_environ.side_effect = lambda keys: (
        fake_export_calls.append(list(keys)) or len(keys)
    )

    fake_response = _mock_completion_response('{"score": 0.5, "reasons": "ok"}')

    with (
        patch("tolokaforge.secrets.get_default", return_value=fake_sm),
        patch("litellm.completion", return_value=fake_response),
    ):
        evaluate_llm_judge(cfg, [{"role": "user", "content": "hi"}])

    assert fake_export_calls == [["OPENAI_API_KEY"]]
