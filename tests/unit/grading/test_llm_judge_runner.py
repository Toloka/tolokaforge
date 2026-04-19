"""Unit tests for Runner-side LLM judge evaluation."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.runner.grading import (
    _format_transcript_for_judge,
    build_grade_reasons,
    combine_grade_components,
    evaluate_llm_judge,
)

pytestmark = pytest.mark.unit


class TestFormatTranscript:
    """Tests for _format_transcript_for_judge."""

    def test_basic_formatting(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = _format_transcript_for_judge(messages)
        assert "[user]: Hello" in result
        assert "[assistant]: Hi there" in result

    def test_empty_messages(self):
        result = _format_transcript_for_judge([])
        assert result == ""

    def test_skips_empty_content(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": ""},
            {"role": "assistant", "content": "World"},
        ]
        result = _format_transcript_for_judge(messages)
        assert "[system]" not in result

    def test_includes_tool_calls(self):
        messages = [
            {
                "role": "assistant",
                "content": "Let me read that file.",
                "tool_calls": [{"name": "read_file", "arguments": {"path": "test.txt"}}],
            },
            {"role": "tool", "content": "File contents here", "tool_call_id": "tc_1"},
        ]
        result = _format_transcript_for_judge(messages)
        assert "read_file" in result
        assert "tool result" in result
        assert "File contents here" in result

    def test_truncates_long_tool_output(self):
        long_content = "x" * 3000
        messages = [
            {"role": "tool", "content": long_content, "tool_call_id": "tc_1"},
        ]
        result = _format_transcript_for_judge(messages)
        assert "..." in result
        assert len(result) < 2100  # truncated to ~2000 + prefix


class TestEvaluateLLMJudge:
    """Tests for evaluate_llm_judge with mocked litellm."""

    def test_success(self):
        config = {
            "model_ref": "openrouter/anthropic/claude-sonnet-4-6",
            "rubric": "Grade quality",
            "output_schema": {"type": "object"},
        }
        messages = [{"role": "user", "content": "Test"}]

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps({"score": 0.85, "reasons": "Good"})))
        ]

        with patch("litellm.completion", return_value=mock_response) as mock_completion:
            score, reasons, cost = evaluate_llm_judge(config, messages)

        assert score == 0.85
        assert reasons == "Good"
        assert isinstance(cost, float)
        mock_completion.assert_called_once()

    def test_score_clamped(self):
        config = {
            "model_ref": "openrouter/test",
            "rubric": "Grade",
            "output_schema": {},
        }
        messages = [{"role": "user", "content": "Test"}]

        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=json.dumps({"score": 1.5, "reasons": "Over"})))
        ]

        with patch("litellm.completion", return_value=mock_response):
            score, _, _cost = evaluate_llm_judge(config, messages)

        assert score == 1.0  # Clamped

    def test_error_returns_zero_not_negative(self):
        """Judge failure returns 0.0 (included in score), not -1.0 (excluded)."""
        config = {
            "model_ref": "openrouter/test",
            "rubric": "Grade",
            "output_schema": {},
        }
        messages = [{"role": "user", "content": "Test"}]

        with patch("litellm.completion", side_effect=Exception("API error")):
            score, reasons, cost = evaluate_llm_judge(config, messages)

        assert score == 0.0  # Was -1.0, now 0.0
        assert "API error" in reasons
        assert cost == 0.0

    def test_no_config(self):
        """Missing model_ref or rubric returns -1.0 (not configured)."""
        score, reasons, cost = evaluate_llm_judge({}, [])
        assert score == -1.0  # Not configured = -1.0
        assert "not configured" in reasons
        assert cost == 0.0


class TestCombineWithLLMJudge:
    """Tests for combine_grade_components with llm_judge weight."""

    def test_weighted_with_llm_judge(self):
        components = {
            "hash_score": -1.0,
            "jsonpath_score": 1.0,
            "transcript_score": 1.0,
            "llm_judge_score": 0.5,
        }
        grading_config = {
            "combine_method": "weighted",
            "weights": {
                "state_checks": 0.6,
                "transcript_rules": 0.2,
                "llm_judge": 0.2,
            },
            "pass_threshold": 0.75,
            "state_checks": {},
            "transcript_rules": {},
            "llm_judge": {},
        }
        score, binary_pass = combine_grade_components(components, grading_config)
        # (1.0*0.6 + 1.0*0.2 + 0.5*0.2) / (0.6+0.2+0.2) = 0.9
        assert abs(score - 0.9) < 0.01
        assert binary_pass is True

    def test_llm_judge_not_evaluated_excluded(self):
        components = {
            "hash_score": -1.0,
            "jsonpath_score": 1.0,
            "transcript_score": 1.0,
            "llm_judge_score": -1.0,  # Not evaluated
        }
        grading_config = {
            "combine_method": "weighted",
            "weights": {
                "state_checks": 0.6,
                "transcript_rules": 0.2,
                "llm_judge": 0.2,
            },
            "pass_threshold": 0.75,
            "state_checks": {},
            "transcript_rules": {},
            "llm_judge": {},
        }
        score, binary_pass = combine_grade_components(components, grading_config)
        # llm_judge excluded: (1.0*0.6 + 1.0*0.2) / (0.6+0.2) = 1.0
        assert abs(score - 1.0) < 0.01
        assert binary_pass is True

    def test_llm_judge_failing_threshold(self):
        components = {
            "hash_score": -1.0,
            "jsonpath_score": 0.5,
            "transcript_score": 0.5,
            "llm_judge_score": 0.3,
        }
        grading_config = {
            "combine_method": "weighted",
            "weights": {
                "state_checks": 0.6,
                "transcript_rules": 0.2,
                "llm_judge": 0.2,
            },
            "pass_threshold": 0.75,
            "state_checks": {},
            "transcript_rules": {},
            "llm_judge": {},
        }
        score, binary_pass = combine_grade_components(components, grading_config)
        # (0.5*0.6 + 0.5*0.2 + 0.3*0.2) / 1.0 = 0.46
        assert score < 0.75
        assert binary_pass is False


class TestBuildGradeReasonsWithJudge:
    """Tests for build_grade_reasons including llm_judge."""

    def test_includes_judge_score(self):
        components = {
            "hash_score": -1.0,
            "jsonpath_score": 1.0,
            "jsonpath_reasons": "all passed",
            "transcript_score": 1.0,
            "llm_judge_score": 0.85,
        }
        reasons = build_grade_reasons(components)
        assert "Judge: score=0.85" in reasons

    def test_excludes_unevaluated_judge(self):
        components = {
            "hash_score": -1.0,
            "jsonpath_score": 1.0,
            "jsonpath_reasons": "pass",
            "transcript_score": -1.0,
            "llm_judge_score": -1.0,
        }
        reasons = build_grade_reasons(components)
        assert "Judge" not in reasons

    def test_includes_judge_reasons(self):
        """Judge reasons string is included in the output when provided."""
        components = {
            "hash_score": -1.0,
            "jsonpath_score": -1.0,
            "transcript_score": -1.0,
            "llm_judge_score": 0.75,
        }
        reasons = build_grade_reasons(components, judge_reasons="Agent followed instructions well")
        assert "Judge: score=0.75 (Agent followed instructions well)" in reasons

    def test_judge_failure_reasons(self):
        """When judge fails (score=0.0), the failure reason is included."""
        components = {
            "hash_score": -1.0,
            "jsonpath_score": -1.0,
            "transcript_score": -1.0,
            "llm_judge_score": 0.0,
        }
        reasons = build_grade_reasons(components, judge_reasons="LLM judge failed: API error")
        assert "Judge: score=0.00 (LLM judge failed: API error)" in reasons
