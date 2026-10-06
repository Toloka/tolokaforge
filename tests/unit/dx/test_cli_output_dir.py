"""Run-CLI ``--output-dir`` surface.

Uses ``CliRunner`` against a stubbed :class:`Orchestrator` that captures the
``RunConfig`` the CLI resolved, so we can assert that ``--output-dir`` overrides
:attr:`RunConfig.evaluation.output_dir` and that omitting it preserves the
config value or the ``results/run_<timestamp>`` default. No real LLM, Docker, or
filesystem outside the run's own output directory is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tolokaforge.core.orchestrator import GradingCompleteness
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def _write_config(tmp_path: Path, *, output_dir: str | None) -> Path:
    """Minimal ``run.yaml`` that parses. ``output_dir=None`` omits the
    ``evaluation.output_dir`` key so the CLI's timestamp default applies."""
    evaluation: dict[str, Any] = {"tasks_glob": str(tmp_path / "tasks" / "*")}
    if output_dir is not None:
        evaluation["output_dir"] = output_dir
    payload: dict[str, Any] = {
        "models": {
            "agent": {"provider": "openai", "name": "gpt-4"},
            "user": {"provider": "openai", "name": "gpt-4o"},
        },
        "evaluation": evaluation,
        "orchestrator": {"repeats": 1, "auto_start_services": False},
        "compute": {"workers": 1},
    }
    config_path = tmp_path / "run.yaml"
    config_path.write_text(yaml.safe_dump(payload))
    return config_path


def _make_capturing_orchestrator(captured: dict[str, Any], *, run_return: Path) -> type:
    class _CapturingOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["config"] = args[0] if args else kwargs.get("config")
            self.tasks = [object()]

        def load_tasks(self) -> None:
            return None

        def run(self, **_: object) -> Path:
            self.grading_completeness = GradingCompleteness(
                total_attempts=1, ungradeable_trial_ids=()
            )
            return run_return

    return _CapturingOrchestrator


def _invoke(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_path: Path,
    extra_args: list[str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    # The capturing orchestrator returns this dir as the run output; it must
    # exist so the CLI's post-run artifact-path emission resolves cleanly.
    run_return = (tmp_path / "run_return").resolve()
    run_return.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        cli_main,
        "Orchestrator",
        _make_capturing_orchestrator(captured, run_return=run_return),
    )
    args = ["run", "--config", str(config_path), *(extra_args or [])]
    result = runner.invoke(cli, args)
    return result, captured


def test_flag_overrides_config_output_dir(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--output-dir X`` wins over the config's ``evaluation.output_dir``."""
    config_path = _write_config(tmp_path, output_dir=str(tmp_path / "from_config"))
    override = str(tmp_path / "from_flag")

    result, captured = _invoke(
        runner,
        tmp_path,
        monkeypatch,
        config_path=config_path,
        extra_args=["--output-dir", override],
    )

    assert result.exit_code == 0, result.stderr
    assert captured["config"].evaluation.output_dir == override


def test_omitted_preserves_config_output_dir(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the flag, the config's ``evaluation.output_dir`` is unchanged."""
    config_value = str(tmp_path / "from_config")
    config_path = _write_config(tmp_path, output_dir=config_value)

    result, captured = _invoke(runner, tmp_path, monkeypatch, config_path=config_path)

    assert result.exit_code == 0, result.stderr
    assert captured["config"].evaluation.output_dir == config_value


def test_omitted_keeps_timestamp_default_when_config_sets_none(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that omits ``output_dir`` falls back to the
    ``results/run_<timestamp>`` default, untouched by the absent flag."""
    config_path = _write_config(tmp_path, output_dir=None)

    result, captured = _invoke(runner, tmp_path, monkeypatch, config_path=config_path)

    assert result.exit_code == 0, result.stderr
    assert captured["config"].evaluation.output_dir.startswith("results/run_")


def test_flag_beats_timestamp_default_when_config_sets_none(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the config sets no ``output_dir``, the flag still wins over the
    timestamp default."""
    config_path = _write_config(tmp_path, output_dir=None)
    override = str(tmp_path / "from_flag")

    result, captured = _invoke(
        runner,
        tmp_path,
        monkeypatch,
        config_path=config_path,
        extra_args=["--output-dir", override],
    )

    assert result.exit_code == 0, result.stderr
    assert captured["config"].evaluation.output_dir == override
