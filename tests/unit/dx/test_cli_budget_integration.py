"""End-to-end wiring between :file:`LIMIT_HIT.json` and the run-end banner.

The orchestrator writes the marker; the CLI reads it and threads
``stopped_reason=f"{marker.which} limit"`` into
:func:`print_run_end_banner`. This test drives that path with a
stubbed :class:`Orchestrator` that writes the marker inside its
``run`` body — the assertion is on the banner bytes on stderr.

Regression check on B2: under ``--display=none`` the banner is
silenced even for a budget-cut run (existing silencer behaviour).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tolokaforge.dx._display import console as _shared_console
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_console_quiet() -> Any:
    """``--display=none`` sets ``_shared_console.quiet = True`` as a
    module-global side effect. Restore ``False`` between tests so a
    silenced console does not bleed into siblings' banner assertions."""
    saved = _shared_console.quiet
    yield
    _shared_console.quiet = saved


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"agent": {"provider": "openai", "name": "gpt-4"}},
                "evaluation": {
                    "output_dir": str(tmp_path / "out"),
                    "tasks_glob": str(tmp_path / "tasks" / "*"),
                },
                "orchestrator": {"repeats": 1, "auto_start_services": False},
                "compute": {"workers": 1},
            }
        )
    )
    return config_path


def _make_marker_writing_orchestrator(*, run_dir: Path, which: str) -> type:
    """Stub Orchestrator that writes ``LIMIT_HIT.json`` inside its ``run``.

    Simulates a budget-triggered graceful shutdown: the marker's
    ``which`` field drives the banner's ``stopped_reason``.
    """

    class _MarkerOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.tasks = [object()]

        def load_tasks(self) -> None:
            return None

        def run(self, *, run_id: str, output_dir: Path) -> Path:
            output_dir.mkdir(parents=True, exist_ok=True)
            marker_payload = {
                "which": which,
                "threshold": 1.0,
                "value_at_hit": 1.5,
                "timestamp": "2026-07-15T12:00:00Z",
            }
            (output_dir / "LIMIT_HIT.json").write_text(json.dumps(marker_payload))
            return output_dir

    return _MarkerOrchestrator


class TestLimitHitBannerIntegration:
    def test_cost_hit_produces_stopped_banner(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``resolve_run_directory`` names the run dir with a timestamp; the
        # stub writes the marker inside whatever the CLI passes as
        # ``output_dir``, so the CLI's own reader picks it up.
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_marker_writing_orchestrator(run_dir=tmp_path, which="cost"),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert "Run stopped (cost limit)" in result.stderr
        # Success line MUST NOT appear when the marker was written.
        assert "Run complete" not in result.stderr

    def test_time_hit_produces_stopped_banner(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_marker_writing_orchestrator(run_dir=tmp_path, which="time"),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert "Run stopped (time limit)" in result.stderr

    def test_sample_hit_produces_stopped_banner(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_marker_writing_orchestrator(run_dir=tmp_path, which="sample"),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert "Run stopped (sample limit)" in result.stderr

    def test_no_marker_produces_success_banner(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)

        class _NoMarkerOrchestrator:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.tasks = [object()]

            def load_tasks(self) -> None:
                return None

            def run(self, **_: object) -> Path:
                return expected_dir

        monkeypatch.setattr(cli_main, "Orchestrator", _NoMarkerOrchestrator)

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert "Run complete" in result.stderr
        assert "Run stopped" not in result.stderr

    def test_display_none_silences_stopped_banner_too(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B2 regression: --display=none continues to silence the stderr
        banner even when a budget cut the run short."""
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_marker_writing_orchestrator(run_dir=tmp_path, which="cost"),
        )

        result = runner.invoke(
            cli,
            ["--display=none", "run", "--config", str(valid_config)],
        )

        assert result.exit_code == 0, result.stderr
        # Under --display=none the shared console is silenced; the banner
        # writes go through it and produce no bytes.
        assert "Run stopped" not in result.stderr
