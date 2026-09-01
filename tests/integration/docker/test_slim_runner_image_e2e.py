"""End-to-end behaviour locks for the slim multi-stage runner image (#539).

The runner image installs the wheel + ``[runner]`` extra into an isolated venv,
strips the pip/setuptools toolchain, and keeps the docker CLI out of the default
image (opt-in, auto-enabled only for terminal-bench runs). These locks prove the
slimming did not break the image's function:

- it boots and every domain-tool driver imports inside the container;
- the docker CLI is absent from the default image;
- it serves gRPC and reports healthy;
- shared-stack and per-trial real-agent runs pass end-to-end against it;
- a ``tolokaforge run-trial`` subprocess drives a real trial to completion against
  it — the one path none of the #536–#539 tests exercise together (#538
  subprocess entry + #539 slim image + #536 ``shared`` resolution + #537
  composition inside the subprocess).

The first three need only Docker (no LLM key); the last three are real-agent runs
gated on Docker + a reachable runner + a provider key, mirroring
``tests/integration/test_run_trial_e2e.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.utils.docker_helpers import current_runner_image_id, is_docker_daemon_available

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Every domain-tool driver the [runner] extra ships. Imported inside the
# container to prove the venv + --no-compile + toolchain strip left them
# functional (the runtime-side companion to the Stage-2 canonical declaration
# lock in tests/canonical/test_runner_image_db_driver_canon.py).
_DOMAIN_DEP_IMPORTS = "import asyncpg, sqlalchemy, alembic, jose, fastapi, uvicorn, odata_query"


@pytest.fixture
def runner_image_id() -> str:
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
    image_id = current_runner_image_id()
    if image_id is None:
        pytest.skip("tolokaforge-runner image not built")
    return image_id


def test_slim_runner_image_boots_and_imports_domain_deps(runner_image_id: str) -> None:
    """The runner entrypoint graph and every domain driver import in-container."""
    probe = f"import tolokaforge.runner.__main__; {_DOMAIN_DEP_IMPORTS}; print('OK')"
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", runner_image_id, "-c", probe],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"boot/import probe failed:\n{result.stderr}"
    assert "OK" in result.stdout


def test_slim_runner_image_omits_docker_cli_by_default(runner_image_id: str) -> None:
    """The default image ships without the docker CLI (opt-in via INSTALL_DOCKER_CLI)."""
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", runner_image_id, "-c", "command -v docker"],
        capture_output=True,
        text=True,
    )
    message = f"docker CLI unexpectedly present in default runner image: {result.stdout!r}"
    assert result.returncode != 0, message


def test_slim_runner_image_domain_db_probe_functions(runner_image_id: str) -> None:
    """The domain DB driver functions in-container: :func:`_fetch_probe_rows`
    runs its lazy ``import asyncpg`` and reaches a real connection attempt.

    Points the helper at an unreachable DSN and asserts asyncpg surfaces a
    connection failure — not a ``ModuleNotFoundError``. The distinction is the
    lock: if asyncpg were missing from the slim image the helper would raise
    an import error; instead it drives the driver far enough to fail on the
    network, proving the domain driver ships and functions (the functional
    companion to the canonical driver-declaration lock at
    ``tests/canonical/test_runner_image_db_driver_canon.py``).
    """
    probe_script = (
        "import asyncio, json\n"
        "from tolokaforge.core.grading.db_probes import _fetch_probe_rows\n"
        "try:\n"
        "    asyncio.run(_fetch_probe_rows('postgresql://u:p@127.0.0.1:1/db', 'SELECT 1'))\n"
        "    print(json.dumps({'reasons': 'unexpected connect success'}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'reasons': f'{type(exc).__name__}: {exc}'}))\n"
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", runner_image_id, "-c", probe_script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"db-probe probe failed:\n{result.stderr}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    reasons = payload["reasons"]
    assert "ModuleNotFoundError" not in reasons, payload
    assert "No module named" not in reasons, payload
    connect_tokens = ("ConnectionRefused", "ConnectionError", "OSError")
    assert any(token in reasons for token in connect_tokens), payload


def test_slim_runner_image_serves_grpc(runner_container) -> None:
    """The slim image boots and reports healthy over gRPC (no LLM key needed)."""
    from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient

    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = GrpcRunnerClient(f"{host}:{port}")
    client.connect(timeout=10)
    try:
        health = client.health_check_detailed()
    finally:
        client.close()
    assert health["status"] == "healthy", f"runner not healthy: {health}"
    assert health["db_service_connected"], "DB service not connected"


def _docker_running() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _runner_reachable() -> bool:
    address = os.environ.get("EXECUTOR_ADDRESS") or "localhost:50051"
    host, _, port = address.partition(":")
    try:
        with socket.create_connection((host, int(port or "50051")), timeout=3):
            return True
    except (OSError, ValueError):
        return False


def _pick_provider() -> tuple[str, str] | None:
    """(provider, model) whose credential is available via ``SecretManager``."""
    from tolokaforge.secrets import get_default

    secrets = get_default()
    if secrets.get_secret("ANTHROPIC_API_KEY"):
        return ("anthropic", "claude-sonnet-4-6")
    if secrets.get_secret("OPENROUTER_API_KEY"):
        return ("openrouter", "anthropic/claude-sonnet-4-6")
    return None


def _models(provider: str, model: str) -> dict[str, dict[str, Any]]:
    return {
        "agent": {"provider": provider, "name": model, "temperature": 0.0, "max_tokens": 4096},
        "user": {"provider": provider, "name": model, "temperature": 0.2},
    }


def _run_trial_on(task_yaml: Path, runtime: str) -> Any:
    """Run one trial through the reachable slim runner and return the TrialResult."""
    from tolokaforge.adapters._task_loader import load_task_yaml
    from tolokaforge.runner import run_trial

    provider, model = _pick_provider()  # type: ignore[misc]
    task, _task_dir = load_task_yaml(task_yaml)
    return run_trial(
        task=task,
        models=_models(provider, model),
        runtime=runtime,
        conductor="in_process",
    )


_real_agent_guards = pytest.mark.skipif(
    not (_docker_running() and _runner_reachable() and _pick_provider() is not None),
    reason="needs Docker + a reachable runner (`make docker-up`) + an ANTHROPIC/OPENROUTER key",
)


@pytest.mark.requires_api
@pytest.mark.llm
@pytest.mark.slow
@_real_agent_guards
def test_slim_runner_image_shared_stack_tool_use() -> None:
    """A shared-stack real-agent trial on tool_use passes against the slim image."""
    task_yaml = (
        _REPO_ROOT
        / "examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
    )
    result = _run_trial_on(task_yaml, runtime="shared")
    assert result.trajectory.status.value == "completed", result.trajectory.status
    assert result.trajectory.grade is not None


@pytest.mark.requires_api
@pytest.mark.llm
@pytest.mark.slow
@_real_agent_guards
def test_slim_runner_image_per_trial_coding_example() -> None:
    """A per-trial real-agent trial on coding_public_example_01 passes (`:local` path)."""
    task_yaml = (
        _REPO_ROOT
        / "examples/native/coding/dataset/tasks/coding/coding_public_example_01/task.yaml"
    )
    result = _run_trial_on(task_yaml, runtime="per_trial")
    assert result.trajectory.status.value == "completed", result.trajectory.status
    assert result.trajectory.grade is not None


@pytest.mark.requires_api
@pytest.mark.llm
@pytest.mark.slow
@_real_agent_guards
def test_slim_runner_image_run_trial_cli_subprocess_shared_stack() -> None:
    """A ``tolokaforge run-trial`` subprocess drives a real trial to completion against
    the slim runner image — #538 (subprocess) + #539 (slim image) + #536
    (``shared`` resolution) + #537 (composition inside the subprocess) together."""
    from tolokaforge.adapters._task_loader import load_task_yaml
    from tolokaforge.core.trial import TrialResult

    provider, model = _pick_provider()  # type: ignore[misc]
    task_yaml = (
        _REPO_ROOT
        / "examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
    )
    task, task_dir = load_task_yaml(task_yaml)

    start = {
        "v": 1,
        "type": "start",
        "task": task.model_dump(mode="json"),
        "models": _models(provider, model),
        "runtime": "shared",
        "conductor": "in_process",
    }
    # A wire task carries no source_dir, so file assets resolve against the
    # subprocess cwd — spawn at the task-pack root. The provider key is inherited
    # from os.environ (exported by scripts/with_env.sh).
    proc = subprocess.run(
        [sys.executable, "-m", "tolokaforge.dx.cli.main", "run-trial"],
        input=json.dumps(start) + "\n",
        cwd=str(task_dir),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, (
        f"run-trial subprocess failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one stdout line, got: {proc.stdout!r}"
    envelope = json.loads(lines[0])
    assert envelope["type"] == "result", envelope
    result = TrialResult.model_validate(envelope["result"])
    assert result.trajectory.status.value == "completed", result.trajectory.status
    assert result.trajectory.grade is not None
