"""The separate-verifier branch of compose synthesis."""

from __future__ import annotations

from pathlib import Path

import yaml

from tolokaforge_adapter_terminal_bench.compose_synthesis import materialise_task_environment
from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

_TASK_TOML = """
[metadata]
difficulty = "medium"
tags = ["demo"]

[agent]
timeout_sec = 1800.0

[verifier]
timeout_sec = 300.0

[environment]
cpus = 2
memory_mb = 4096
"""


def _write_task(base: Path, task_id: str, *, v4: bool) -> None:
    task_dir = base / task_id
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (task_dir / "instruction.md").write_text("Do the thing.\n")
    toml = _TASK_TOML
    if v4:
        toml = 'artifacts = ["/app"]\n' + _TASK_TOML
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "Dockerfile").write_text('FROM python:3.12-slim\nCMD ["sleep", "infinity"]\n')
        (tests_dir / "test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    (task_dir / "task.toml").write_text(toml)


def _materialise(base: Path, task_id: str):
    task = discover_tasks(base)[task_id]
    env = materialise_task_environment(task, staging_root=base / "staging")
    doc = yaml.safe_load(env.compose_file.read_text())
    return env, doc


def _volume_targets(service_body: dict) -> list[str]:
    targets = []
    for mount in service_body.get("volumes", []):
        if isinstance(mount, str):
            parts = mount.split(":")
            targets.append(parts[1] if len(parts) >= 2 else parts[0])
    return targets


def test_v4_task_gets_a_verifier_service(tmp_path: Path) -> None:
    _write_task(tmp_path, "sep", v4=True)
    env, doc = _materialise(tmp_path, "sep")

    assert env.verifier_service == "verifier"
    services = doc["services"]
    assert "verifier" in services

    verifier = services["verifier"]
    assert verifier["build"] == {"context": "./tests", "dockerfile": "Dockerfile"}
    assert verifier["container_name"] == "tbench_${TOLOKAFORGE_TRIAL_SLUG}_verifier"
    assert "/logs" in _volume_targets(verifier)
    # Distinct from the agent and the engine services.
    assert env.agent_service != "verifier"
    assert "runner" not in services and "db-service" not in services

    # The agent no longer mounts /tests — the suite runs in the verifier.
    agent = services[env.agent_service]
    agent_targets = _volume_targets(agent)
    assert "/tests" not in agent_targets
    assert "/logs" in agent_targets


def test_v21_task_has_no_verifier_service(tmp_path: Path) -> None:
    _write_task(tmp_path, "plain", v4=False)
    env, doc = _materialise(tmp_path, "plain")

    assert env.verifier_service is None
    services = doc["services"]
    assert "verifier" not in services

    agent_targets = _volume_targets(services[env.agent_service])
    assert "/tests" in agent_targets
    assert "/logs" in agent_targets
