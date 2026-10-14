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


_TIER3_TOML = (
    'artifacts = [{ source = "/app" }, { source = "/tmp/pg.dump", service = "postgres" }]\n'
    "[agent]\ntimeout_sec = 1800.0\n"
    '[verifier]\ntimeout_sec = 300.0\nenvironment_mode = "separate"\n'
    '[[verifier.collect]]\nservice = "postgres"\ncommand = "pg_dumpall > /tmp/pg.dump"\n'
    "[environment]\ncpus = 2\nmemory_mb = 4096\n"
)

_AGENT_COMPOSE = (
    "services:\n"
    "  postgres:\n    image: postgres:16\n"
    "  main:\n    environment:\n      DATABASE_URL: postgresql://u:p@postgres:5432/db\n"
    "    depends_on:\n      postgres:\n        condition: service_healthy\n"
)
_TESTS_COMPOSE = (
    "services:\n"
    "  postgres:\n    image: postgres:16\n"
    '    healthcheck:\n      test: ["CMD-SHELL", "pg_isready"]\n'
    "  main:\n    environment:\n      DATABASE_URL: postgresql://u:p@postgres:5432/db\n"
    "    depends_on:\n      postgres:\n        condition: service_healthy\n"
)


def _write_tier3_task(base: Path, task_id: str) -> None:
    task_dir = base / task_id
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (task_dir / "environment" / "docker-compose.yaml").write_text(_AGENT_COMPOSE)
    (task_dir / "instruction.md").write_text("Do the thing.\n")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "Dockerfile").write_text('FROM python:3.12-slim\nCMD ["sleep","infinity"]\n')
    (tests_dir / "test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    (tests_dir / "docker-compose.yaml").write_text(_TESTS_COMPOSE)
    (task_dir / "task.toml").write_text(_TIER3_TOML)


def test_tier3_merges_verifier_stack_namespaced_and_aliased(tmp_path: Path) -> None:
    _write_tier3_task(tmp_path, "dbtask")
    env, doc = _materialise(tmp_path, "dbtask")
    svcs = doc["services"]
    # Agent stack keeps its own un-namespaced services; the verifier stack is
    # merged under a verifier_ prefix — no key collision on postgres/main.
    assert {"main", "postgres", "verifier_main", "verifier_postgres"} <= set(svcs)
    assert env.verifier_service == "verifier_main"
    # The verifier main builds tests/Dockerfile, lives on the verifier network,
    # and depends on the namespaced sidecar.
    assert svcs["verifier_main"]["build"]["context"] == "./tests"
    assert svcs["verifier_main"]["networks"] == {"tb4_verifier": {}}
    assert svcs["verifier_main"]["depends_on"] == {
        "verifier_postgres": {"condition": "service_healthy"}
    }
    # The verifier sidecar carries an alias of its original name so the main's
    # hard-coded `-h postgres` resolves to it, not the agent's postgres.
    assert svcs["verifier_postgres"]["networks"] == {"tb4_verifier": {"aliases": ["postgres"]}}
    assert svcs["verifier_postgres"]["image"] == "postgres:16"
    # The agent's own postgres stays on the default network (no networks key).
    assert "networks" not in svcs["postgres"]
    assert (doc.get("networks") or {}).get("tb4_verifier") == {}


def test_tier1_has_no_verifier_stack(tmp_path: Path) -> None:
    _write_task(tmp_path, "plain", v4=True)
    env, doc = _materialise(tmp_path, "plain")
    # The single-container shape keeps the lone `verifier` service, no stack.
    assert env.verifier_service == "verifier"
    assert "verifier" in doc["services"]
    assert not any(k.startswith("verifier_") for k in doc["services"])
    assert "tb4_verifier" not in (doc.get("networks") or {})


_AGENT_COMPOSE_VOL = (
    "services:\n"
    "  postgres:\n    image: postgres:16\n"
    '    volumes: ["pgdata:/var/lib/postgresql/data"]\n'
    "  main:\n    environment:\n      DATABASE_URL: postgresql://u:p@postgres:5432/db\n"
    "volumes:\n  pgdata:\n"
)
_TESTS_COMPOSE_VOL = (
    "services:\n"
    "  postgres:\n    image: postgres:16\n"
    '    volumes: ["pgdata:/var/lib/postgresql/data"]\n'
    '    healthcheck:\n      test: ["CMD-SHELL", "pg_isready"]\n'
    '  cache:\n    image: redis:7-alpine\n    volumes: ["shared:/data"]\n'
    "  main:\n    environment:\n      DATABASE_URL: postgresql://u:p@postgres:5432/db\n"
    '    volumes: ["shared:/tb4/redis_cache"]\n'
    "    depends_on:\n      postgres:\n        condition: service_healthy\n"
    "volumes:\n  pgdata:\n  shared:\n"
)


def test_tier3_named_volumes_are_namespaced_without_agent_collision(tmp_path: Path) -> None:
    task_dir = tmp_path / "voltask"
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (task_dir / "environment" / "docker-compose.yaml").write_text(_AGENT_COMPOSE_VOL)
    (task_dir / "instruction.md").write_text("Do the thing.\n")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "Dockerfile").write_text('FROM python:3.12-slim\nCMD ["sleep","infinity"]\n')
    (tests_dir / "test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    (tests_dir / "docker-compose.yaml").write_text(_TESTS_COMPOSE_VOL)
    (task_dir / "task.toml").write_text(_TIER3_TOML)

    _env, doc = _materialise(tmp_path, "voltask")
    svcs = doc["services"]
    vols = doc.get("volumes") or {}

    # Top-level named volumes: the agent's pgdata stays un-namespaced, the verifier
    # stack's get the verifier_ prefix — no collision collapsing them into one.
    assert "pgdata" in vols  # agent's own
    assert "verifier_pgdata" in vols and "verifier_shared" in vols

    # Agent postgres keeps its own volume; the verifier postgres points at the
    # namespaced one (its own data dir, not the agent's).
    assert _volume_sources(svcs["postgres"]) == ["pgdata"]
    assert _volume_sources(svcs["verifier_postgres"]) == ["verifier_pgdata"]

    # The redis shared volume stays SHARED between verifier_main and verifier_cache
    # (both reference verifier_shared), just isolated from the agent stack.
    assert "verifier_shared" in _volume_sources(svcs["verifier_main"])
    assert "verifier_shared" in _volume_sources(svcs["verifier_cache"])


def _volume_sources(service_body: dict) -> list[str]:
    """Named-volume source names in a service's volumes (skips bind mounts)."""
    out = []
    for v in service_body.get("volumes") or []:
        src = (
            v.split(":")[0]
            if isinstance(v, str)
            else (v.get("source") if isinstance(v, dict) else None)
        )
        if src and not src.startswith((".", "/")):
            out.append(src)
    return out
