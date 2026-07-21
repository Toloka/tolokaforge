"""Unit tests for the ``construct_config`` unknown-key warning surface and
the schema-shape aliases that survive it.

Two contracts:

* An unknown top-level key on a Project-layer root emits a
  ``DeprecationWarning`` naming the file, the key, and the closest schema
  match — then the key is dropped so the pack still loads.
* The schema-shape aliases still resolve: they are lifted / renamed at the
  parse boundary before construction, so a legacy key never surfaces as an
  unknown field.
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


class TestUnknownKeyWarnings:
    """A representative model from each YAML root warns on a stray top-level
    key and then drops it rather than failing the load."""

    def test_run_config_warns_on_unknown_key(self) -> None:
        with pytest.warns(DeprecationWarning, match="unknown key 'bogus'"):
            cfg = construct_config(
                RunConfig, _run_config_base(bogus=1), source=Path("run_configs/dev.yaml")
            )
        assert not hasattr(cfg, "bogus")

    def test_project_config_warns_on_unknown_key(self) -> None:
        with pytest.warns(DeprecationWarning, match="unknown key 'bogus'"):
            construct_config(ProjectConfig, {"name": "p", "bogus": 1}, source=Path("project.yaml"))

    def test_task_config_warns_on_unknown_key(self) -> None:
        with pytest.warns(DeprecationWarning, match="unknown key 'bogus'"):
            construct_config(
                TaskConfig,
                {"task_id": "t", "description": "d", "bogus": 1},
                source=Path("task.yaml"),
            )

    def test_grading_config_warns_on_unknown_key(self) -> None:
        with pytest.warns(DeprecationWarning, match="unknown key 'bogus'"):
            construct_config(
                GradingConfig, {"combine": {}, "bogus": 1}, source=Path("grading.yaml")
            )

    def test_environment_patch_warns_on_unknown_key(self) -> None:
        with pytest.warns(DeprecationWarning, match="unknown key 'bogus'"):
            construct_config(EnvironmentPatch, {"bogus": 1}, source=Path("task.yaml"))

    def test_warning_names_file_key_and_suggestion(self) -> None:
        with pytest.warns(DeprecationWarning) as record:
            construct_config(
                RunConfig, _run_config_base(computee={}), source=Path("run_configs/dev.yaml")
            )
        message = str(record[0].message)
        assert "dev.yaml" in message
        assert "computee" in message
        assert "compute" in message

    def test_nested_unknown_key_dropped_without_warning(self, recwarn) -> None:
        # Only top-level keys are checked; an unknown key nested inside a
        # sub-model is dropped silently — a future strict flip restores the
        # recursive scan.
        construct_config(
            RunConfig,
            _run_config_base(orchestrator={"mox_turns": 5}),
            source=Path("run_configs/dev.yaml"),
        )
        assert not [w for w in recwarn if "unknown key" in str(w.message)]


class TestConstructConfigErrorSurface:
    def test_non_extra_error_re_raises_unchanged(self) -> None:
        # A missing required field is not an unknown-key case; the loader must
        # surface pydantic's own ValidationError, never swallow it behind the
        # warn-and-drop path.
        with pytest.raises(ValidationError):
            construct_config(
                RunConfig,
                {"orchestrator": {}, "evaluation": {"output_dir": "x"}},
                source=Path("run_configs/dev.yaml"),
            )


class TestAliasesSurviveConstruction:
    """The critical interaction: legacy aliases are lifted before the model
    sees the data, so construction accepts them while an unknown key warns."""

    def test_task_packs_alias_accepted(self) -> None:
        cfg = EvaluationConfig(task_packs=["./pack_a"], output_dir="x")
        assert cfg.projects == ["./pack_a"]

    def test_uppercase_network_policy_accepted(self) -> None:
        patch = EnvironmentPatch(network_policy="NO_INTERNET")
        assert patch.network_policy is NetworkPolicy.NO_INTERNET

    def test_security_context_user_alias_accepted(self) -> None:
        ctx = SecurityContext(user=1000)
        assert ctx.run_as_user == 1000

    def test_user_simulator_alias_lifted_before_construction(self) -> None:
        # ``user_simulator`` is canonicalised into ``actors.user`` at the
        # loader boundary (per-layer, pre-merge) — the same lift the task
        # loader runs before constructing TaskConfig. Construction then sees
        # only ``actors.user`` and accepts it.
        data = canonicalize_actor_config(
            {"task_id": "t", "description": "d", "user_simulator": {"mode": "scripted"}}
        )
        task = construct_config(TaskConfig, data, source=Path("task.yaml"))
        assert task.resolve_user_simulator().mode == "scripted"
