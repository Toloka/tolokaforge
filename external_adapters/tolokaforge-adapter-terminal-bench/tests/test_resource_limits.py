"""The CPU and memory caps a task's ``[environment]`` declares reach its agent container.

Without a cap the agent's processes share the trial's machine with dockerd and
the tool runner. An agent that builds at the machine's full width (``nproc``
counts the host's CPUs, not the trial's) can then starve both: the runner stops
answering, and every later tool call fails for a reason that is not the agent's.
Upstream caps the task's ``main`` container with top-level ``cpus`` and
``mem_limit``, and only for the fields the task declares; these tests pin the
same contract on the adapter's synthesised compose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from tolokaforge_adapter_terminal_bench.compose_synthesis import materialise_task_environment
from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

pytestmark = pytest.mark.unit

_SEPARATE_VERIFIER = '[verifier]\ntimeout_sec = 300.0\nenvironment_mode = "separate"\n'


def _write_task(
    base: Path,
    *,
    environment: str,
    agent_compose: str | None = None,
    separate_verifier: bool = False,
) -> None:
    task_dir = base / "task"
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    if agent_compose is not None:
        (task_dir / "environment" / "docker-compose.yaml").write_text(agent_compose)
    (task_dir / "instruction.md").write_text("Do the thing.\n")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    verifier = "[verifier]\ntimeout_sec = 300.0\n"
    if separate_verifier:
        (tests_dir / "Dockerfile").write_text('FROM python:3.12-slim\nCMD ["sleep", "infinity"]\n')
        verifier = 'artifacts = ["/app"]\n' + _SEPARATE_VERIFIER
    (task_dir / "task.toml").write_text(
        f"{verifier}[agent]\ntimeout_sec = 1800.0\n[environment]\n{environment}"
    )


def _services(base: Path) -> dict[str, Any]:
    env = materialise_task_environment(discover_tasks(base)["task"], staging_root=base / "staging")
    return yaml.safe_load(env.compose_file.read_text())["services"]


@pytest.mark.parametrize("separate_verifier", [False, True], ids=["single", "separate-verifier"])
def test_declared_cpus_and_memory_cap_the_agent_container(
    tmp_path: Path, separate_verifier: bool
) -> None:
    _write_task(
        tmp_path, environment="cpus = 4\nmemory_mb = 12288\n", separate_verifier=separate_verifier
    )

    services = _services(tmp_path)

    assert services["main"]["cpus"] == 4.0
    assert services["main"]["mem_limit"] == "12288m"
    for name, body in services.items():
        if name != "main":
            assert "cpus" not in body and "mem_limit" not in body, name


def test_an_undeclared_field_caps_nothing(tmp_path: Path) -> None:
    _write_task(tmp_path, environment="memory_mb = 2048\n")

    main = _services(tmp_path)["main"]

    assert "cpus" not in main
    assert main["mem_limit"] == "2048m"


def test_a_task_declaring_neither_runs_uncapped(tmp_path: Path) -> None:
    _write_task(tmp_path, environment="")

    main = _services(tmp_path)["main"]

    assert "cpus" not in main and "mem_limit" not in main


def test_the_task_compose_keeps_its_own_agent_limits(tmp_path: Path) -> None:
    _write_task(
        tmp_path,
        environment="cpus = 4\nmemory_mb = 12288\n",
        agent_compose="services:\n  main:\n    mem_limit: 6g\n",
    )

    main = _services(tmp_path)["main"]

    assert main["mem_limit"] == "6g"
    assert main["cpus"] == 4.0


def test_deploy_limits_in_the_task_compose_win_whole(tmp_path: Path) -> None:
    _write_task(
        tmp_path,
        environment="cpus = 4\nmemory_mb = 12288\n",
        agent_compose=(
            "services:\n  main:\n    deploy:\n      resources:\n        limits:\n"
            "          memory: 6G\n"
        ),
    )

    main = _services(tmp_path)["main"]

    assert "cpus" not in main and "mem_limit" not in main
    assert main["deploy"]["resources"]["limits"] == {"memory": "6G"}


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        pytest.param("memory_mb = 0\n", "greater than zero", id="zero-memory"),
        pytest.param("cpus = -1\n", "greater than zero", id="negative-cpus"),
        pytest.param('cpus = "4"\n', "must be a number", id="cpus-as-text"),
        pytest.param("memory_mb = 1024.5\n", "whole number", id="fractional-memory"),
    ],
)
def test_an_invalid_declaration_is_refused_at_parse(
    tmp_path: Path, environment: str, message: str
) -> None:
    _write_task(tmp_path, environment=environment)

    with pytest.raises(ValueError, match=message):
        discover_tasks(tmp_path)
