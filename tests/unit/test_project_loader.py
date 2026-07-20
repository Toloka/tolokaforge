"""Unit tests for the project loader — walk-up, deep-merge, and the
task/run effective-config resolvers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.models import (
    GradingCombineConfig,
    ProjectConfig,
    RunDefaults,
    TaskDefaults,
)
from tolokaforge.core.project_loader import (
    CANONICAL_RUN_CONFIGS_DIR,
    LEGACY_RUN_CONFIG_DIR,
    PROJECT_FILENAME,
    deep_merge,
    detect_project_layout,
    find_project_yaml,
    load_project_config,
    resolve_effective_grading_combine,
    resolve_effective_run_config_data,
    warn_legacy_run_config_dir,
)

pytestmark = pytest.mark.unit


ENV_FIXTURE = (
    Path(__file__).parent.parent
    / "canonical"
    / "fixtures"
    / "environment_manifest"
    / "safe_one_service.yaml"
)


# ── deep_merge ─────────────────────────────────────────────────────────


class TestDeepMerge:
    def test_delta_wins_scalars(self) -> None:
        assert deep_merge({"a": 1, "b": 2}, {"a": 3}) == {"a": 3, "b": 2}

    def test_delta_overrides_nested_dicts(self) -> None:
        base = {"section": {"x": 1, "y": 2}}
        delta = {"section": {"y": 20, "z": 30}}
        assert deep_merge(base, delta) == {"section": {"x": 1, "y": 20, "z": 30}}

    def test_delta_lists_replace(self) -> None:
        assert deep_merge({"xs": [1, 2, 3]}, {"xs": [9]}) == {"xs": [9]}

    def test_empty_delta_returns_base_copy(self) -> None:
        base = {"a": 1}
        result = deep_merge(base, {})
        assert result == base
        assert result is not base  # copy, not alias

    def test_empty_base_returns_delta_shape(self) -> None:
        assert deep_merge({}, {"a": 1, "b": {"c": 2}}) == {"a": 1, "b": {"c": 2}}

    def test_none_delta_scalar_replaces_base(self) -> None:
        # Explicit None in delta overrides base; that's how a user
        # unsets a project-level default at task level.
        assert deep_merge({"key": "base"}, {"key": None}) == {"key": None}

    def test_dict_delta_over_scalar_base(self) -> None:
        # Delta wins even when the types disagree.
        assert deep_merge({"key": "scalar"}, {"key": {"nested": 1}}) == {"key": {"nested": 1}}


# ── find_project_yaml ──────────────────────────────────────────────────


class TestFindProjectYaml:
    def test_finds_at_start_dir(self, tmp_path: Path) -> None:
        (tmp_path / PROJECT_FILENAME).write_text("name: p\n")
        assert find_project_yaml(tmp_path) == (tmp_path / PROJECT_FILENAME).resolve()

    def test_walks_up_from_nested_file(self, tmp_path: Path) -> None:
        (tmp_path / PROJECT_FILENAME).write_text("name: p\n")
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        run_config = deep / "dev.yaml"
        run_config.write_text("models: {}\n")
        assert find_project_yaml(run_config) == (tmp_path / PROJECT_FILENAME).resolve()

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert find_project_yaml(tmp_path) is None

    def test_max_depth_caps_the_walk(self, tmp_path: Path) -> None:
        (tmp_path / PROJECT_FILENAME).write_text("name: p\n")
        deep = tmp_path
        for name in ["a", "b", "c", "d"]:
            deep = deep / name
            deep.mkdir()
        # Walk from 4-level-deep with max_depth=1 — shouldn't reach the root.
        assert find_project_yaml(deep, max_depth=1) is None


# ── detect_project_layout ──────────────────────────────────────────────


class TestDetectProjectLayout:
    def test_canonical_layout(self, tmp_path: Path) -> None:
        (tmp_path / PROJECT_FILENAME).write_text("name: p\n")
        runs = tmp_path / CANONICAL_RUN_CONFIGS_DIR
        runs.mkdir()
        cfg = runs / "dev.yaml"
        cfg.write_text("models: {}\n")
        project_root, legacy = detect_project_layout(cfg)
        assert project_root == tmp_path.resolve()
        assert legacy is False

    def test_legacy_directory_flagged(self, tmp_path: Path) -> None:
        (tmp_path / PROJECT_FILENAME).write_text("name: p\n")
        runs = tmp_path / LEGACY_RUN_CONFIG_DIR
        runs.mkdir()
        cfg = runs / "dev.yaml"
        cfg.write_text("models: {}\n")
        project_root, legacy = detect_project_layout(cfg)
        assert project_root == tmp_path.resolve()
        assert legacy is True

    def test_no_project_yaml(self, tmp_path: Path) -> None:
        runs = tmp_path / CANONICAL_RUN_CONFIGS_DIR
        runs.mkdir()
        cfg = runs / "dev.yaml"
        cfg.write_text("models: {}\n")
        project_root, legacy = detect_project_layout(cfg)
        assert project_root is None
        assert legacy is False


class TestLegacyDirWarning:
    def test_warn_emits_deprecation_warning(self, tmp_path: Path) -> None:
        cfg = tmp_path / LEGACY_RUN_CONFIG_DIR / "dev.yaml"
        cfg.parent.mkdir()
        cfg.write_text("models: {}\n")
        with pytest.warns(DeprecationWarning, match="run_configs"):
            warn_legacy_run_config_dir(cfg)


# ── load_project_config ────────────────────────────────────────────────


class TestLoadProjectConfig:
    def _write_project(self, root: Path, extra: dict | None = None) -> Path:
        content: dict = {"name": "demo", "version": 1}
        if extra:
            content.update(extra)
        path = root / PROJECT_FILENAME
        with path.open("w") as f:
            yaml.safe_dump(content, f)
        return path

    def test_loads_minimal_project(self, tmp_path: Path) -> None:
        path = self._write_project(tmp_path)
        project = load_project_config(path)
        assert project.name == "demo"
        assert project.version == 1
        assert isinstance(project.task_defaults, TaskDefaults)
        assert project.run_defaults is None

    def test_resolves_default_environment_stack_compose_relative_path(self, tmp_path: Path) -> None:
        # Copy the fixture compose file into a project-relative path so
        # relative-path resolution has something to hit.
        env_dir = tmp_path / "shared"
        env_dir.mkdir()
        rel_compose = env_dir / "environment.compose.yaml"
        rel_compose.write_text(ENV_FIXTURE.read_text())
        path = self._write_project(
            tmp_path,
            {
                "default_environment": {
                    "stack": {
                        "compose_file": "shared/environment.compose.yaml",
                    },
                },
            },
        )
        project = load_project_config(path)
        assert project.default_environment is not None
        assert project.default_environment.stack is not None
        assert project.default_environment.stack.compose_file is not None
        assert project.default_environment.stack.compose_file.is_absolute()
        assert project.default_environment.stack.compose_file == rel_compose.resolve()

    def test_null_default_environment_stack_warns_and_drops(self, tmp_path: Path) -> None:
        path = self._write_project(tmp_path, {"default_environment": {"stack": None}})
        with pytest.warns(
            DeprecationWarning, match=r"default_environment\.stack: null'.*is deprecated"
        ):
            project = load_project_config(path)
        # Null key is dropped so the project loads as if the stack were unset.
        assert project.default_environment is not None
        assert project.default_environment.stack is None

    def test_null_default_environment_stack_compose_file_warns_and_drops(
        self, tmp_path: Path
    ) -> None:
        path = self._write_project(
            tmp_path, {"default_environment": {"stack": {"compose_file": None}}}
        )
        with pytest.warns(
            DeprecationWarning,
            match=r"default_environment\.stack\.compose_file: null'.*is deprecated",
        ):
            project = load_project_config(path)
        # Null compose_file is dropped; stack subobject survives.
        assert project.default_environment is not None
        assert project.default_environment.stack is not None
        assert project.default_environment.stack.compose_file is None

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_project_config(tmp_path / "nope.yaml")

    def test_non_mapping_raises(self, tmp_path: Path) -> None:
        path = tmp_path / PROJECT_FILENAME
        path.write_text("- not a mapping\n")
        with pytest.raises(RuntimeError, match="YAML mapping"):
            load_project_config(path)


# ── resolve_effective_run_config_data ──────────────────────────────────


class TestResolveEffectiveRunConfig:
    def test_none_project_returns_run_config_unchanged(self) -> None:
        run_config = {"orchestrator": {"workers": 4}}
        assert resolve_effective_run_config_data(None, run_config) == run_config

    def test_missing_run_defaults_returns_run_config_unchanged(self) -> None:
        project = ProjectConfig(name="p")
        run_config = {"orchestrator": {"workers": 4}}
        assert resolve_effective_run_config_data(project, run_config) == run_config

    def test_run_defaults_layered_under_run_config(self) -> None:
        project = ProjectConfig(
            name="p",
            run_defaults=RunDefaults.model_validate(
                {
                    "compute": {"workers": 2, "max_budget_usd": 20.0},
                    "storage": {
                        # postgres_dsn is required alongside backend=postgres
                        # (validator on QueueStorageConfig), so this exercises
                        # both a nested override and inheritance of a
                        # non-schema-default value.
                        "queue": {
                            "backend": "postgres",
                            "postgres_dsn": "postgresql://x@h/db",
                        },
                    },
                }
            ),
        )
        run_config = {"compute": {"workers": 8}}  # only workers overridden
        merged = resolve_effective_run_config_data(project, run_config)
        assert merged["compute"]["workers"] == 8  # delta wins
        assert merged["compute"]["max_budget_usd"] == 20.0  # inherited from defaults
        assert merged["storage"]["queue"]["backend"] == "postgres"  # inherited
        assert merged["storage"]["queue"]["postgres_dsn"] == "postgresql://x@h/db"

    def test_returns_new_dict(self) -> None:
        project = ProjectConfig(
            name="p",
            run_defaults=RunDefaults.model_validate({"compute": {"workers": 2}}),
        )
        run_config = {"orchestrator": {"workers": 4}}
        merged = resolve_effective_run_config_data(project, run_config)
        assert merged is not run_config

    def test_preserves_storage_discriminator_when_type_matches_default(self) -> None:
        # Regression: dumping run_defaults with exclude_defaults=True dropped
        # the `type: "local"` discriminator on storage.artifacts/logs (matches
        # LocalStorageConfig.type default), then RunConfig(**merged) failed
        # to reconstruct the discriminated union with union_tag_not_found.
        # The example-microservices-pack is the shipped repro.
        from tolokaforge.core.models import RunConfig

        project = ProjectConfig(
            name="p",
            run_defaults=RunDefaults.model_validate(
                {
                    "compute": {"workers": 1},
                    "storage": {
                        "artifacts": {"type": "local", "path": "./results"},
                        "logs": {"type": "local", "path": "./results/logs"},
                    },
                }
            ),
        )
        run_config = {
            "models": {"agent": {"provider": "openrouter", "name": "moonshotai/kimi-k2.6"}},
            "orchestrator": {"repeats": 1, "max_turns": 4},
            "evaluation": {
                "projects": ["examples/native/tool_use/dataset"],
                "tasks_glob": "**/task.yaml",
                "output_dir": "results/regression",
            },
        }
        merged = resolve_effective_run_config_data(project, run_config)
        # Discriminator tag survives the dump.
        assert merged["storage"]["artifacts"]["type"] == "local"
        assert merged["storage"]["logs"]["type"] == "local"
        # And the merged dict reconstructs into a RunConfig without
        # union_tag_not_found on the discriminated storage backends.
        RunConfig(**merged)


# ── resolve_effective_grading_combine ──────────────────────────────────


class TestResolveEffectiveGradingCombine:
    def test_both_none_yields_canonical_defaults(self) -> None:
        combine = resolve_effective_grading_combine(None, None)
        assert combine == GradingCombineConfig()
        assert combine.method == "weighted"
        assert combine.weights == {}
        assert combine.pass_threshold == 0.8

    def test_project_only_fills_canonical_defaults(self) -> None:
        combine = resolve_effective_grading_combine({"weights": {"llm_judge": 1.0}}, None)
        assert combine.weights == {"llm_judge": 1.0}
        assert combine.pass_threshold == 0.8
        assert combine.method == "weighted"

    def test_task_only_wins_project_is_noop(self) -> None:
        combine = resolve_effective_grading_combine(
            None, {"weights": {"state_checks": 0.5}, "pass_threshold": 0.6}
        )
        assert combine.weights == {"state_checks": 0.5}
        assert combine.pass_threshold == 0.6

    def test_partial_task_delta_inherits_project_weights(self) -> None:
        # The ``long_debugging_session`` shape: task ships only
        # ``pass_threshold`` and inherits the project's whole ``weights``.
        combine = resolve_effective_grading_combine(
            {"weights": {"llm_judge": 1.0}}, {"pass_threshold": 0.7}
        )
        assert combine.method == "weighted"
        assert combine.weights == {"llm_judge": 1.0}
        assert combine.pass_threshold == 0.7

    def test_task_scalar_shadows_project_scalar(self) -> None:
        combine = resolve_effective_grading_combine(
            {"pass_threshold": 0.8}, {"pass_threshold": 0.7}
        )
        assert combine.pass_threshold == 0.7

    def test_weights_merge_key_by_key(self) -> None:
        combine = resolve_effective_grading_combine(
            {"weights": {"llm_judge": 1.0}}, {"weights": {"state_checks": 0.5}}
        )
        assert combine.weights == {"llm_judge": 1.0, "state_checks": 0.5}

    def test_weights_task_overrides_shared_key(self) -> None:
        combine = resolve_effective_grading_combine(
            {"weights": {"llm_judge": 1.0}}, {"weights": {"llm_judge": 0.5}}
        )
        assert combine.weights == {"llm_judge": 0.5}
