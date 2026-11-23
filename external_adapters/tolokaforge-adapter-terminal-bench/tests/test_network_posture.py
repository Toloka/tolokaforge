"""The network posture a task's declared ``network_mode`` resolves to.

A pack's ``network_mode = "no-network"`` reaches the engine as
``EnvironmentPatch.network_policy``, the manifest's only network-posture
surface (ADR-0009). The multi-container case runs the engine's own
``enforce_network_policy`` over the adapter's own synthesised compose, so the
topology a declaring task comes up on is pinned here rather than inferred.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter
from tolokaforge_adapter_terminal_bench.compose_synthesis import materialise_task_environment
from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

from tolokaforge.core.compose_materialisation import enforce_network_policy
from tolokaforge.core.netpolicy_constants import (
    NETPOLICY_EDGE_NETWORK,
    NETPOLICY_INTERNAL_NETWORK,
)
from tolokaforge.runner.models import NetworkPolicy

pytestmark = pytest.mark.unit

_AGENT_COMPOSE_WITH_SIBLING = (
    "services:\n"
    "  postgres:\n    image: postgres:16\n"
    "  main:\n    environment:\n      DATABASE_URL: postgresql://u:p@postgres:5432/db\n"
    "    depends_on:\n      postgres:\n        condition: service_healthy\n"
)


def _write_task(
    base: Path,
    task_id: str,
    *,
    environment_keys: str = "",
    verifier_keys: str = "",
    verifier_environment_keys: str = "",
    agent_compose: str | None = None,
    tests_compose: str | None = None,
) -> None:
    """A separate-verifier pack whose ``network_mode`` declarations vary."""
    task_dir = base / task_id
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    if agent_compose is not None:
        (task_dir / "environment" / "docker-compose.yaml").write_text(agent_compose)
    (task_dir / "instruction.md").write_text("Do the thing.\n")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "Dockerfile").write_text('FROM python:3.12-slim\nCMD ["sleep", "infinity"]\n')
    (tests_dir / "test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    if tests_compose is not None:
        (tests_dir / "docker-compose.yaml").write_text(tests_compose)
    verifier_environment = (
        f"[verifier.environment]\n{verifier_environment_keys}" if verifier_environment_keys else ""
    )
    (task_dir / "task.toml").write_text(
        'artifacts = ["/app"]\n'
        "[agent]\ntimeout_sec = 1800.0\n"
        '[verifier]\ntimeout_sec = 300.0\nenvironment_mode = "separate"\n'
        f"{verifier_keys}"
        f"{verifier_environment}"
        f"[environment]\ncpus = 2\nmemory_mb = 4096\n{environment_keys}"
    )


def _adapter(base: Path, **params: object) -> TerminalBenchAdapter:
    return TerminalBenchAdapter(
        {"terminal_bench_dir": str(base), "prebuild_images": False, **params}
    )


def _resolved_policy(base: Path, task_id: str, **params: object) -> NetworkPolicy:
    patch = _adapter(base, **params).get_task(task_id).environment_manifest
    assert patch is not None
    return patch.network_policy


@pytest.mark.parametrize(
    ("environment_keys", "verifier_keys", "expected"),
    [
        pytest.param(
            'network_mode = "no-network"\n',
            'network_mode = "no-network"\n',
            NetworkPolicy.NO_INTERNET,
            id="both-sides-declare-no-network",
        ),
        pytest.param(
            'network_mode = "no-network"\n',
            "",
            NetworkPolicy.NO_INTERNET,
            id="agent-alone-declares-no-network",
        ),
        pytest.param(
            "",
            'network_mode = "no-network"\n',
            NetworkPolicy.NO_INTERNET,
            id="verifier-alone-declares-no-network",
        ),
        pytest.param(
            'network_mode = "default"\n',
            'network_mode = "default"\n',
            NetworkPolicy.FULL_INTERNET,
            id="default-keeps-the-run-level-policy",
        ),
        pytest.param(
            'network_mode = "bridge"\n',
            "",
            NetworkPolicy.FULL_INTERNET,
            id="bridge-keeps-the-run-level-policy",
        ),
        pytest.param(
            "",
            "",
            NetworkPolicy.FULL_INTERNET,
            id="no-declaration-keeps-the-run-level-policy",
        ),
    ],
)
def test_declared_network_mode_resolves_the_stack_policy(
    tmp_path: Path,
    environment_keys: str,
    verifier_keys: str,
    expected: NetworkPolicy,
) -> None:
    _write_task(tmp_path, "task", environment_keys=environment_keys, verifier_keys=verifier_keys)

    assert _resolved_policy(tmp_path, "task") is expected


def test_nested_verifier_environment_declaration_is_read(tmp_path: Path) -> None:
    """Packs split between ``[verifier]`` and the nested ``[verifier.environment]``."""
    _write_task(tmp_path, "task", verifier_environment_keys='network_mode = "no-network"\n')

    assert _resolved_policy(tmp_path, "task") is NetworkPolicy.NO_INTERNET


@pytest.mark.parametrize(
    "environment_keys",
    [
        pytest.param("", id="undeclared"),
        pytest.param('network_mode = "default"\n', id="default"),
        pytest.param('network_mode = "bridge"\n', id="bridge"),
    ],
)
def test_a_non_isolating_task_keeps_a_stricter_run_level_policy(
    tmp_path: Path, environment_keys: str
) -> None:
    """The declaration is a floor under the operator's knob, not a replacement for it.

    A task asking for no isolation does not take the operator's isolation away:
    against a run-level ``no_internet`` the stack still comes up sealed.
    """
    _write_task(tmp_path, "task", environment_keys=environment_keys)

    policy = _resolved_policy(tmp_path, "task", network_policy=NetworkPolicy.NO_INTERNET.value)

    assert policy is NetworkPolicy.NO_INTERNET


def test_no_network_refuses_a_coding_harness_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness CLI authenticates with its provider from inside the sealed container."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-key")
    _write_task(tmp_path, "task", environment_keys='network_mode = "no-network"\n')
    adapter = _adapter(tmp_path, agent_harness="claude-code", agent_model="anthropic/stub")

    with pytest.raises(ValueError) as excinfo:
        adapter.get_task("task")

    message = str(excinfo.value)
    assert "'task'" in message
    assert "claude-code" in message


def test_no_network_refuses_a_verifier_stack_by_name(tmp_path: Path) -> None:
    """A task-authored verifier stack is a topology this adapter cannot isolate.

    Isolation is one policy over the whole merged compose file, so the engine
    cannot seal the agent's half while leaving the suite's own services on the
    terms their author wrote. Refused rather than scored under a posture
    neither half was authored for.
    """
    _write_task(
        tmp_path,
        "task",
        environment_keys='network_mode = "no-network"\n',
        agent_compose=_AGENT_COMPOSE_WITH_SIBLING,
        tests_compose=_AGENT_COMPOSE_WITH_SIBLING,
    )
    adapter = _adapter(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        adapter.get_task("task")

    message = str(excinfo.value)
    assert "'task'" in message
    assert "tests/docker-compose.yaml" in message


def test_no_network_keeps_a_multi_container_task_reachable(tmp_path: Path) -> None:
    """The agent keeps its own sidecars; only egress goes.

    Isolation is the engine's ``network_policy`` and never a compose
    ``network_mode: none`` on the agent service: that detaches the container
    from every network including compose's implicit default, so an agent that
    could no longer reach its own database would score 0.0 with the stack
    reporting healthy.
    """
    _write_task(
        tmp_path,
        "task",
        environment_keys='network_mode = "no-network"\n',
        agent_compose=_AGENT_COMPOSE_WITH_SIBLING,
    )
    policy = _resolved_policy(tmp_path, "task")
    env = materialise_task_environment(
        discover_tasks(tmp_path)["task"], staging_root=tmp_path / "staging"
    )
    doc = yaml.safe_load(env.compose_file.read_text())

    isolated = enforce_network_policy(doc, policy, "runner", [])

    services = isolated["services"]
    assert not any("network_mode" in body for body in services.values())
    for name in ("main", "postgres", "verifier"):
        assert services[name]["networks"] == [NETPOLICY_INTERNAL_NETWORK], name
    assert isolated["networks"][NETPOLICY_INTERNAL_NETWORK] == {"internal": True}
    assert NETPOLICY_EDGE_NETWORK in isolated["networks"]
