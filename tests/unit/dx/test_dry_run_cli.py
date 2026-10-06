"""CLI integration tests for ``tolokaforge run --dry-run``.

Locks the Stage 2 contract:

- exit 0 with stdout empty and the rendered panels on stderr,
- the first three tasks render as panels (fixed cap, no CLI knob),
- a task pack with fewer than three tasks renders that many exactly,
- zero HTTP: both ``httpx.Client.send`` and ``litellm.completion`` remain
  unreached by the CLI path,
- preset overlays supplied via ``--presets-file`` reach the resolved
  agent line on the rendered panel,
- ``--display=none`` silences the shared console — no preamble, no
  panels on stderr.

Every test drives Click's :class:`CliRunner` with ``mix_stderr=False``
so the stdout/stderr contract is inspectable directly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.core.llm.presets import ignored_sampling_params
from tolokaforge.core.logging import _TOLOKAFORGE_ROOT_HANDLER_SENTINEL
from tolokaforge.core.models import RunConfig
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_console_quiet():
    from tolokaforge.dx._display import console as _console

    saved = _console.quiet
    yield
    _console.quiet = saved


@pytest.fixture(autouse=True)
def _isolated_root_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
            root.removeHandler(handler)
    root.handlers = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def _write_task_pack(root: Path, task_ids: list[str]) -> Path:
    """Materialise a minimal native task pack under *root*.

    Each task carries a static ``initial_user_message`` so dry-run
    renders a literal user prompt (not the LLM-simulator placeholder).
    Returns the dataset root the run config should point at via
    ``evaluation.task_packs``.
    """
    dataset = root / "dataset"
    for task_id in task_ids:
        task_dir = dataset / "tasks" / "fixture" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.yaml").write_text(
            yaml.safe_dump(
                {
                    "task_id": task_id,
                    "name": task_id.replace("_", " ").title(),
                    "category": "fixture",
                    "description": "dry-run fixture task",
                    "max_turns": 4,
                    "initial_user_message": f"Hello from {task_id}.",
                    "initial_state": {},
                    "user_simulator": {"mode": "scripted", "persona": "silent"},
                    "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                    "grading": "grading.yaml",
                }
            )
        )
        (task_dir / "grading.yaml").write_text(
            yaml.safe_dump({"combine": {"method": "weighted", "pass_threshold": 0.5}})
        )
    return dataset


def _write_run_config(
    root: Path, dataset: Path, *, agent_name: str = "anthropic/claude-sonnet-4-6"
) -> Path:
    config_path = root / "run.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "agent": {
                        "provider": "openrouter",
                        "name": agent_name,
                    }
                },
                "orchestrator": {
                    "repeats": 1,
                    "auto_start_services": False,
                },
                "compute": {"workers": 1},
                "evaluation": {
                    "projects": [str(dataset)],
                    "tasks_glob": "**/task.yaml",
                    "output_dir": str(root / "out"),
                },
            }
        )
    )
    return config_path


def _write_harnesses_run_config(root: Path, dataset: Path) -> Path:
    """Run config with a two-entry ``harnesses:`` block and no ``harness_adapter``.

    Each entry pins one fixture task through its own native adapter — the shape
    that, before per-entry resolution reached the dry-run, fell through to
    single-adapter native whole-repo discovery.
    """
    config_path = root / "run_harnesses.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "agent": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4-6"}
                },
                "orchestrator": {"repeats": 1, "auto_start_services": False},
                "compute": {"workers": 1},
                "evaluation": {
                    "projects": [str(dataset)],
                    "tasks_glob": "**/task.yaml",
                    "output_dir": str(root / "out"),
                },
                "harnesses": {
                    "entries": [
                        {"name": "alpha", "adapter": "native", "task_ids": ["fixture_01"]},
                        {"name": "beta", "adapter": "native", "task_ids": ["fixture_02"]},
                    ]
                },
            }
        )
    )
    return config_path


def _count_panels(stderr: str) -> int:
    return len(re.findall(r"Task fixture_\d+ · Trial 0", stderr))


class TestDryRunHarnesses:
    def test_dry_run_resolves_each_harness_entry(self, runner: CliRunner, tmp_path: Path) -> None:
        """A ``harnesses:`` dry-run renders each entry's first-turn wiring through
        the composite — both entries, entry-qualified, no native fallthrough and
        no crash."""
        dataset = _write_task_pack(tmp_path, ["fixture_01", "fixture_02", "fixture_03"])
        config = _write_harnesses_run_config(tmp_path, dataset)

        result = runner.invoke(cli, ["run", "--config", str(config), "--dry-run"])

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""
        assert "Dry run:" in result.stderr
        assert "Task alpha/fixture_01 · Trial 0" in result.stderr
        assert "Task beta/fixture_02 · Trial 0" in result.stderr
        # Each entry's allow-list is honoured — fixture_03 is pulled by neither,
        # so no native whole-repo discovery leaked it in.
        assert "fixture_03" not in result.stderr
        assert "rendering first 2 sample(s) (of 2 task(s) available)" in result.stderr


class TestDryRunExitAndStreams:
    def test_dry_run_exits_zero_stdout_empty(self, runner: CliRunner, tmp_path: Path) -> None:
        dataset = _write_task_pack(tmp_path, ["fixture_01", "fixture_02", "fixture_03"])
        config = _write_run_config(tmp_path, dataset)

        result = runner.invoke(cli, ["run", "--config", str(config), "--dry-run"])

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""
        assert "Dry run:" in result.stderr


class TestDryRunPanelCount:
    def test_dry_run_renders_default_three_panels(self, runner: CliRunner, tmp_path: Path) -> None:
        """Dry-run always renders the first three tasks. The count is
        fixed at :data:`_DEFAULT_DRY_RUN_SAMPLES` — no CLI knob."""
        dataset = _write_task_pack(tmp_path, [f"fixture_{i:02d}" for i in range(1, 6)])
        config = _write_run_config(tmp_path, dataset)

        result = runner.invoke(cli, ["run", "--config", str(config), "--dry-run"])

        assert result.exit_code == 0, result.stderr
        assert _count_panels(result.stderr) == 3
        assert "rendering first 3 sample(s) (of 5 task(s) available)" in result.stderr

    def test_dry_run_caps_to_task_count_when_fewer(self, runner: CliRunner, tmp_path: Path) -> None:
        """A task pack with fewer than three tasks renders exactly that
        many — no padding."""
        dataset = _write_task_pack(tmp_path, ["fixture_01", "fixture_02"])
        config = _write_run_config(tmp_path, dataset)

        result = runner.invoke(cli, ["run", "--config", str(config), "--dry-run"])

        assert result.exit_code == 0, result.stderr
        assert _count_panels(result.stderr) == 2


class TestDryRunNoHttp:
    def test_dry_run_makes_no_http_via_respx_or_monkeypatch(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both the httpx transport and the litellm entry point stay
        untouched. Belt-and-braces guard against provider SDKs that
        might route around one of the two."""
        import httpx
        import litellm

        def _raise_http(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("dry-run must not call httpx.Client.send")

        def _raise_litellm(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("dry-run must not call litellm.completion")

        monkeypatch.setattr(httpx.Client, "send", _raise_http)
        monkeypatch.setattr(litellm, "completion", _raise_litellm)
        monkeypatch.setattr("tolokaforge.core.llm.client.completion", _raise_litellm, raising=False)

        dataset = _write_task_pack(tmp_path, ["fixture_01", "fixture_02"])
        config = _write_run_config(tmp_path, dataset)

        result = runner.invoke(cli, ["run", "--config", str(config), "--dry-run"])

        assert result.exit_code == 0, result.stderr
        assert _count_panels(result.stderr) == 2


class TestDryRunPresetOverlay:
    def test_dry_run_reflects_preset_overlays(self, runner: CliRunner, tmp_path: Path) -> None:
        """An overlay that binds the agent model to a distinct preset
        name must show up on the ``preset:`` field of the rendered
        panel — the same source-of-truth ``_write_artifacts`` records
        in ``task.yaml.model_config.agent.resolved.effective_preset``."""
        overlay_preset_name = "dry_ov"
        overlay_path = tmp_path / "overlay.yaml"
        overlay_path.write_text(
            yaml.safe_dump(
                {
                    "presets": {
                        overlay_preset_name: {
                            "match": ["anthropic/claude-sonnet-4-6"],
                            "response_policy": "standard",
                        }
                    }
                }
            )
        )
        dataset = _write_task_pack(tmp_path, ["fixture_01"])
        config = _write_run_config(tmp_path, dataset)

        result = runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(config),
                "--presets-file",
                str(overlay_path),
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert overlay_preset_name in result.stderr


class TestDryRunJudgeModel:
    def test_the_judge_flag_writes_no_temperature(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A judge model whose preset sends no sampling earns no ignored-value
        warning, because the flag never writes a temperature the user did not."""
        overlay_path = tmp_path / "overlay.yaml"
        preset = {"match": ["acme/judge*"], "params": {"supports_sampling_params": False}}
        overlay_path.write_text(yaml.safe_dump({"presets": {"acme_judge": preset}}))
        dataset = _write_task_pack(tmp_path, ["fixture_01"])
        config = _write_run_config(tmp_path, dataset)
        built: list[RunConfig] = []
        monkeypatch.setattr(
            "tolokaforge.dx.cli.main._run_dry_run",
            lambda *, run_config, **_: built.append(run_config),
        )

        result = runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(config),
                "--presets-file",
                str(overlay_path),
                "--judge-model",
                "acme/judge-1",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.stderr
        [run_config] = built
        assert "temperature" not in run_config.models["judge"].model_fields_set
        assert ignored_sampling_params(run_config.models) == []


class TestDryRunDisplayNone:
    def test_dry_run_display_none_silences_stderr(self, runner: CliRunner, tmp_path: Path) -> None:
        dataset = _write_task_pack(tmp_path, ["fixture_01", "fixture_02"])
        config = _write_run_config(tmp_path, dataset)

        result = runner.invoke(
            cli, ["--display", "none", "run", "--config", str(config), "--dry-run"]
        )

        assert result.exit_code == 0, result.stderr
        assert result.stderr == ""
        assert result.stdout == ""
