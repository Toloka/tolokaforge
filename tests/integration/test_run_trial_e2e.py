"""Real-agent-loop parity lock for ``tolokaforge.runner.run_trial``.

Against a live local runner (``shared`` backend, ``conductor="in_process"``)
and a cheap real LLM, ``run_trial`` must produce a trial whose
``status`` / ``grade.binary_pass`` / ``grade.score`` / final env state match
what the :class:`Orchestrator` persists for the same task + models +
``repeats=1`` (modulo trial id / worker id / timestamps / cost jitter). This is
the only tier that exercises ``register_trial`` / ``execute_tool`` /
``grade_trial`` for real — the surface a bare ``InMemoryRuntimeBackend``
cannot provide, so the fast composition lock lives in the canonical tier.

**Gated.** Needs Docker + a live runner (``make docker-up``) and a real LLM
provider key. Skip-guarded on all three; runs in the push/nightly/gate lane,
not on every PR.

**Cost.** One tool-use trial (max ~9 turns) on a Claude Sonnet model. Two runs
(``run_trial`` + the orchestrator baseline) — a few cents.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml
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
_TASK_YAML = (
    _REPO_ROOT
    / "examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
)


def _docker_running() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _runner_reachable() -> bool:
    """Best-effort TCP probe of the runner. ``EXECUTOR_ADDRESS`` is a service
    address, not a secret, so a direct ``os.environ`` read is correct here."""
    address = os.environ.get("EXECUTOR_ADDRESS") or "localhost:50051"
    host, _, port = address.partition(":")
    try:
        with socket.create_connection((host, int(port or "50051")), timeout=3):
            return True
    except (OSError, ValueError):
        return False


def _pick_provider() -> tuple[str, str] | None:
    """(provider, model) whose credential is available via ``SecretManager``."""
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


def _run_orchestrator_baseline(
    provider: str, model: str, output_dir: Path, env: dict[str, str]
) -> dict[str, Any]:
    """Drive ``tolokaforge run`` for the same task and read the persisted trial.

    Returns ``{"status", "binary_pass", "score", "env"}`` from the on-disk
    ``trajectory.yaml`` / ``grade.yaml`` / ``env.yaml``.
    """
    cfg = {
        "models": _models(provider, model),
        "orchestrator": {
            "workers": 1,
            "repeats": 1,
            "max_turns": 9,
            "auto_start_services": False,
            "queue_backend": "sqlite",
        },
        "evaluation": {
            "task_packs": [_PACK],
            "tasks_glob": _TASK_GLOB,
            "output_dir": str(output_dir),
            "cache_images": True,
        },
    }
    cfg_path = output_dir.parent / "run.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))

    proc = subprocess.run(
        ["uv", "run", "tolokaforge", "run", "--config", str(cfg_path)],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, (
        f"orchestrator baseline run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )

    trial_dirs = list(output_dir.parent.glob("**/trials/*/*/trajectory.yaml"))
    assert trial_dirs, f"no persisted trial under {output_dir.parent}"
    trial_dir = trial_dirs[0].parent
    trajectory = yaml.safe_load((trial_dir / "trajectory.yaml").read_text())
    grade = yaml.safe_load((trial_dir / "grade.yaml").read_text())
    env_state = yaml.safe_load((trial_dir / "env.yaml").read_text())
    return {
        "status": trajectory["status"],
        "binary_pass": grade["binary_pass"],
        "score": grade["score"],
        "env": env_state,
    }


@pytest.mark.skipif(not _docker_running(), reason="Docker daemon not available")
@pytest.mark.skipif(not _runner_reachable(), reason="No runner reachable (run `make docker-up`)")
@pytest.mark.skipif(
    _pick_provider() is None,
    reason="Neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY is set",
)
def test_run_trial_matches_orchestrator_real_agent_loop(tmp_path: Path) -> None:
    from tolokaforge.runner import run_trial

    provider, model = _pick_provider()  # type: ignore[misc]
    task, _task_dir = load_task_yaml(_TASK_YAML)

    result = run_trial(
        task=task,
        models=_models(provider, model),
        runtime="shared",
        conductor="in_process",
    )

    baseline = _run_orchestrator_baseline(
        provider, model, tmp_path / "orch" / "results", os.environ.copy()
    )

    assert result.trajectory.status.value == baseline["status"]
    assert result.trajectory.grade is not None
    assert result.trajectory.grade.binary_pass == baseline["binary_pass"]
    assert result.trajectory.grade.score == pytest.approx(baseline["score"])
    assert result.trajectory.final_env_state == baseline["env"]
