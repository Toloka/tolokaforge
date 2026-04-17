"""Unit tests for run-configuration validator.

Tests exercise ``tolokaforge.core.config_validator`` without network or
API keys.
"""

import pytest

from tolokaforge.core.config_validator import (
    Severity,
    ValidationResult,
    _model_supports_reasoning,
    validate_run_config,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    agent_name: str = "openai/gpt-4o",
    agent_provider: str = "openrouter",
    agent_reasoning: str = "off",
    user_name: str = "anthropic/claude-sonnet-4.6",
    user_provider: str = "openrouter",
    **overrides: object,
) -> dict:
    """Build a minimal valid RunConfig dict."""
    base = {
        "models": {
            "agent": {
                "provider": agent_provider,
                "name": agent_name,
                "temperature": 0.6,
                "reasoning": agent_reasoning,
            },
            "user": {
                "provider": user_provider,
                "name": user_name,
                "temperature": 0.0,
            },
        },
        "orchestrator": {
            "workers": 5,
            "repeats": 3,
            "max_turns": 30,
            "runtime": "docker",
        },
        "evaluation": {
            "tasks_glob": "tasks/**/task.yaml",
            "output_dir": "output",
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Verify that schema violations produce errors."""

    def test_valid_config_no_errors(self):
        result = validate_run_config(_make_config())
        assert result.ok
        assert len(result.errors) == 0

    def test_missing_models_key(self):
        raw = {
            "orchestrator": {"workers": 1, "runtime": "docker"},
            "evaluation": {"output_dir": "x"},
        }
        result = validate_run_config(raw)
        assert not result.ok

    def test_missing_evaluation_key(self):
        raw = {
            "models": {
                "agent": {"provider": "openrouter", "name": "openai/gpt-4o"},
                "user": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"},
            },
            "orchestrator": {"workers": 1, "runtime": "docker"},
        }
        result = validate_run_config(raw)
        assert not result.ok

    def test_invalid_runtime(self):
        cfg = _make_config()
        cfg["orchestrator"]["runtime"] = "in-process"
        result = validate_run_config(cfg)
        assert not result.ok


# ---------------------------------------------------------------------------
# Reasoning compatibility
# ---------------------------------------------------------------------------


class TestReasoningValidation:
    """Verify reasoning-related warnings."""

    def test_reasoning_off_no_warning(self):
        result = validate_run_config(_make_config(agent_reasoning="off"))
        reasoning_issues = [i for i in result.issues if "reasoning" in i.path]
        assert len(reasoning_issues) == 0

    def test_reasoning_on_supported_model_no_warning(self):
        """Claude and Gemini-3 should be recognized as reasoning-capable."""
        result = validate_run_config(
            _make_config(agent_name="anthropic/claude-opus-4.6", agent_reasoning="medium")
        )
        reasoning_warnings = [
            i for i in result.issues if "reasoning" in i.path and i.severity == Severity.WARNING
        ]
        assert len(reasoning_warnings) == 0

    def test_reasoning_on_unsupported_model_warns(self):
        """MiniMax should trigger a reasoning warning."""
        result = validate_run_config(
            _make_config(agent_name="minimax/minimax-m2.7", agent_reasoning="medium")
        )
        reasoning_warnings = [
            i for i in result.issues if "reasoning" in i.path and i.severity == Severity.WARNING
        ]
        assert len(reasoning_warnings) == 1
        assert "minimax" in reasoning_warnings[0].message.lower()

    def test_reasoning_on_unknown_model_info(self):
        """Unknown model should produce an INFO, not a warning."""
        result = validate_run_config(
            _make_config(agent_name="some-new-vendor/new-model", agent_reasoning="high")
        )
        reasoning_infos = [
            i for i in result.issues if "reasoning" in i.path and i.severity == Severity.INFO
        ]
        assert len(reasoning_infos) == 1


# ---------------------------------------------------------------------------
# Model reasoning support helper
# ---------------------------------------------------------------------------


class TestModelSupportsReasoning:
    """Direct tests for ``_model_supports_reasoning``."""

    @pytest.mark.parametrize(
        "model,expected",
        [
            ("anthropic/claude-opus-4.6", True),
            ("anthropic/claude-sonnet-4.6", True),
            ("openai/o3-mini", True),
            ("openai/o1-preview", True),
            ("google/gemini-3-flash-preview", True),
            ("google/gemini-2.0-flash", True),
            ("deepseek/deepseek-reasoner", True),
            ("minimax/minimax-m2.7", False),
            ("meta-llama/llama-3-70b", False),
            ("mistral/mistral-large", False),
            ("x-ai/grok-4.20", None),  # unknown
            ("openai/gpt-5.4-pro", None),  # unknown
        ],
    )
    def test_known_models(self, model: str, expected: bool | None):
        assert _model_supports_reasoning(model) is expected


# ---------------------------------------------------------------------------
# Max tokens
# ---------------------------------------------------------------------------


class TestMaxTokensValidation:
    """Verify max_tokens boundary checks."""

    def test_normal_max_tokens_no_warning(self):
        cfg = _make_config()
        cfg["models"]["agent"]["max_tokens"] = 16384
        result = validate_run_config(cfg)
        max_tok_warns = [i for i in result.issues if "max_tokens" in i.path]
        assert len(max_tok_warns) == 0

    def test_huge_max_tokens_warns(self):
        cfg = _make_config()
        cfg["models"]["agent"]["max_tokens"] = 200_000
        result = validate_run_config(cfg)
        max_tok_warns = [
            i for i in result.issues if "max_tokens" in i.path and i.severity == Severity.WARNING
        ]
        assert len(max_tok_warns) == 1


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TestOrchestratorValidation:
    """Verify orchestrator sanity checks."""

    def test_high_workers_warns(self):
        cfg = _make_config()
        cfg["orchestrator"]["workers"] = 100
        result = validate_run_config(cfg)
        worker_warns = [i for i in result.issues if "workers" in i.path]
        assert len(worker_warns) == 1

    def test_high_max_turns_warns(self):
        cfg = _make_config()
        cfg["orchestrator"]["max_turns"] = 200
        result = validate_run_config(cfg)
        turn_warns = [i for i in result.issues if "max_turns" in i.path]
        assert len(turn_warns) == 1


# ---------------------------------------------------------------------------
# ValidationResult helpers
# ---------------------------------------------------------------------------


class TestValidationResult:
    """Test result dataclass properties."""

    def test_ok_when_no_errors(self):
        r = ValidationResult()
        assert r.ok

    def test_not_ok_when_error(self):
        from tolokaforge.core.config_validator import ValidationIssue

        r = ValidationResult(
            issues=[
                ValidationIssue(
                    severity=Severity.ERROR,
                    path="x",
                    message="bad",
                )
            ]
        )
        assert not r.ok

    def test_ok_with_warnings_only(self):
        from tolokaforge.core.config_validator import ValidationIssue

        r = ValidationResult(
            issues=[
                ValidationIssue(
                    severity=Severity.WARNING,
                    path="x",
                    message="hmm",
                )
            ]
        )
        assert r.ok
