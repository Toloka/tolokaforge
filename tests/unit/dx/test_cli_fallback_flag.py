"""``models.agent.fallbacks`` config-driven fallback chain.

Locks the surface in ``tolokaforge/dx/cli/main.py``:

* When the run config declares ``models.agent.fallbacks: [...]``,
  :class:`OrchestratorDeps.agent_client_factory` is populated with a
  factory that wraps the primary agent config in a
  :class:`FallbackLLMClient`.
* When the field is absent or empty, the factory stays ``None`` and the
  orchestrator constructs a bare :class:`LLMClient`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tolokaforge.core.llm.fallback_client import FallbackLLMClient
from tolokaforge.core.models import ModelConfig
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def _config_with_fallbacks(tmp_path: Path, fallbacks: list[dict]) -> Path:
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "agent": {
                        "provider": "openrouter",
                        "name": "gpt-4",
                        "fallbacks": fallbacks,
                    },
                },
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


def _make_capturing_orchestrator(
    captured: dict[str, Any],
    *,
    run_return: Path,
) -> type:
    class _CapturingOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["deps"] = kwargs.get("deps")
            self.tasks = [object()]

        def load_tasks(self) -> None:
            return None

        def run(self, **_: object) -> Path:
            return run_return

    return _CapturingOrchestrator


class TestFallbackFromConfig:
    def test_fallbacks_installs_agent_client_factory(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = _config_with_fallbacks(
            tmp_path,
            [
                {"provider": "openai", "name": "gpt-4o-mini"},
                {"provider": "anthropic", "name": "claude-sonnet-4.6"},
            ],
        )
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(config_path)])

        assert result.exit_code == 0, result.stderr
        factory = captured["deps"].agent_client_factory
        assert factory is not None
        # Invoking the factory produces a FallbackLLMClient whose chain is
        # the primary agent config plus the two config-declared fallbacks.
        primary = ModelConfig(provider="openrouter", name="gpt-4")
        wrapper = factory(primary)
        assert isinstance(wrapper, FallbackLLMClient)
        assert wrapper.chain[0].name == "gpt-4"
        assert wrapper.chain[1].provider == "openai"
        assert wrapper.chain[1].name == "gpt-4o-mini"
        assert wrapper.chain[2].provider == "anthropic"
        assert wrapper.chain[2].name == "claude-sonnet-4.6"

    def test_no_fallbacks_leaves_agent_client_factory_none(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No ``fallbacks`` field at all.
        config_path = tmp_path / "run.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "models": {"agent": {"provider": "openrouter", "name": "gpt-4"}},
                    "evaluation": {
                        "output_dir": str(tmp_path / "out"),
                        "tasks_glob": str(tmp_path / "tasks" / "*"),
                    },
                    "orchestrator": {"repeats": 1, "auto_start_services": False},
                    "compute": {"workers": 1},
                }
            )
        )
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(config_path)])

        assert result.exit_code == 0, result.stderr
        assert captured["deps"].agent_client_factory is None

    def test_empty_fallbacks_list_leaves_factory_none(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit ``fallbacks: []`` is treated the same as absent —
        no factory installed, orchestrator builds a bare LLMClient."""
        config_path = _config_with_fallbacks(tmp_path, [])
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(config_path)])

        assert result.exit_code == 0, result.stderr
        assert captured["deps"].agent_client_factory is None
