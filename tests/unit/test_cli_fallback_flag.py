"""Stage-3 ``--fallback-models`` CLI flag parsing + wiring.

Locks the surface in ``tolokaforge/cli/main.py``:

* :func:`_parse_fallback_models` produces the correct ordered
  ``list[ModelConfig]`` — ``<provider>/<name>`` splits on the first
  ``/``, bare names inherit the primary agent's provider.
* Malformed input raises :class:`click.BadParameter` naming the offender.
* When the flag is set, :class:`OrchestratorDeps.agent_client_factory`
  is populated; when unset, it stays ``None``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.cli.main as cli_main
from tolokaforge.cli.main import _parse_fallback_models, cli
from tolokaforge.core.llm.fallback_client import FallbackLLMClient
from tolokaforge.core.models import ModelConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _parse_fallback_models — pure parser
# ---------------------------------------------------------------------------


class TestParseFallbackModels:
    def test_provider_slash_name_splits_on_first_slash(self) -> None:
        chain = _parse_fallback_models("a/b,c/d", default_provider="openai")
        assert [m.provider for m in chain] == ["a", "c"]
        assert [m.name for m in chain] == ["b", "d"]

    def test_bare_name_inherits_primary_provider(self) -> None:
        chain = _parse_fallback_models("gpt-4o,claude-sonnet", default_provider="openrouter")
        assert [m.provider for m in chain] == ["openrouter", "openrouter"]
        assert [m.name for m in chain] == ["gpt-4o", "claude-sonnet"]

    def test_first_slash_wins_for_openrouter_style(self) -> None:
        chain = _parse_fallback_models(
            "openrouter/anthropic/claude-sonnet-4.6", default_provider="openai"
        )
        assert chain[0].provider == "openrouter"
        assert chain[0].name == "anthropic/claude-sonnet-4.6"

    def test_whitespace_is_stripped_inside_tokens(self) -> None:
        chain = _parse_fallback_models(" a/b , c/d ", default_provider="openai")
        assert [m.provider for m in chain] == ["a", "c"]
        assert [m.name for m in chain] == ["b", "d"]

    @pytest.mark.parametrize("spec", ["", "   ", "\t\n"])
    def test_empty_spec_raises_bad_parameter(self, spec: str) -> None:
        import click

        with pytest.raises(click.BadParameter, match="empty"):
            _parse_fallback_models(spec, default_provider="openai")

    def test_empty_token_raises_bad_parameter(self) -> None:
        import click

        with pytest.raises(click.BadParameter, match="empty token"):
            _parse_fallback_models("a/b,,c/d", default_provider="openai")

    def test_slash_with_empty_name_raises_bad_parameter(self) -> None:
        import click

        with pytest.raises(click.BadParameter, match="empty model name"):
            _parse_fallback_models("openai/", default_provider="openai")


# ---------------------------------------------------------------------------
# CLI wiring — factory injection
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "agent": {"provider": "openrouter", "name": "gpt-4"},
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


class TestFallbackCliWiring:
    def test_flag_installs_agent_client_factory(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(valid_config),
                "--fallback-models",
                "openai/gpt-4o-mini,anthropic/claude-sonnet-4.6",
            ],
        )

        assert result.exit_code == 0, result.stderr
        factory = captured["deps"].agent_client_factory
        assert factory is not None
        # Invoking the factory produces a FallbackLLMClient whose chain is
        # the primary agent config plus the two parsed fallbacks.
        primary = ModelConfig(provider="openrouter", name="gpt-4")
        wrapper = factory(primary)
        assert isinstance(wrapper, FallbackLLMClient)
        assert wrapper.chain[0].name == "gpt-4"
        assert wrapper.chain[1].provider == "openai"
        assert wrapper.chain[1].name == "gpt-4o-mini"
        assert wrapper.chain[2].provider == "anthropic"
        assert wrapper.chain[2].name == "claude-sonnet-4.6"

    def test_no_flag_leaves_agent_client_factory_none(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert captured["deps"].agent_client_factory is None

    def test_empty_flag_value_exits_nonzero(
        self,
        runner: CliRunner,
        valid_config: Path,
    ) -> None:
        result = runner.invoke(cli, ["run", "--config", str(valid_config), "--fallback-models", ""])
        assert result.exit_code != 0
        assert "empty" in result.stderr.lower()

    def test_bare_name_inherits_primary_provider_via_cli(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config), "--fallback-models", "gpt-4o"],
        )

        assert result.exit_code == 0, result.stderr
        factory = captured["deps"].agent_client_factory
        assert factory is not None
        primary = ModelConfig(provider="openrouter", name="gpt-4")
        wrapper = factory(primary)
        # The bare-name fallback inherited the primary's provider ("openrouter").
        assert wrapper.chain[1].provider == "openrouter"
        assert wrapper.chain[1].name == "gpt-4o"
