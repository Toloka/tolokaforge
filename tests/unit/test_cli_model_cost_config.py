"""Stage-3 ``--model-cost-config`` overlay wiring.

Locks the surface in ``tolokaforge/cli/main.py``:

* ``--model-cost-config <path>`` calls
  :func:`pricing.reload_pricing` with the overlay path BEFORE the
  :class:`Orchestrator` is constructed.
* After the CLI exits, the mutation to :data:`MODEL_PRICING` is
  observable via :func:`estimate_cost` (module-global side effect —
  matches the pre-existing :func:`reload_pricing` semantics).
* A non-existent path is rejected by ``click.Path(exists=True)`` before
  the callback runs.
* YAML and JSON overlays are both accepted (suffix-detected inside
  :func:`reload_pricing`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.cli.main as cli_main
from tolokaforge.cli.main import cli
from tolokaforge.core import pricing

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


@pytest.fixture
def restore_pricing() -> Any:
    """Reload the pristine shipped pricing table after each test so
    module-global mutations don't leak into sibling tests."""
    yield
    pricing.reload_pricing()


def _make_stub_orchestrator(run_return: Path) -> type:
    class _StubOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.tasks = [object()]

        def load_tasks(self) -> None:
            return None

        def run(self, **_: object) -> Path:
            return run_return

    return _StubOrchestrator


class TestModelCostConfigOverlay:
    def test_yaml_overlay_is_applied_before_orchestrator(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_pricing: Any,
    ) -> None:
        overlay = tmp_path / "prices.yaml"
        overlay.write_text(
            yaml.safe_dump(
                {
                    "models": {
                        "synthetic/test-model": {"input": 1.0, "output": 2.0},
                    }
                }
            )
        )
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_stub_orchestrator(expected_dir),
        )

        result = runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(valid_config),
                "--model-cost-config",
                str(overlay),
            ],
        )

        assert result.exit_code == 0, result.stderr
        # The overlay's model is now priced — pre-overlay, `synthetic/test-model`
        # was unknown so `estimate_cost` returned None.
        cost = pricing.estimate_cost(
            "synthetic/test-model", input_tokens=1_000_000, output_tokens=0
        )
        assert cost is not None
        assert cost == pytest.approx(1.0)

    def test_json_overlay_is_applied_before_orchestrator(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_pricing: Any,
    ) -> None:
        overlay = tmp_path / "prices.json"
        overlay.write_text(
            json.dumps(
                {
                    "models": {
                        "synthetic/json-model": {"input": 3.0, "output": 5.0},
                    }
                }
            )
        )
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_stub_orchestrator(expected_dir),
        )

        result = runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(valid_config),
                "--model-cost-config",
                str(overlay),
            ],
        )

        assert result.exit_code == 0, result.stderr
        cost = pricing.estimate_cost(
            "synthetic/json-model", input_tokens=1_000_000, output_tokens=1_000_000
        )
        assert cost == pytest.approx(8.0)

    def test_nonexistent_path_is_rejected_before_callback(
        self,
        runner: CliRunner,
        valid_config: Path,
    ) -> None:
        result = runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(valid_config),
                "--model-cost-config",
                "/nonexistent/path.yaml",
            ],
        )
        assert result.exit_code != 0
        assert "does not exist" in result.stderr.lower()
