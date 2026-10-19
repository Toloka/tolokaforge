"""A run-level engine-stack provisioning failure is a clean operator error.

When the run-level engine/runner stack fails to come up, the composer raises a
typed :class:`~tolokaforge.core.runtime.ProvisionError` out of
``Orchestrator.run`` (via ``runtime_backend.connect`` → ``materialise_run``).
The CLI ``run`` command must surface that as a clean operator error — a
non-zero exit with an actionable message — rather than letting the exception
escape uncaught and dump a raw Python traceback.

Per-trial provisioning failures are recorded as infrastructure aborts inside
the run and never reach this boundary; this guards only the run-level path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


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


def _make_provision_failing_orchestrator() -> type:
    """Stub Orchestrator whose ``run`` fails as the run-level stack bring-up does."""

    class _ProvisionFailingOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.tasks = [object()]

        def load_tasks(self) -> None:
            return None

        def run(self, *, run_id: str, output_dir: Path) -> Path:
            raise ProvisionError(
                trial_id=run_id,
                stage="materialise_run",
                reason="docker compose up failed for stack 'engine' (scope=run): exit code 1",
            )

    return _ProvisionFailingOrchestrator


def test_run_level_provision_failure_is_a_clean_operator_error(
    runner: CliRunner,
    valid_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "Orchestrator", _make_provision_failing_orchestrator())

    result = runner.invoke(cli, ["run", "--config", str(valid_config)])

    # Non-zero exit, but the typed provisioning failure did not escape uncaught
    # (that would make it the stored exception and print a raw traceback).
    assert result.exit_code != 0
    assert not isinstance(result.exception, ProvisionError)
    # The operator sees a clean, actionable message carrying the compose error.
    assert "engine/runner stack could not be started" in result.stderr
    assert "docker compose up failed" in result.stderr
