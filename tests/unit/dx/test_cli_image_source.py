"""Run-CLI ``--image-source`` / ``TOLOKAFORGE_IMAGE_SOURCE`` surface.

Uses ``CliRunner`` against a stubbed :class:`Orchestrator` that
captures the ``RunConfig`` the CLI resolved so we can assert the
image-source flag / env / YAML precedence produced the correct
:attr:`RunConfig.docker.image_source`. No real LLM, Docker, or
filesystem outside the run's own output directory is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def _write_config(tmp_path: Path, docker_block: dict[str, Any] | None = None) -> Path:
    """Minimal ``run.yaml`` payload that parses. Adds an optional
    ``docker:`` block so precedence tests can seed a YAML-declared
    value and verify a CLI flag / env override wins."""
    payload: dict[str, Any] = {
        "models": {
            "agent": {"provider": "openai", "name": "gpt-4"},
            "user": {"provider": "openai", "name": "gpt-4o"},
        },
        "evaluation": {
            "output_dir": str(tmp_path / "out"),
            "tasks_glob": str(tmp_path / "tasks" / "*"),
        },
        "orchestrator": {"repeats": 1, "auto_start_services": False},
        "compute": {"workers": 1},
    }
    if docker_block is not None:
        payload["docker"] = docker_block
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
            return run_return

    return _CapturingOrchestrator


def _invoke(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_path: Path,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    expected_dir = (tmp_path / "results" / "run").resolve()
    expected_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        cli_main,
        "Orchestrator",
        _make_capturing_orchestrator(captured, run_return=expected_dir),
    )
    # Clear TOLOKAFORGE_IMAGE_SOURCE by default so a shell that has it
    # exported does not silently pollute the test — the env-precedence
    # tests below re-set it explicitly.
    monkeypatch.delenv("TOLOKAFORGE_IMAGE_SOURCE", raising=False)
    if env is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    args = ["run", "--config", str(config_path), *(extra_args or [])]
    result = runner.invoke(cli, args)
    return result, captured


class TestPrecedence:
    def test_default_is_none_and_docker_block_absent(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No flag, no env, no YAML `docker:` block — RunConfig.docker
        stays ``None``. The pull-vs-build policy in ``EngineStack``
        instantiates a default ``DockerConfig`` (image_source='auto')
        as its own fallback; leaving the block absent here confirms
        the CLI does not spuriously create it."""
        config_path = _write_config(tmp_path)

        result, captured = _invoke(runner, tmp_path, monkeypatch, config_path=config_path)

        assert result.exit_code == 0, result.stderr
        assert captured["config"].docker is None

    def test_yaml_docker_block_survives_without_override(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A YAML ``docker.image_source`` value reaches ``RunConfig``
        unchanged when no flag or env is provided."""
        config_path = _write_config(tmp_path, docker_block={"image_source": "build"})

        result, captured = _invoke(runner, tmp_path, monkeypatch, config_path=config_path)

        assert result.exit_code == 0, result.stderr
        assert captured["config"].docker is not None
        assert captured["config"].docker.image_source == "build"

    def test_env_var_overrides_yaml(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = _write_config(tmp_path, docker_block={"image_source": "build"})

        result, captured = _invoke(
            runner,
            tmp_path,
            monkeypatch,
            config_path=config_path,
            env={"TOLOKAFORGE_IMAGE_SOURCE": "pull"},
        )

        assert result.exit_code == 0, result.stderr
        assert captured["config"].docker is not None
        assert captured["config"].docker.image_source == "pull"

    def test_flag_beats_env_and_yaml(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = _write_config(tmp_path, docker_block={"image_source": "build"})

        result, captured = _invoke(
            runner,
            tmp_path,
            monkeypatch,
            config_path=config_path,
            extra_args=["--image-source", "auto"],
            env={"TOLOKAFORGE_IMAGE_SOURCE": "pull"},
        )

        assert result.exit_code == 0, result.stderr
        assert captured["config"].docker is not None
        assert captured["config"].docker.image_source == "auto"


class TestInputValidation:
    def test_bad_flag_value_click_error(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Click's ``type=Choice`` rejects unknown values before the
        run body executes."""
        config_path = _write_config(tmp_path)

        result, _ = _invoke(
            runner,
            tmp_path,
            monkeypatch,
            config_path=config_path,
            extra_args=["--image-source", "pulll"],
        )

        assert result.exit_code != 0
        assert "'pulll'" in result.stderr or "pulll" in result.output

    def test_bad_env_value_click_error(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The env var is validated by the run body (Click's Choice
        only covers the flag), so an invalid ``TOLOKAFORGE_IMAGE_SOURCE``
        must surface as a Click ``BadParameter`` and not silently pass
        through to construct_config where it would then produce a
        pydantic ValidationError with a less-obvious message."""
        config_path = _write_config(tmp_path)

        result, _ = _invoke(
            runner,
            tmp_path,
            monkeypatch,
            config_path=config_path,
            env={"TOLOKAFORGE_IMAGE_SOURCE": "puull"},
        )

        assert result.exit_code != 0
        combined = (result.output or "") + (result.stderr or "")
        assert "TOLOKAFORGE_IMAGE_SOURCE" in combined or "puull" in combined
