"""Unit tests for the ProjectConfig / TaskDefaults schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import (
    ActorSpec,
    GradingCombineConfig,
    GradingDefaults,
    ProjectConfig,
    StuckHeuristicsDefaults,
    TaskDefaults,
    TaskDiscoveryConfig,
    TaskInventoryConfig,
    TimeoutDefaults,
)
from tolokaforge.runner.models import EnvironmentPatch, StackPatch

pytestmark = pytest.mark.unit

ENV_FIXTURE = (
    Path(__file__).parent.parent
    / "canonical"
    / "fixtures"
    / "environment_manifest"
    / "safe_one_service.yaml"
)


class TestTaskDiscoveryDefaults:
    def test_default_glob(self) -> None:
        cfg = TaskDiscoveryConfig()
        assert cfg.glob == "tasks/**/task.yaml"

    def test_custom_glob(self) -> None:
        cfg = TaskDiscoveryConfig(glob="benchmarks/**/task.yaml")
        assert cfg.glob == "benchmarks/**/task.yaml"

    def test_inventory_defaults(self) -> None:
        inv = TaskInventoryConfig()
        assert inv.discovery.glob == "tasks/**/task.yaml"


class TestTaskDefaultsFields:
    def test_all_fields_optional(self) -> None:
        td = TaskDefaults()
        assert td.adapter_type is None
        assert td.max_turns is None
        assert td.system_prompt is None
        assert td.actors is None
        assert td.policies == {}
        assert td.metadata is None
        assert td.adapter_settings == {}
        assert td.tools is None
        assert td.grading_defaults is None
        assert td.timeouts is None
        assert td.stuck_heuristics is None
        assert td.continue_prompt is None

    def test_max_turns_positive(self) -> None:
        with pytest.raises(ValidationError):
            TaskDefaults(max_turns=0)

    def test_actors_user_nested(self) -> None:
        td = TaskDefaults(actors={"user": ActorSpec(mode="llm", persona="curious engineer")})
        assert td.actors is not None
        assert td.actors["user"].persona == "curious engineer"

    def test_timeout_defaults_nested(self) -> None:
        td = TaskDefaults(timeouts=TimeoutDefaults(trial_seconds=1200, tool_call_seconds=90))
        assert td.timeouts is not None
        assert td.timeouts.trial_seconds == 1200
        assert td.timeouts.tool_call_seconds == 90

    def test_timeout_defaults_reject_non_positive(self) -> None:
        with pytest.raises(ValidationError):
            TimeoutDefaults(trial_seconds=0)
        with pytest.raises(ValidationError):
            TimeoutDefaults(tool_call_seconds=0)

    def test_stuck_heuristics_defaults(self) -> None:
        sh = StuckHeuristicsDefaults()
        assert sh.enabled is True
        assert sh.max_repeated_tool_calls == 5
        assert sh.max_idle_turns == 3

    def test_stuck_heuristics_reject_non_positive_counts(self) -> None:
        with pytest.raises(ValidationError):
            StuckHeuristicsDefaults(max_repeated_tool_calls=0)
        with pytest.raises(ValidationError):
            StuckHeuristicsDefaults(max_idle_turns=0)

    def test_grading_defaults_wraps_combine(self) -> None:
        gd = GradingDefaults(
            combine=GradingCombineConfig(
                method="weighted",
                weights={"state_checks": 0.5, "llm_judge": 0.5},
                pass_threshold=0.7,
            )
        )
        assert gd.combine is not None
        assert gd.combine.pass_threshold == 0.7

    def test_grading_defaults_accepts_partial_combine(self) -> None:
        # A project can declare only pass_threshold without enumerating
        # every weight — weights defaults to an empty dict.
        gd = GradingDefaults(combine=GradingCombineConfig(pass_threshold=0.9))
        assert gd.combine is not None
        assert gd.combine.pass_threshold == 0.9
        assert gd.combine.weights == {}

    def test_grading_defaults_refuse_a_combine_key_they_do_not_declare(self) -> None:
        """The rejection reaches ``project.yaml``, four levels down from its own root.

        A project-level ``methd`` used to drop out of the whole layer in silence, so
        every task inheriting these defaults folded as ``weighted``.
        """
        with pytest.raises(ValidationError) as excinfo:
            ProjectConfig(
                name="typo-eval",
                task_defaults={"grading_defaults": {"combine": {"methd": "all"}}},
            )

        assert [error["loc"] for error in excinfo.value.errors()] == [
            ("task_defaults", "grading_defaults", "combine", "methd")
        ]

    def test_policies_and_adapter_settings_are_dicts(self) -> None:
        td = TaskDefaults(
            policies={"max_tool_calls_per_turn": 10},
            adapter_settings={"bundle_path": "./domain"},
        )
        assert td.policies == {"max_tool_calls_per_turn": 10}
        assert td.adapter_settings == {"bundle_path": "./domain"}


class TestProjectConfigMinimal:
    def test_minimal_project(self) -> None:
        p = ProjectConfig(name="minimal-eval")
        assert p.name == "minimal-eval"
        assert p.version == 1
        assert p.description is None
        assert p.tasks.discovery.glob == "tasks/**/task.yaml"
        assert p.default_environment is None
        assert isinstance(p.task_defaults, TaskDefaults)
        assert p.run_defaults is None

    def test_project_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            ProjectConfig()  # type: ignore[call-arg]

    def test_project_version_default_is_one(self) -> None:
        p = ProjectConfig(name="v-check")
        assert p.version == 1

    def test_project_with_populated_task_defaults(self) -> None:
        p = ProjectConfig(
            name="populated",
            description="A populated project",
            task_defaults=TaskDefaults(
                adapter_type="native",
                max_turns=20,
                continue_prompt="Continue.",
                timeouts=TimeoutDefaults(trial_seconds=600, tool_call_seconds=60),
                stuck_heuristics=StuckHeuristicsDefaults(),
            ),
        )
        assert p.description == "A populated project"
        assert p.task_defaults.adapter_type == "native"
        assert p.task_defaults.max_turns == 20
        assert p.task_defaults.continue_prompt == "Continue."


class TestProjectConfigWithEnvironment:
    def test_default_environment_binds(self) -> None:
        p = ProjectConfig(
            name="with-env",
            default_environment=EnvironmentPatch(stack=StackPatch(compose_file=ENV_FIXTURE)),
        )
        assert p.default_environment is not None
        assert p.default_environment.stack is not None
        assert p.default_environment.stack.compose_file == ENV_FIXTURE

    def test_patch_constructs_with_no_io(self) -> None:
        # The patch shape is I/O-free at construction: pointing at a
        # non-existent compose file must not raise. Validation is
        # deferred to :func:`resolve`, which materialises the manifest.
        patch = EnvironmentPatch(stack=StackPatch(compose_file=Path("/nonexistent/compose.yaml")))
        assert patch.stack is not None
        assert patch.stack.compose_file == Path("/nonexistent/compose.yaml")
