"""``--runtime remote`` end to end against a real trial node process.

A real ``python -m tolokaforge.trial_node`` process serves one native trial: the
control plane (``tolokaforge run`` with ``orchestrator.runtime: remote``) ships
the inputs over gRPC, the node runs the agent loop and grades against the local
runner, and the control plane rebuilds the reduced bundle from the returned
trajectory. The node's own fuller bundle lands under its output root.

**Gated.** Needs a Docker daemon with the core images built and a live runner
(``make docker-build-core && make docker-up``) — the node dials the runner at
``EXECUTOR_ADDRESS`` for a task that declares no per-trial stack — and a real LLM
provider key. Skip-guarded on all three; runs on Linux CI, not on every PR.

A second test ships the ``multi_service`` example, whose task declares its own
single-stack compose file, to a node that mounts the task pack at its own path:
the node rebases the compose path and materialises the three-service stack
itself, so it needs ``tolokaforge-runner:local`` and
``tolokaforge-db-service:local`` plus a pre-pulled ``nginx:1.27-alpine`` in its
own Docker daemon, and no pre-running runner.

**Cost.** One tool-use trial on a Claude Sonnet model per test — a few cents.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from tolokaforge.secrets import container_secrets_env, get_default

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_api,
    pytest.mark.requires_docker,
    pytest.mark.llm,
    pytest.mark.slow,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK = "examples/native/tool_use/dataset"
_TASK_GLOB = "**/tool_use_public_example_01/task.yaml"
_MULTI_SERVICE_DATASET = "examples/native/multi_service/dataset"
_MULTI_SERVICE_TASK = (
    _REPO_ROOT / _MULTI_SERVICE_DATASET / "tasks/multi_service/multi_service_example_01"
)
_MULTI_SERVICE_IMAGES = (
    "tolokaforge-runner:local",
    "tolokaforge-db-service:local",
    "nginx:1.27-alpine",
)
_NODE_START_TIMEOUT_S = 60.0


def _docker_running() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _images_present(images: tuple[str, ...]) -> bool:
    """True when the local Docker daemon already holds every image (the node pulls and builds none)."""
    if not shutil.which("docker"):
        return False
    try:
        return all(
            subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True, timeout=10
            ).returncode
            == 0
            for image in images
        )
    except (subprocess.TimeoutExpired, OSError):
        return False


def _runner_address() -> str:
    """``EXECUTOR_ADDRESS`` is a service address, not a secret, so a direct read is correct."""
    return os.environ.get("EXECUTOR_ADDRESS") or "localhost:50051"


def _runner_reachable() -> bool:
    host, _, port = _runner_address().partition(":")
    try:
        with socket.create_connection((host, int(port or "50051")), timeout=3):
            return True
    except (OSError, ValueError):
        return False


def _pick_provider() -> tuple[str, str] | None:
    secrets = get_default()
    if secrets.get_secret("ANTHROPIC_API_KEY"):
        return ("anthropic", "claude-sonnet-4-6")
    if secrets.get_secret("OPENROUTER_API_KEY"):
        return ("openrouter", "anthropic/claude-sonnet-4-6")
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + _NODE_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"the trial node exited early with code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    pytest.fail(f"the trial node did not listen on port {port} within {_NODE_START_TIMEOUT_S}s")


@contextmanager
def _running_node(output_root: Path, task_pack_root: Path | None = None) -> Iterator[str]:
    """A real trial node process; yields its ``host:port``."""
    port = _free_port()
    env = {
        **os.environ,
        **container_secrets_env(),
        "EXECUTOR_ADDRESS": _runner_address(),
    }
    command = [
        sys.executable,
        "-m",
        "tolokaforge.trial_node",
        "--port",
        str(port),
        "--output-root",
        str(output_root),
    ]
    if task_pack_root is not None:
        command += ["--task-pack-root", str(task_pack_root)]
    process = subprocess.Popen(command, cwd=str(_REPO_ROOT), env=env, text=True)
    try:
        _wait_for_port(port, process)
        yield f"127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def node(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A real trial node process; yields its ``host:port`` and its output root."""
    output_root = tmp_path / "node-out"
    with _running_node(output_root) as address:
        yield address, output_root


@pytest.fixture
def pack_mounting_node(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A node whose task pack is a copy of the multi_service task at the node's own path."""
    pack_root = tmp_path / "node-packs"
    shutil.copytree(_MULTI_SERVICE_TASK, pack_root / _MULTI_SERVICE_TASK.name)
    output_root = tmp_path / "node-out"
    with _running_node(output_root, pack_root) as address:
        yield address, output_root


@pytest.mark.skipif(not _docker_running(), reason="Docker daemon not available")
@pytest.mark.skipif(not _runner_reachable(), reason="No runner reachable (run `make docker-up`)")
@pytest.mark.skipif(
    _pick_provider() is None,
    reason="Neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY is set",
)
def test_a_native_trial_runs_on_a_real_node_to_a_graded_reduced_bundle(
    tmp_path: Path, node: tuple[str, Path]
) -> None:
    address, node_output_root = node
    provider, model = _pick_provider()  # type: ignore[misc]
    results = tmp_path / "results"
    config = {
        "models": {
            "agent": {"provider": provider, "name": model, "temperature": 0.0, "max_tokens": 4096},
            "user": {"provider": provider, "name": model, "temperature": 0.2},
        },
        "orchestrator": {
            "workers": 1,
            "repeats": 1,
            "runtime": "remote",
            "auto_start_services": False,
            "remote": {"nodes": [address], "lease_timeout_s": 60},
        },
        "evaluation": {
            "task_packs": [_PACK],
            "tasks_glob": _TASK_GLOB,
            "output_dir": str(results),
        },
    }
    config_path = tmp_path / "run.yaml"
    config_path.write_text(yaml.safe_dump(config))

    proc = subprocess.run(
        ["uv", "run", "tolokaforge", "run", "--config", str(config_path)],
        cwd=str(_REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    assert proc.returncode == 0, (
        f"remote run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    trajectories = list(tmp_path.glob("**/trials/*/*/trajectory.yaml"))
    control_plane = [p for p in trajectories if node_output_root not in p.parents]
    assert len(control_plane) == 1, f"expected one control-plane trial bundle, got {trajectories}"
    trial_dir = control_plane[0].parent
    assert sorted(p.name for p in trial_dir.iterdir()) == [
        "grade.yaml",
        "metrics.yaml",
        "tool_log.yaml",
        "trajectory.yaml",
    ]
    trajectory = yaml.safe_load((trial_dir / "trajectory.yaml").read_text())
    grade = yaml.safe_load((trial_dir / "grade.yaml").read_text())
    assert trajectory["status"] == "completed"
    assert grade["binary_pass"] is True, json.dumps(grade, indent=2, default=str)
    # The node persisted its own bundle before replying.
    assert any(node_output_root.glob("**/trials/*/*/trajectory.yaml"))


@pytest.mark.skipif(not _docker_running(), reason="Docker daemon not available")
@pytest.mark.skipif(
    not _images_present(_MULTI_SERVICE_IMAGES),
    reason="Needs tolokaforge-runner:local, tolokaforge-db-service:local and nginx:1.27-alpine "
    "(run `make docker-build-core` and `docker pull nginx:1.27-alpine`)",
)
@pytest.mark.skipif(
    _pick_provider() is None,
    reason="Neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY is set",
)
def test_a_task_declared_stack_is_materialised_by_the_node_from_its_own_pack_copy(
    tmp_path: Path, pack_mounting_node: tuple[str, Path]
) -> None:
    address, node_output_root = pack_mounting_node
    provider, model = _pick_provider()  # type: ignore[misc]
    results = tmp_path / "results"
    config = {
        "models": {
            "agent": {"provider": provider, "name": model, "temperature": 0.0, "max_tokens": 4096},
            "user": {"provider": provider, "name": model, "temperature": 0.2},
        },
        "orchestrator": {
            "workers": 1,
            "repeats": 1,
            "runtime": "remote",
            "auto_start_services": False,
            "remote": {"nodes": [address], "lease_timeout_s": 60},
        },
        "evaluation": {
            "projects": [_MULTI_SERVICE_DATASET],
            "tasks_glob": "tasks/multi_service/multi_service_example_01/task.yaml",
            "output_dir": str(results),
        },
    }
    config_path = tmp_path / "run.yaml"
    config_path.write_text(yaml.safe_dump(config))

    proc = subprocess.run(
        ["uv", "run", "tolokaforge", "run", "--config", str(config_path)],
        cwd=str(_REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
    )

    assert proc.returncode == 0, (
        f"remote run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    trajectories = list(tmp_path.glob("**/trials/*/*/trajectory.yaml"))
    control_plane = [p for p in trajectories if node_output_root not in p.parents]
    assert len(control_plane) == 1, f"expected one control-plane trial bundle, got {trajectories}"
    trial_dir = control_plane[0].parent
    trajectory = yaml.safe_load((trial_dir / "trajectory.yaml").read_text())
    grade = yaml.safe_load((trial_dir / "grade.yaml").read_text())
    assert trajectory["status"] == "completed"
    assert grade["binary_pass"] is True, json.dumps(grade, indent=2, default=str)
    assert any(node_output_root.glob("**/trials/*/*/trajectory.yaml"))
