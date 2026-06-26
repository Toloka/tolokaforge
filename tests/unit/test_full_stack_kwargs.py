"""``full_stack`` must accept and forward every kwarg ``core_stack`` does.

The orchestrator passes the same kwargs (including ``enable_playwright``,
``task_pack_mounts``, ``extra_runner_binds``, ``mount_docker_socket``,
``enable_dind``) regardless of which stack factory it picks. Without this,
mobile/browser tasks (which trigger the full_stack switch) would lose
Playwright + bind mounts and fail at first action.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.docker.stacks.core import core_stack
from tolokaforge.docker.stacks.full import full_stack

pytestmark = pytest.mark.unit


def _runner_def(stack):
    runner = stack.services.get("runner")
    assert runner is not None, "runner ServiceDefinition not found in stack"
    return runner


def test_core_stack_runner_omits_rag_service_url():
    """The core stack provisions no rag-service, so the runner container must
    NOT carry ``RAG_SERVICE_URL``. An honest absence is load-bearing: the
    runner only builds a RAG client (and the judge only gets a ``search_kb``
    tool) when this var is present, so a stray value here would point the
    judge at a host that does not exist on this stack — it would then grade
    silently without reading the KB it grades against (issue #95)."""
    runner = _runner_def(core_stack())
    assert "RAG_SERVICE_URL" not in runner.environment


def test_full_stack_runner_injects_rag_service_url():
    """The full stack DOES run a rag-service, so the runner container carries
    ``RAG_SERVICE_URL`` pointing at it. This is what makes the agent's
    full-stack RAG client non-None and the judge's search_kb reachable."""
    runner = _runner_def(full_stack())
    assert runner.environment.get("RAG_SERVICE_URL") == "http://tolokaforge-rag-service:8001"


def test_full_stack_includes_db_runner_and_extras():
    stack = full_stack()
    services = set(stack.services.keys())
    assert {"db-service", "runner", "rag-service", "mock-web"}.issubset(services)


def test_full_stack_forwards_enable_playwright():
    stack = full_stack(enable_playwright=True)
    runner = _runner_def(stack)
    assert runner.build_args.get("INSTALL_PLAYWRIGHT") == "true"


def test_full_stack_forwards_extra_runner_binds(tmp_path: Path):
    bind_src = tmp_path / "logs"
    bind_src.mkdir()
    stack = full_stack(extra_runner_binds=[(bind_src, "/var/log/runner")])
    runner = _runner_def(stack)
    bind_targets = [m.target for m in runner.mounts]
    assert "/var/log/runner" in bind_targets


def test_full_stack_forwards_task_pack_mounts(tmp_path: Path):
    pack = tmp_path / "tasks"
    pack.mkdir()
    stack = full_stack(task_pack_mounts=[pack])
    runner = _runner_def(stack)
    abs_pack = str(pack.resolve())
    bind_targets = [m.target for m in runner.mounts]
    assert abs_pack in bind_targets


def test_full_stack_forwards_mount_docker_socket():
    stack = full_stack(mount_docker_socket=True)
    runner = _runner_def(stack)
    bind_targets = [m.target for m in runner.mounts]
    assert "/var/run/docker.sock" in bind_targets


def test_full_stack_default_omits_playwright():
    stack = full_stack()
    runner = _runner_def(stack)
    assert "INSTALL_PLAYWRIGHT" not in runner.build_args


def test_full_stack_mock_web_pins_build_context():
    """``mock-web`` declares ``context_files`` so its image-content-hash
    only depends on the service's own source. Without this the orchestrator
    hashes the whole repo and the cache fires on every unrelated edit
    (observed during PR #127 verification — repeated 10+ minute rebuilds
    triggered by changes that mock-web's Dockerfile never COPYs in).
    """
    stack = full_stack()
    mock_web = stack.services.get("mock-web")
    assert mock_web is not None
    assert mock_web.context_files == ["tolokaforge/env/mock_web_service/"]


def test_full_stack_rag_service_pins_build_context():
    """``rag-service`` context_files contains the resolved wheel (for
    ``import tolokaforge.secrets``) and the service's own directory."""
    stack = full_stack()
    rag_service = stack.services.get("rag-service")
    assert rag_service is not None
    assert len(rag_service.context_files) == 2
    assert rag_service.context_files[0].endswith(".whl")
    assert rag_service.context_files[1] == "tolokaforge/env/rag_service/"
