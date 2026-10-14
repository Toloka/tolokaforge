"""The ``local`` dispatch adapter's subprocess mode, end to end on one machine.

``tolokaforge run`` with ``orchestrator.dispatch={adapter: local, config:
{mode: subprocess, workers: 2}}`` spawns two local ``python -m
tolokaforge.trial_node`` worker processes, fans two trials out across them, and
rebuilds a reduced bundle per trial. No node addresses are configured — the
adapter spawns and tears down its own workers.

**Gated.** Needs a Docker daemon with the core images built and a live runner
(``make docker-build-core && make docker-up``) — each spawned worker dials the
runner at ``EXECUTOR_ADDRESS`` for a task that declares no per-trial stack — and
a real LLM provider key. Skip-guarded on all three; runs on Linux CI, not on
macOS (Docker-out-of-Docker hangs there) and not on every PR.

**Cost.** Two tool-use trials on a Claude Sonnet model — a few cents.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import yaml

from tolokaforge.secrets import get_default

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


def _docker_running() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _runner_address() -> str:
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


@pytest.mark.skipif(not _docker_running(), reason="Docker daemon not available")
@pytest.mark.skipif(not _runner_reachable(), reason="No runner reachable (run `make docker-up`)")
@pytest.mark.skipif(
    _pick_provider() is None,
    reason="Neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY is set",
)
def test_subprocess_mode_fans_two_trials_across_two_local_workers(tmp_path: Path) -> None:
    provider, model = _pick_provider()  # type: ignore[misc]
    results = tmp_path / "results"
    config = {
        "models": {
            "agent": {"provider": provider, "name": model, "temperature": 0.0, "max_tokens": 4096},
            "user": {"provider": provider, "name": model, "temperature": 0.2},
        },
        "orchestrator": {
            "workers": 2,
            "repeats": 2,
            "auto_start_services": False,
            "dispatch": {"adapter": "local", "config": {"mode": "subprocess", "workers": 2}},
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
        timeout=1200,
        check=False,
    )

    assert proc.returncode == 0, (
        f"local subprocess run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    bundles = sorted(results.glob("trials/*/*/grade.yaml"))
    assert len(bundles) == 2, f"expected two control-plane trial bundles, got {bundles}"
    for grade_path in bundles:
        grade = yaml.safe_load(grade_path.read_text())
        assert grade["binary_pass"] is True, json.dumps(grade, indent=2, default=str)
        trajectory = yaml.safe_load((grade_path.parent / "trajectory.yaml").read_text())
        assert trajectory["status"] == "completed"
