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

from tests.task_scaffold import write_task

pytestmark = pytest.mark.unit


def _services(base: Path) -> dict[str, Any]:
    env = materialise_task_environment(discover_tasks(base)["task"], staging_root=base / "staging")
    return yaml.safe_load(env.compose_file.read_text())["services"]


@pytest.mark.parametrize("separate_verifier", [False, True], ids=["single", "separate-verifier"])
def test_declared_cpus_and_memory_cap_the_agent_container(
    tmp_path: Path, separate_verifier: bool
) -> None:
    write_task(
        tmp_path, environment="cpus = 4\nmemory_mb = 12288\n", separate_verifier=separate_verifier
    )

    services = _services(tmp_path)

    assert services["main"]["cpus"] == 4
    assert services["main"]["mem_limit"] == "12288m"
    for name, body in services.items():
        if name != "main":
            assert "cpus" not in body and "mem_limit" not in body, name


def test_an_undeclared_field_caps_nothing(tmp_path: Path) -> None:
    write_task(tmp_path, environment="memory_mb = 2048\n", separate_verifier=False)

    main = _services(tmp_path)["main"]

    assert "cpus" not in main
    assert main["mem_limit"] == "2048m"


def test_a_task_declaring_neither_runs_uncapped(tmp_path: Path) -> None:
    write_task(tmp_path, environment="", separate_verifier=False)

    main = _services(tmp_path)["main"]

    assert "cpus" not in main and "mem_limit" not in main


def test_the_task_compose_keeps_its_own_agent_limits(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        environment="cpus = 4\nmemory_mb = 12288\n",
        agent_compose="services:\n  main:\n    mem_limit: 6g\n",
        separate_verifier=False,
    )

    main = _services(tmp_path)["main"]

    assert main["mem_limit"] == "6g"
    assert main["cpus"] == 4


def test_deploy_limits_in_the_task_compose_win_whole(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        environment="cpus = 4\nmemory_mb = 12288\n",
        agent_compose=(
            "services:\n  main:\n    deploy:\n      resources:\n        limits:\n"
            "          memory: 6G\n"
        ),
        separate_verifier=False,
    )

    main = _services(tmp_path)["main"]

    assert "cpus" not in main and "mem_limit" not in main
    assert main["deploy"]["resources"]["limits"] == {"memory": "6G"}


def test_the_substituted_values_follow_the_declaration(tmp_path: Path) -> None:
    """``${CPUS}`` / ``${MEMORY}`` read the same validated fields the caps do, with
    the defaults only for a task that declares none."""
    write_task(
        tmp_path, "declared", environment="cpus = 4\nmemory_mb = 12288\n", separate_verifier=False
    )
    write_task(tmp_path, "undeclared", environment="", separate_verifier=False)

    tasks = discover_tasks(tmp_path)

    assert (tasks["declared"].cpus, tasks["declared"].memory_mb) == (4, 12288)
    assert (tasks["undeclared"].cpus, tasks["undeclared"].memory_mb) == (2, 4096)


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
    write_task(tmp_path, environment=environment, separate_verifier=False)

    with pytest.raises(ValueError, match=message):
        discover_tasks(tmp_path)
