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
    if agent_reasoning.lower() in ("off", ""):
        reasoning_block: dict = {"mode": "off"}
    else:
        reasoning_block = {"mode": "adaptive", "effort_hint": agent_reasoning}
    base = {
        "models": {
            "agent": {
                "provider": agent_provider,
                "name": agent_name,
                "temperature": 0.6,
                "reasoning": reasoning_block,
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
        runtime_errors = [
            i
            for i in result.errors
            if i.path == "orchestrator.runtime" and "in-process" in i.message
        ]
        assert runtime_errors, "expected an actionable orchestrator.runtime error"
        message = runtime_errors[0].message
        for known in ("shared", "per_trial", "in_memory"):
            assert known in message

    @pytest.mark.parametrize("runtime", ["shared", "per_trial", "in_memory"])
    def test_valid_builtin_runtime(self, runtime: str):
        cfg = _make_config()
        cfg["orchestrator"]["runtime"] = runtime
        result = validate_run_config(cfg)
        assert result.ok

    def test_docker_alias_validates(self):
        """``runtime: docker`` is a retained legacy alias for ``shared`` — it
        must still pass validation even though the registry has no ``docker``
        name."""
        cfg = _make_config()
        cfg["orchestrator"]["runtime"] = "docker"
        result = validate_run_config(cfg)
        assert result.ok
        assert not [i for i in result.errors if i.path == "orchestrator.runtime"]


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
            ("anthropic/claude-opus-4.7", True),
            ("openai/o3-mini", True),
            ("openai/o1-preview", True),
            ("openai/gpt-5.4", True),
            ("openai/gpt-5.4-pro", True),
            ("openai/gpt-5.5", True),
            ("google/gemini-3-flash-preview", True),
            ("google/gemini-2.0-flash", True),
            ("deepseek/deepseek-reasoner", True),
            ("qwen/qwen3.6-plus", True),
            ("moonshotai/kimi-k2.6", True),
            ("moonshotai/kimi-k2.5", True),
            ("minimax/minimax-m2.7", False),
            ("meta-llama/llama-3-70b", False),
            ("mistral/mistral-large", False),
            ("x-ai/grok-4.20", None),  # unknown
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


class TestPreflightConsultsTheOverlay:
    """`config validate` loads the `litellm_models:` block, so it has to use it.

    Otherwise the command that schema-validates the declaration turns around
    and reports the model unable to call functions, while the run works. The
    test drives the real CLI: an earlier version of this passed by calling
    `set_overlay_path` itself, which is exactly the step the CLI was missing.
    """

    def _tree(self, tmp_path, *, declared: bool, provider: str = "fake-vendor-xyz"):
        import yaml

        overlay = tmp_path / "overlay.yaml"
        entry = {"evidence": "2026-08-10, litellm 1.96.0: measured"}
        if declared:
            entry["supports_function_calling"] = True
        else:
            entry["supports_reasoning"] = True
        overlay.write_text(
            yaml.safe_dump({"litellm_models": {"fake-vendor-xyz/muse-spark-1.2": entry}})
        )

        config = tmp_path / "run.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "evaluation": {
                        "tasks_glob": "tasks/**/task.yaml",
                        "output_dir": "output",
                        "harness_adapter": {"type": "frozen_mcp_core"},
                    },
                    "orchestrator": {"workers": 1, "repeats": 1},
                    "models": {
                        "agent": {"provider": provider, "name": "muse-spark-1.2"},
                        "user": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"},
                    },
                    "engine": {"presets_file": str(overlay)},
                }
            )
        )
        return config

    def _validate(self, config):
        from click.testing import CliRunner

        from tolokaforge.dx.cli.main import cli

        return CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])

    def test_a_declared_model_is_not_reported_unable(self, tmp_path):
        result = self._validate(self._tree(tmp_path, declared=True))
        assert "does not appear to support function calling" not in result.output

    @pytest.mark.parametrize("provider", ["meta", "Meta", "META"])
    def test_the_preflight_lookup_is_case_symmetric_like_the_run(self, provider, tmp_path):
        """Asserted on the lookup, not on the CLI output.

        A capitalised provider makes litellm raise, so the wrapper answers
        `None` and no issue is emitted whatever the overlay says - a CLI-level
        case test would pass without the symmetry existing.
        """
        import yaml

        from tolokaforge.core import config_validator as cv
        from tolokaforge.core.llm.presets import set_overlay_path

        overlay = tmp_path / "overlay.yaml"
        overlay.write_text(
            yaml.safe_dump(
                {
                    "litellm_models": {
                        f"{provider.lower()}/muse-spark-1.2": {
                            "supports_function_calling": True,
                            "evidence": "2026-08-10, litellm 1.96.0: measured",
                        }
                    }
                }
            )
        )
        set_overlay_path(str(overlay))
        try:
            assert cv._declared_function_calling("muse-spark-1.2", provider) is True
        finally:
            set_overlay_path(None)

    def test_and_one_the_overlay_does_not_declare_still_is(self, tmp_path):
        """The check still fails closed - the overlay is not a blanket pass."""
        result = self._validate(self._tree(tmp_path, declared=False))
        assert "does not appear to support function calling" in result.output
