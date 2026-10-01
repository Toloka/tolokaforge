"""Unit tests for run-configuration validator.

Tests exercise ``tolokaforge.core.config_validator`` without network or
API keys.
"""

import json
from types import MappingProxyType

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.core.config_validator import (
    Severity,
    ValidationResult,
    _model_supports_reasoning,
    validate_run_config,
)
from tolokaforge.core.models import ModelConfig, RunConfig

pytestmark = pytest.mark.unit

_REGISTERABLE_TASK = {
    "task_id": "wire_task",
    "name": "wire_task",
    "category": "test",
    "description": "A task the trial spec can carry.",
    "adapter_type": "native",
    "system_prompt": "You are a test assistant.",
    "initial_state": {"tables": {}, "schemas": []},
    "agent_tools": [],
    "user_tools": [],
}


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

    def test_invalid_agent_loop(self):
        """A name no package registers is a config error, not a trial outcome."""
        cfg = _make_config()
        cfg["orchestrator"]["agent_loop"] = "engine_loop"
        result = validate_run_config(cfg)
        assert not result.ok
        loop_errors = [
            i
            for i in result.errors
            if i.path == "orchestrator.agent_loop" and "engine_loop" in i.message
        ]
        assert loop_errors, "expected an actionable orchestrator.agent_loop error"
        assert "engine-loop" in loop_errors[0].message

    def test_builtin_agent_loop_validates(self):
        cfg = _make_config()
        cfg["orchestrator"]["agent_loop"] = "engine-loop"
        result = validate_run_config(cfg)
        assert result.ok

    def test_agent_loop_left_unset_validates(self):
        """The field's default is a registration, so silence is not an error."""
        result = validate_run_config(_make_config())
        assert result.ok
        assert not [i for i in result.issues if i.path == "orchestrator.agent_loop"]

    @pytest.mark.parametrize(
        "session, offending",
        [
            pytest.param({}, "header", id="missing-header"),
            pytest.param({"header": "x session"}, "'x session'", id="space"),
            pytest.param({"header": "x:y"}, "'x:y'", id="colon"),
            pytest.param({"header": "Authorization"}, "'Authorization'", id="reserved"),
            pytest.param({"header": "x-session-id", "ttl": 30}, "ttl", id="unknown-key"),
        ],
    )
    def test_a_malformed_session_block_is_refused_at_load(self, session, offending):
        cfg = _make_config()
        cfg["models"]["agent"]["session"] = session
        with pytest.raises(ValidationError) as refused:
            RunConfig(**cfg)
        assert offending in str(refused.value)
        result = validate_run_config(cfg)
        assert [(i.path, offending in i.message) for i in result.errors] == [("(root)", True)]

    def test_a_fallback_does_not_inherit_its_parents_session(self):
        cfg = _make_config()
        cfg["models"]["agent"]["session"] = {"header": "x-session-id"}
        cfg["models"]["agent"]["fallbacks"] = [{"provider": "openrouter", "name": "openai/gpt-4o"}]
        agent = RunConfig(**cfg).models["agent"]
        assert agent.session is not None and agent.session.header == "x-session-id"
        assert agent.fallbacks[0].session is None


_BOOL_KEY_CLAUSE = (
    "unknown key True, which YAML read as bool — config keys must be strings. "
    "Quote it to write it as one."
)


class TestModelConfigRefusesUndeclaredKeys:
    """Every block under ``models.<role>`` refuses a key its type does not declare."""

    @pytest.mark.parametrize(
        "mutate, location, clause",
        [
            pytest.param(
                lambda agent: agent.update(
                    fallbacks=[{"provider": "openrouter", "name": "openai/gpt-4o", "sesion": {}}]
                ),
                "models.agent.fallbacks.0",
                "unknown key 'sesion' — did you mean 'session'?",
                id="fallback",
            ),
            pytest.param(
                lambda agent: agent.update(session={"hedaer": "x-session-id"}),
                "models.agent.session",
                "unknown key 'hedaer' — did you mean 'header'?",
                id="session",
            ),
            pytest.param(
                lambda agent: agent.update({True: "x"}),
                "models.agent",
                _BOOL_KEY_CLAUSE,
                id="non-string-key",
            ),
            pytest.param(
                lambda agent: agent.update(reasoning={True: 1}),
                "models.agent.reasoning",
                _BOOL_KEY_CLAUSE,
                id="reasoning-non-string-key",
            ),
        ],
    )
    def test_an_undeclared_key_is_refused_at_its_path_with_its_fix(self, mutate, location, clause):
        cfg = _make_config()
        mutate(cfg["models"]["agent"])

        with pytest.raises(ValidationError) as refused:
            RunConfig(**cfg)

        [error] = refused.value.errors()
        assert ".".join(str(part) for part in error["loc"]) == location
        assert clause in error["msg"]

    def test_a_mapping_that_is_not_a_dict_gets_the_named_refusal(self):
        block = MappingProxyType({"provider": "openrouter", "name": "a/b", "tempreature": 0.1})

        with pytest.raises(ValidationError) as refused:
            ModelConfig.model_validate(block)

        assert "did you mean 'temperature'?" in refused.value.errors()[0]["msg"]

        reasoning = MappingProxyType({"mdoe": "budget"})
        with pytest.raises(ValidationError) as refused:
            ModelConfig.model_validate(
                {"provider": "openrouter", "name": "a/b", "reasoning": reasoning}
            )

        [error] = refused.value.errors()
        assert error["loc"] == ("reasoning",)
        assert "did you mean 'mode'?" in error["msg"]

    def test_every_undeclared_key_in_one_block_is_named_in_one_refusal(self):
        cfg = _make_config()
        cfg["models"]["agent"].update(sesion={}, gateway_route="toloka_litellm")

        with pytest.raises(ValidationError) as refused:
            RunConfig(**cfg)

        [error] = refused.value.errors()
        assert "unknown key 'sesion'" in error["msg"]
        assert "unknown key 'gateway_route'" in error["msg"]

    def test_the_trial_spec_wire_refuses_an_undeclared_model_config_key(self):
        from tests.utils.runner_requests import trial_spec_json
        from tolokaforge.core.trial import TrialSpec

        spec = json.loads(trial_spec_json(_REGISTERABLE_TASK))
        spec["agent_model_config"]["sesion"] = {"header": "x-session-id"}

        with pytest.raises(ValidationError) as refused:
            TrialSpec.model_validate_json(json.dumps(spec))

        [error] = refused.value.errors()
        assert error["loc"] == ("agent_model_config",)
        assert "did you mean 'session'?" in error["msg"]

    def test_config_validate_reports_the_refusal_as_an_error(self, tmp_path):
        from click.testing import CliRunner

        from tolokaforge.dx.cli.main import cli

        cfg = _make_config()
        cfg["models"]["agent"]["sesion"] = {"header": "x-session-id"}
        config = tmp_path / "run.yaml"
        config.write_text(yaml.safe_dump(cfg))

        result = CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])

        assert result.exit_code != 0
        assert "[ERROR]" in result.output
        assert "\nmodels.agent\n" in result.output
        assert "did you mean 'session'" in result.output


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

    def test_an_unmapped_agent_model_without_overlay_declaration_reports_info(self, tmp_path):
        """`fake-vendor-xyz/muse-spark-1.2` is absent from litellm's map by
        construction, so the check cannot answer either way. The command emits
        an INFO nudge with the exact overlay entry to declare, and does not
        surface the "does not appear to support function calling" line — that
        message is reserved for models litellm's map carries with the flag
        explicitly False.

        Load-bearing invariant: `fake-vendor-xyz/muse-spark-1.2` stays absent
        from litellm's map. Vendor name is a fabrication; a future map
        cleanup that adds it would flip this assertion.
        """
        result = self._validate(self._tree(tmp_path, declared=False))
        assert "does not appear to support function calling" not in result.output
        assert result.exit_code == 0
        assert "cannot confirm function-calling support" in result.output


class TestUnmappedAgentModelReportsInfoNotError:
    """`litellm.supports_function_calling` returns False for two distinct
    states — "map has an entry that says False" and "map has no entry at all".
    The preflight distinguishes them via `get_model_info`: an unmapped key
    becomes an INFO with the overlay-declaration hint; a mapped-and-false key
    stays an ERROR.
    """

    def test_unmapped_agent_model_is_not_reported_unable(self):
        """`provider: google, name: gemini-legacy-v1` sits outside litellm's
        map — the Gemini entries live under the `gemini/` prefix, not
        `google/`, and no `google/gemini-legacy-v1` entry exists. The
        preflight emits an INFO nudge with the overlay entry to declare and
        exits zero; the "does not appear to support function calling" line
        is reserved for keys the map carries with the flag explicitly False.

        Load-bearing invariant: `google/gemini-legacy-v1` stays absent from
        litellm's map. If a future litellm bump adds it under the `google/`
        key, the map answer becomes authoritative and this INFO flips to
        whatever the map declares — desirable, but the assertion will need
        to move to a different unmapped key to keep locking the regression.
        """
        from tolokaforge.core.config_validator import Severity, validate_run_config

        cfg = _make_config(agent_provider="google", agent_name="gemini-legacy-v1")
        result = validate_run_config(cfg)

        assert result.ok, [str(i) for i in result.errors]
        assert not any(
            "does not appear to support function calling" in i.message for i in result.issues
        )
        infos = [
            i
            for i in result.issues
            if i.severity == Severity.INFO
            and i.path == "models.agent.name"
            and "cannot confirm function-calling support" in i.message
        ]
        assert len(infos) == 1, [str(i) for i in result.issues]

    def test_mapped_and_unsupported_agent_model_still_errors(self, monkeypatch):
        """Locks the branch, not a specific litellm entry: monkeypatch the
        seam so it returns False (mapped-and-unsupported) and assert the
        ERROR fires end-to-end from `_validate_model`. Binding to a concrete
        model id would flip with a litellm bump.
        """
        from tolokaforge.core import config_validator as cv
        from tolokaforge.core.llm.presets import set_overlay_path

        monkeypatch.setattr(cv, "_model_supports_function_calling", lambda name: False)
        # No overlay, so nothing declares the model: the pure False-from-litellm
        # path, not the overlay-declared shortcut.
        set_overlay_path(None)

        cfg = _make_config(agent_provider="openai", agent_name="whisper-1")
        result = cv.validate_run_config(cfg)

        fc_errors = [
            i
            for i in result.errors
            if i.path == "models.agent.name"
            and "does not appear to support function calling" in i.message
        ]
        assert len(fc_errors) == 1, [str(i) for i in result.issues]

    def test_openrouter_unmapped_agent_model_is_info_not_warning(self):
        """An unmapped OpenRouter agent-model is treated as unknown, not
        known-not-supported: INFO, same severity as any other unmapped
        provider. The `openrouter/` prefix downgrades a *mapped-and-False*
        answer to WARNING (niche upstream models), but that branch requires
        a positive False from the map — silence on unmapped keys is uniform.

        Load-bearing invariant: `openrouter/fake-vendor-xyz/muse-spark-9.9`
        stays absent from litellm's map.
        """
        from tolokaforge.core.config_validator import Severity, validate_run_config

        cfg = _make_config(
            agent_provider="openrouter",
            agent_name="fake-vendor-xyz/muse-spark-9.9",
        )
        result = validate_run_config(cfg)

        fc_issues = [
            i
            for i in result.issues
            if i.path == "models.agent.name"
            and (
                "does not appear to support function calling" in i.message
                or "cannot confirm function-calling support" in i.message
            )
        ]
        assert len(fc_issues) == 1, [str(i) for i in result.issues]
        assert fc_issues[0].severity == Severity.INFO
        assert "cannot confirm function-calling support" in fc_issues[0].message
