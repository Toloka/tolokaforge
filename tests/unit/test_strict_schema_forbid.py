"""Unit tests for strict ``extra="forbid"`` on the Project-layer models and
the ``construct_config`` loader error surface.

Two contracts:

* Every Project-layer root rejects an unknown key — a stray field is a load
  error, not a silently-dropped value.
* The schema-shape aliases still resolve under strict validation: they are
  lifted / renamed at the parse boundary *before* the extra-key check, so a
  legacy key never reaches the model as an unknown field.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tolokaforge.core.deprecations import canonicalize_actor_config
from tolokaforge.core.models import (
    EvaluationConfig,
    GradingConfig,
    ProjectConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.project_loader import construct_config
from tolokaforge.runner.models import EnvironmentPatch, NetworkPolicy, SecurityContext

pytestmark = pytest.mark.unit


def _run_config_base(**extra) -> dict:
    return {"models": {}, "orchestrator": {}, "evaluation": {"output_dir": "x"}, **extra}


class TestUnknownKeysRejected:
    """A representative model from each YAML root refuses a stray field."""

    def test_run_config_rejects_unknown_key(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            RunConfig(**_run_config_base(bogus=1))

    def test_project_config_rejects_unknown_key(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ProjectConfig(name="p", bogus=1)

    def test_task_config_rejects_unknown_key(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            TaskConfig(task_id="t", description="d", bogus=1)

    def test_grading_config_rejects_unknown_key(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            GradingConfig(combine={}, bogus=1)

    def test_environment_patch_rejects_unknown_key(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            EnvironmentPatch(bogus=1)


class TestConstructConfigErrorSurface:
    def test_unknown_key_names_file_key_and_suggestion(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            construct_config(
                RunConfig,
                _run_config_base(orchestrator={"mox_turns": 5}),
                source=Path("run_configs/dev.yaml"),
            )
        message = str(excinfo.value)
        assert "dev.yaml" in message
        assert "orchestrator.mox_turns" in message
        assert "max_turns" in message

    def test_non_extra_error_re_raises_unchanged(self) -> None:
        # A missing required field is not an extra-key error; the loader must
        # surface pydantic's own ValidationError, never the friendly message.
        with pytest.raises(ValidationError):
            construct_config(
                RunConfig,
                {"orchestrator": {}, "evaluation": {"output_dir": "x"}},
                source=Path("run_configs/dev.yaml"),
            )


class TestAliasesSurviveStrictValidation:
    """The critical interaction: legacy aliases are lifted before the extra
    check, so strict validation accepts them while an unknown key fails."""

    def test_task_packs_alias_accepted(self) -> None:
        cfg = EvaluationConfig(task_packs=["./pack_a"], output_dir="x")
        assert cfg.projects == ["./pack_a"]

    def test_uppercase_network_policy_accepted(self) -> None:
        patch = EnvironmentPatch(network_policy="NO_INTERNET")
        assert patch.network_policy is NetworkPolicy.NO_INTERNET

    def test_security_context_user_alias_accepted(self) -> None:
        ctx = SecurityContext(user=1000)
        assert ctx.run_as_user == 1000

    def test_user_simulator_alias_lifted_before_forbid(self) -> None:
        # ``user_simulator`` is canonicalised into ``actors.user`` at the
        # loader boundary (per-layer, pre-merge) — the same lift the task
        # loader runs before constructing TaskConfig. Strict validation then
        # sees only ``actors.user`` and accepts it.
        data = canonicalize_actor_config(
            {"task_id": "t", "description": "d", "user_simulator": {"mode": "scripted"}}
        )
        task = construct_config(TaskConfig, data, source=Path("task.yaml"))
        assert task.resolve_user_simulator().mode == "scripted"
