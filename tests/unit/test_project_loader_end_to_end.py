"""End-to-end tests for the project loader flow — mirrors what the CLI
does when it processes ``--config``.

Each scenario writes a small ``project.yaml`` + ``run_configs/*.yaml``
tree under ``tmp_path`` and drives it through the same resolver chain
the CLI uses. Fast: no Docker, no LLM, no filesystem outside tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.models import RunConfig
from tolokaforge.core.project_loader import (
    detect_project_layout,
    load_project_config,
    resolve_effective_run_config_data,
    synthesize_default_project,
    warn_legacy_run_config_dir,
)

pytestmark = pytest.mark.unit


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(data, f)


def _drive_cli_loader(config_path: Path) -> RunConfig:
    """Replay the sequence the CLI uses. Returns the effective RunConfig.

    Kept in sync with ``tolokaforge.cli.main.run``:
    1. Read the YAML at *config_path*.
    2. Detect the enclosing project layout.
    3. Load ``project.yaml`` or synthesise a default.
    4. Merge ``project.run_defaults`` under the run-config dict.
    5. Construct ``RunConfig`` from the merged dict.
    """
    with config_path.open() as f:
        config_data = yaml.safe_load(f)
    project_root, used_legacy_dir = detect_project_layout(config_path)
    if used_legacy_dir:
        warn_legacy_run_config_dir(config_path)
    if project_root is not None:
        project = load_project_config(project_root / "project.yaml")
    else:
        project = synthesize_default_project(project_root=config_path.parent)
    config_data = resolve_effective_run_config_data(project, config_data)
    return RunConfig(**config_data)


class TestProjectAwareLoad:
    def test_run_defaults_layered_under_dev_run_config(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "demo",
                "run_defaults": {
                    "compute": {"workers": 2, "max_budget_usd": 20.0},
                    "orchestrator": {"repeats": 1},
                },
            },
        )
        _write_yaml(
            tmp_path / "run_configs" / "dev.yaml",
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/model"}},
                "orchestrator": {"repeats": 5},  # override just repeats
                "evaluation": {"output_dir": "results/dev"},
            },
        )
        run_config = _drive_cli_loader(tmp_path / "run_configs" / "dev.yaml")
        # Inherited from run_defaults:
        assert run_config.compute is not None
        assert run_config.compute.workers == 2
        assert run_config.compute.max_budget_usd == 20.0
        # Overridden by delta:
        assert run_config.orchestrator.repeats == 5
        # Delta-only fields:
        assert run_config.evaluation.output_dir == "results/dev"

    def test_run_config_delta_wins_on_nested_conflict(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "demo",
                "run_defaults": {
                    "compute": {"workers": 2, "max_budget_usd": 20.0},
                    "orchestrator": {"repeats": 1},  # required by RunConfig schema
                },
            },
        )
        _write_yaml(
            tmp_path / "run_configs" / "nightly.yaml",
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/m"}},
                "compute": {"workers": 16, "max_budget_usd": 200.0},
                "evaluation": {"output_dir": "results/nightly"},
            },
        )
        run_config = _drive_cli_loader(tmp_path / "run_configs" / "nightly.yaml")
        assert run_config.compute is not None
        assert run_config.compute.workers == 16  # delta wins
        assert run_config.compute.max_budget_usd == 200.0
        # orchestrator inherits from run_defaults since delta omits it
        assert run_config.orchestrator.repeats == 1

    def test_run_config_without_project_yaml_uses_synthesised_default(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "run_configs" / "dev.yaml",
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/m"}},
                "orchestrator": {"repeats": 3},
                "evaluation": {"output_dir": "results/dev"},
            },
        )
        # No project.yaml — loader must synthesise a default silently
        # (info log, no warning).
        run_config = _drive_cli_loader(tmp_path / "run_configs" / "dev.yaml")
        assert run_config.orchestrator.repeats == 3
        assert run_config.compute is None  # nothing to inject

    def test_legacy_run_config_dir_emits_deprecation_warning(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "project.yaml", {"name": "demo"})
        _write_yaml(
            tmp_path / "run_config" / "dev.yaml",  # singular — legacy
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/m"}},
                "orchestrator": {"repeats": 1},
                "evaluation": {"output_dir": "results/dev"},
            },
        )
        with pytest.warns(DeprecationWarning, match="run_configs"):
            run_config = _drive_cli_loader(tmp_path / "run_config" / "dev.yaml")
        assert run_config.evaluation.output_dir == "results/dev"

    def test_canonical_run_configs_dir_no_warning(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "project.yaml", {"name": "demo"})
        _write_yaml(
            tmp_path / "run_configs" / "dev.yaml",  # plural — canonical
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/m"}},
                "orchestrator": {"repeats": 1},
                "evaluation": {"output_dir": "results/dev"},
            },
        )
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("error", DeprecationWarning)
            _drive_cli_loader(tmp_path / "run_configs" / "dev.yaml")


class TestTaskLoaderWithProjectDefaults:
    """Task-side resolution flowing through the modified ``load_task_yaml``."""

    def test_project_task_defaults_layered_under_task(self, tmp_path: Path) -> None:
        from tolokaforge.adapters._task_loader import load_task_yaml

        # Minimal task.yaml with only per-task identity + one override.
        task_dir = tmp_path / "tasks" / "sample_task"
        task_dir.mkdir(parents=True)
        _write_yaml(
            task_dir / "task.yaml",
            {
                "task_id": "sample",
                "name": "Sample",
                "category": "demo",
                "description": "sample task",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {"mode": "llm"},
                "grading": "grading.yaml",
                "max_turns": 60,  # task-level override
            },
        )
        # Project supplies max_turns=20 by default; adapter_type=native.
        project_defaults = {
            "adapter_type": "native",
            "max_turns": 20,
            "continue_prompt": "Continue.",
        }
        task, task_dir_out = load_task_yaml(
            task_dir / "task.yaml",
            project_task_defaults=project_defaults,
        )
        assert task.task_id == "sample"
        assert task.max_turns == 60  # task wins
        assert task.adapter_type == "native"  # from defaults

    def test_stuck_heuristics_and_timeouts_flow_from_project_defaults(self, tmp_path: Path) -> None:
        # Project-level task_defaults declares stuck_heuristics and
        # timeouts; the M2 loader chain layers them under each task; the
        # constructed TaskConfig carries them (they were dropped before
        # this milestone because TaskConfig lacked the fields).
        from tolokaforge.adapters._task_loader import load_task_yaml

        task_dir = tmp_path / "tasks" / "sample_task"
        task_dir.mkdir(parents=True)
        _write_yaml(
            task_dir / "task.yaml",
            {
                "task_id": "sample",
                "description": "sample task",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {"mode": "llm"},
                "grading": "grading.yaml",
            },
        )
        project_defaults = {
            "stuck_heuristics": {
                "enabled": True,
                "max_repeated_tool_calls": 4,
                "max_idle_turns": 2,
            },
            "timeouts": {"trial_seconds": 400, "tool_call_seconds": 30},
        }
        task, _ = load_task_yaml(
            task_dir / "task.yaml",
            project_task_defaults=project_defaults,
        )
        assert task.stuck_heuristics is not None
        assert task.stuck_heuristics.max_repeated_tool_calls == 4
        assert task.stuck_heuristics.max_idle_turns == 2
        assert task.timeouts is not None
        assert task.timeouts.trial_seconds == 400
        assert task.timeouts.tool_call_seconds == 30

    def test_task_load_without_defaults_matches_legacy_behaviour(self, tmp_path: Path) -> None:
        from tolokaforge.adapters._task_loader import load_task_yaml

        task_dir = tmp_path / "tasks" / "legacy"
        task_dir.mkdir(parents=True)
        _write_yaml(
            task_dir / "task.yaml",
            {
                "task_id": "legacy",
                "name": "Legacy",
                "category": "demo",
                "description": "no project.yaml",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {"mode": "llm"},
                "grading": "grading.yaml",
                "adapter_type": "native",
                "max_turns": 40,
            },
        )
        task, _ = load_task_yaml(task_dir / "task.yaml")
        assert task.task_id == "legacy"
        assert task.max_turns == 40

    def test_project_defaults_outrank_domain_bundle(self, tmp_path: Path) -> None:
        """Precedence chain (low → high): domain → project defaults → task.

        Reproduces the specific case where domain sets a field, project
        sets the same field to a different value, and the task doesn't
        touch it — project must win.
        """
        from tolokaforge.adapters._task_loader import load_task_yaml

        domain_dir = tmp_path / "_shared"
        domain_dir.mkdir()
        _write_yaml(
            domain_dir / "domain.yaml",
            {
                "adapter_type": "native",
                "max_turns": 15,
                "policies": {"guidance": ["from domain"]},
            },
        )
        task_dir = tmp_path / "testcases" / "case_a"
        task_dir.mkdir(parents=True)
        _write_yaml(
            task_dir / "task.yaml",
            {
                "domain": "../../_shared/domain.yaml",
                "task_id": "case_a",
                "name": "Case A",
                "category": "demo",
                "description": "domain-referring task",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {"mode": "llm"},
                "grading": "grading.yaml",
            },
        )
        # Project overrides the same fields the domain set.
        project_defaults = {
            "adapter_type": "tlk_mcp_core",  # differs from domain's "native"
            "max_turns": 50,  # differs from domain's 15
        }
        task, _ = load_task_yaml(
            task_dir / "task.yaml",
            project_task_defaults=project_defaults,
        )
        assert task.adapter_type == "tlk_mcp_core"  # project wins over domain
        assert task.max_turns == 50  # project wins over domain
        # Fields set only by domain still survive when project/task are silent.
        assert task.policies == {"guidance": ["from domain"]}

    def test_task_still_beats_project_and_domain(self, tmp_path: Path) -> None:
        """Task.yaml is the top of the precedence chain."""
        from tolokaforge.adapters._task_loader import load_task_yaml

        domain_dir = tmp_path / "_shared"
        domain_dir.mkdir()
        _write_yaml(
            domain_dir / "domain.yaml",
            {"max_turns": 10},
        )
        task_dir = tmp_path / "testcases" / "case_b"
        task_dir.mkdir(parents=True)
        _write_yaml(
            task_dir / "task.yaml",
            {
                "domain": "../../_shared/domain.yaml",
                "task_id": "case_b",
                "name": "Case B",
                "category": "demo",
                "description": "task overrides everything",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {"mode": "llm"},
                "grading": "grading.yaml",
                "max_turns": 99,  # task's own value
            },
        )
        task, _ = load_task_yaml(
            task_dir / "task.yaml",
            project_task_defaults={"max_turns": 50},
        )
        assert task.max_turns == 99  # task wins over project and domain


class TestProjectPathResolution:
    """`_resolve_project_paths` must rewrite every task-side path field
    that shows up in project.task_defaults, not just the two the earlier
    version handled."""

    def test_project_system_prompt_resolved_to_absolute_path(self, tmp_path: Path) -> None:
        prompt = tmp_path / "shared" / "system.md"
        prompt.parent.mkdir()
        prompt.write_text("hi")
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "demo",
                "task_defaults": {"system_prompt": "shared/system.md"},
            },
        )
        from tolokaforge.core.project_loader import load_project_config

        project = load_project_config(tmp_path / "project.yaml")
        assert project.task_defaults.system_prompt == str(prompt.resolve())

    def test_project_tools_mcp_server_path_resolved(self, tmp_path: Path) -> None:
        """A ``tools.agent.mcp_server`` string on task_defaults is a path
        field per the task loader's declaration; the project loader must
        resolve it before Pydantic construction so downstream consumers
        find the file."""
        mcp = tmp_path / "shared" / "mcp_server.py"
        mcp.parent.mkdir()
        mcp.write_text("# stub")
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "demo",
                "task_defaults": {
                    "tools": {"agent": {"mcp_server": "shared/mcp_server.py"}},
                },
            },
        )
        from tolokaforge.core.project_loader import load_project_config

        project = load_project_config(tmp_path / "project.yaml")
        assert project.task_defaults.tools is not None
        assert project.task_defaults.tools.agent["mcp_server"] == str(mcp.resolve())


class TestSchemaAliasCollision:
    """`EvaluationConfig` must emit a clear DeprecationWarning when a
    caller sets both ``projects`` and ``task_packs``."""

    def test_both_set_projects_wins(self) -> None:
        import warnings as _warnings

        from tolokaforge.core.models import EvaluationConfig

        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            cfg = EvaluationConfig(
                output_dir="results/x",
                projects=["a", "b"],
                task_packs=["c", "d"],
            )
        assert cfg.projects == ["a", "b"]
        assert cfg.task_packs == []
        assert any(
            issubclass(w.category, DeprecationWarning) and "both set" in str(w.message)
            for w in caught
        )


class TestActorRosterSubsetOfModels:
    """The roster check runs after ``project.run_defaults`` merges into
    the selected run config — the only point where both ``actors`` and
    ``models`` are visible together."""

    def test_llm_actor_present_in_models_passes(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "p",
                "task_defaults": {
                    "actors": {"user": {"mode": "llm", "persona": "curious"}},
                },
                "run_defaults": {
                    "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                },
            },
        )
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(run_cfg, {"models": {"user": {"provider": "openai", "name": "gpt-4o"}}})

        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert "user" in merged["models"]

    def test_llm_actor_missing_from_models_fails_loud(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "p",
                "task_defaults": {
                    "actors": {"user": {"mode": "llm", "persona": "curious"}},
                },
            },
        )
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(run_cfg, {"models": {"judge": {"provider": "openai", "name": "gpt-4o"}}})

        from tolokaforge.core.project_loader import load_effective_run_config

        with pytest.raises(ValueError, match="not declared under `models`"):
            load_effective_run_config(run_cfg)

    def test_scripted_actor_does_not_require_model(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "p",
                "task_defaults": {
                    "actors": {"user": {"mode": "scripted"}},
                },
            },
        )
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(run_cfg, {})

        from tolokaforge.core.project_loader import load_effective_run_config

        # Scripted actors don't need a matching model entry — must not raise.
        merged, _ = load_effective_run_config(run_cfg)
        assert merged.get("models", {}) == {}

    def test_no_actors_declared_is_no_op(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(run_cfg, {})

        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert merged == {}

    def test_llm_actor_fails_when_models_is_empty(self, tmp_path: Path) -> None:
        # Boundary case: `models: {}` is a valid mapping but has no
        # entries. An llm actor still fails loud — pinning it explicitly
        # so the "missing" branch isn't confused with a shape-error
        # branch by a future refactor.
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "p",
                "task_defaults": {
                    "actors": {"user": {"mode": "llm", "persona": "curious"}},
                },
            },
        )
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(run_cfg, {"models": {}})

        from tolokaforge.core.project_loader import load_effective_run_config

        with pytest.raises(ValueError, match="not declared under `models`"):
            load_effective_run_config(run_cfg)

    def test_malformed_models_block_is_a_load_error(self, tmp_path: Path) -> None:
        # A typo where `models:` is a list (or any non-mapping) would
        # confuse the roster check downstream — surface the shape error
        # at the check site with the offending type in the message.
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "p",
                "task_defaults": {
                    "actors": {"user": {"mode": "llm", "persona": "curious"}},
                },
            },
        )
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(run_cfg, {"models": ["not", "a", "mapping"]})

        from tolokaforge.core.project_loader import load_effective_run_config

        with pytest.raises(ValueError, match="`models`.*must be a mapping"):
            load_effective_run_config(run_cfg)


class TestProjectAssetsPathAnchoring:
    """``assets.seeds.<name>.path`` and bare-string shorthand entries
    resolve to absolute paths under the project directory."""

    def test_dict_form_relative_path_anchored(self, tmp_path: Path) -> None:
        # A seed file that must exist so downstream consumers can find it.
        seed = tmp_path / "shared" / "seeds" / "base.sql"
        seed.parent.mkdir(parents=True)
        seed.write_text("-- fixture\n")
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "p",
                "assets": {
                    "seeds": {
                        "base": {
                            "path": "shared/seeds/base.sql",
                            "kind": "sql_dump",
                        },
                    },
                },
            },
        )
        project = load_project_config(tmp_path / "project.yaml")
        assert project.assets is not None
        assert project.assets.seeds["base"].path == seed.resolve()

    def test_bare_string_shorthand_relative_anchored(self, tmp_path: Path) -> None:
        seed = tmp_path / "shared" / "seeds" / "base.sql"
        seed.parent.mkdir(parents=True)
        seed.write_text("-- fixture\n")
        _write_yaml(
            tmp_path / "project.yaml",
            {"name": "p", "assets": {"seeds": {"base": "shared/seeds/base.sql"}}},
        )
        project = load_project_config(tmp_path / "project.yaml")
        assert project.assets is not None
        assert project.assets.seeds["base"].path == seed.resolve()
        assert project.assets.seeds["base"].kind == "sql_dump"


class TestEnvVarInterpolation:
    """``${VAR}`` substitution runs on run-config string values after
    ``project.run_defaults`` merge and before the roster check. Only
    string values are interpolated; keys, numbers, booleans, and lists
    of non-strings pass through untouched. Missing variables collect
    into a single error naming every offending key path."""

    def test_interpolated_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_OUTPUT_DIR", "results/from-env")
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {"output_dir": "${MY_OUTPUT_DIR}"},
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert merged["evaluation"]["output_dir"] == "results/from-env"

    def test_missing_variable_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {"output_dir": "${DEFINITELY_UNSET_VAR}"},
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        with pytest.raises(
            ValueError,
            match=r"unresolved environment variable.*DEFINITELY_UNSET_VAR",
        ):
            load_effective_run_config(run_cfg)

    def test_non_strings_pass_through_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No env var set — numbers, booleans, and lists of non-strings
        # must not participate in the walker's string-value branch.
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "orchestrator": {
                    "workers": 4,
                    "auto_start_services": True,
                    "repeats": 1,
                },
                "evaluation": {"output_dir": "results/x"},
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert merged["orchestrator"]["auto_start_services"] is True

    def test_string_with_no_placeholder_untouched(self, tmp_path: Path) -> None:
        # A literal string without ``${...}`` must survive verbatim —
        # the walker's replace callback only rewrites placeholders.
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {"output_dir": "results/literal"},
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert merged["evaluation"]["output_dir"] == "results/literal"

    def test_bareword_dollar_not_interpolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only ``${VAR}`` (braces mandatory) is interpolated; ``$VAR``
        # bareword stays literal — an intentional design choice
        # documented on the helper.
        monkeypatch.setenv("BAREWORD_VAR", "would-be-substituted")
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {"output_dir": "prefix-$BAREWORD_VAR"},
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert merged["evaluation"]["output_dir"] == "prefix-$BAREWORD_VAR"

    def test_interpolation_fires_after_run_defaults_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Project supplies a base model name with a placeholder that the
        # operator resolves at run time. The run config doesn't override
        # models, so the project default flows through the merge into
        # the effective dict — and then interpolation fires, substituting
        # the operator-supplied value. Uses `models` (which is a valid
        # RunDefaults field) since `evaluation` isn't on RunDefaults.
        monkeypatch.setenv("MODEL_TAG", "gpt-4o-2026-nightly")
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "p",
                "run_defaults": {
                    "models": {"user": {"provider": "openai", "name": "${MODEL_TAG}"}},
                },
            },
        )
        run_cfg = tmp_path / "run_configs" / "nightly.yaml"
        _write_yaml(run_cfg, {"evaluation": {"output_dir": "results/nightly"}})
        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert merged["models"]["user"]["name"] == "gpt-4o-2026-nightly"

    def test_credential_shaped_names_rejected_at_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rule-2 boundary: even if the env var IS set, a placeholder
        # with a credential-shaped suffix is rejected. Credentials
        # must flow through SecretManager, not the plaintext merged
        # config dict.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak-here")
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {"output_dir": "results/${OPENAI_API_KEY}"},
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        with pytest.raises(ValueError) as exc:
            load_effective_run_config(run_cfg)
        message = str(exc.value)
        assert "credential-shaped" in message
        assert "OPENAI_API_KEY" in message
        assert "SecretManager" in message
        # And the env var's real value must never appear in the error —
        # the point of the boundary is to keep it out of every downstream
        # log surface, starting with our own error path.
        assert "sk-should-not-leak-here" not in message

    def test_credential_suffix_variants_all_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Suffix-based match mirrors tests/unit/secrets/test_no_raw_secret_access.py.
        # A representative from each suffix pins the shape. Uses
        # ``evaluation.output_dir`` (non-deprecated string field) as
        # the concatenation site so this test doesn't depend on the
        # deprecated ``orchestrator.continue_prompt`` — if a future
        # refactor moves interpolation after RunConfig construction,
        # the assertion still holds without picking up a stray
        # DeprecationWarning.
        variants = [
            "MY_TOKEN",
            "MY_SECRET",
            "MY_PASSWORD",
            "POSTGRES_DSN",
            "GITHUB_PAT",
            "MY_CREDENTIAL",
            "MY_CREDENTIALS",
        ]
        for var in variants:
            monkeypatch.setenv(var, "unused")
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {
                    # Non-deprecated string field; the walker sees every
                    # concatenated placeholder in one pass.
                    "output_dir": "results/"
                    + "-".join(f"${{{v}}}" for v in variants),
                },
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        with pytest.raises(ValueError, match="credential-shaped") as exc:
            load_effective_run_config(run_cfg)
        message = str(exc.value)
        for var in variants:
            assert var in message, f"expected {var!r} in error message"

    def test_dict_keys_are_never_interpolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pins that only string VALUES are interpolated. A ${...}-shaped
        # dict key would parse as a literal string key on the merged
        # dict — no substitution attempted, no missing-var error.
        monkeypatch.setenv("SHOULD_NOT_FIRE", "surprise")
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {"output_dir": "results/x"},
                "orchestrator": {
                    # A YAML mapping key using ${...} shape — the walker
                    # must skip keys entirely, so this survives verbatim
                    # regardless of whether the env var is set.
                    "typesense": {"${SHOULD_NOT_FIRE}": "literal-value"},
                },
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        # The key survives untouched; env var value never leaks in.
        assert "${SHOULD_NOT_FIRE}" in merged["orchestrator"]["typesense"]
        assert "surprise" not in merged["orchestrator"]["typesense"]

    def test_variable_names_with_digits_substitute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The regex's second-char class is [A-Za-z0-9_], so digits are
        # allowed after the first character. Pin the common shapes
        # (``NAME_1``, ``NAME2``, ``NAME_1A``) so a future tightening
        # of the pattern can't silently break them.
        monkeypatch.setenv("MY_VAR_1", "one")
        monkeypatch.setenv("MY_VAR2", "two")
        monkeypatch.setenv("MY_VAR_1A", "one-a")
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {
                    "output_dir": "results/${MY_VAR_1}-${MY_VAR2}-${MY_VAR_1A}",
                },
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert merged["evaluation"]["output_dir"] == "results/one-two-one-a"

    def test_key_path_in_error_uses_bracket_index_no_dot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin the rendered key-path format for list indices: the error
        # should say ``projects[0]`` (not ``projects.[0]``). A regression
        # in :func:`_render_key_path` would reintroduce the spurious dot.
        monkeypatch.delenv("MISSING_IN_LIST", raising=False)
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {
                    "output_dir": "results/x",
                    "projects": ["${MISSING_IN_LIST}"],
                },
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        with pytest.raises(ValueError) as exc:
            load_effective_run_config(run_cfg)
        message = str(exc.value)
        assert "evaluation.projects[0]" in message, message
        assert "projects.[0]" not in message, message

    def test_placeholder_in_list_value_substituted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The walker recurses lists; pin the list-branch positively so
        # a future refactor to the walker can't silently break list-
        # of-strings substitution (a shape real packs use for
        # ``evaluation.projects`` when parameterising by env).
        monkeypatch.setenv("PACK_ROOT", "/abs/packs")
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {
                    "output_dir": "results/x",
                    "projects": ["${PACK_ROOT}/pack-a", "${PACK_ROOT}/pack-b"],
                },
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        merged, _ = load_effective_run_config(run_cfg)
        assert merged["evaluation"]["projects"] == [
            "/abs/packs/pack-a",
            "/abs/packs/pack-b",
        ]

    def test_input_dict_not_mutated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The public helper's contract says the input tree is not
        # mutated — a caller who kept a reference to the raw dict must
        # still see it verbatim after the interpolation returns. Pin
        # via an isolated helper call rather than through the loader
        # (loader wraps everything in a fresh dict via deep_merge).
        import copy

        from tolokaforge.core.project_loader import _interpolate_env_vars

        monkeypatch.setenv("VAL", "resolved")
        original = {
            "root": "${VAL}/x",
            "nested": {"leaf": "prefix-${VAL}"},
            "list_of_strings": ["a", "${VAL}"],
        }
        snapshot = copy.deepcopy(original)
        result = _interpolate_env_vars(original, source_path=tmp_path / "x.yaml")
        assert original == snapshot, "input dict was mutated"
        assert result is not original
        assert result["root"] == "resolved/x"

    def test_multiple_missing_vars_reported_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Author-ergonomic: report every miss in a single error rather
        # than one-at-a-time. Fixing three typos should take one edit
        # pass, not three reload cycles.
        for var in ("UNSET_A", "UNSET_B", "UNSET_C"):
            monkeypatch.delenv(var, raising=False)
        _write_yaml(tmp_path / "project.yaml", {"name": "p"})
        run_cfg = tmp_path / "run_configs" / "dev.yaml"
        _write_yaml(
            run_cfg,
            {
                "models": {"user": {"provider": "openai", "name": "${UNSET_A}"}},
                "evaluation": {"output_dir": "${UNSET_B}/${UNSET_C}"},
            },
        )
        from tolokaforge.core.project_loader import load_effective_run_config

        with pytest.raises(ValueError) as exc:
            load_effective_run_config(run_cfg)
        message = str(exc.value)
        assert "UNSET_A" in message
        assert "UNSET_B" in message
        assert "UNSET_C" in message


class TestDeepMergeIsSingleImpl:
    """`deep_merge` in the project loader is the single implementation
    the task loader imports — no shadow copy in `_task_loader.py`."""

    def test_task_loader_imports_from_project_loader(self) -> None:
        from tolokaforge.adapters import _task_loader
        from tolokaforge.core import project_loader

        assert _task_loader.deep_merge is project_loader.deep_merge
