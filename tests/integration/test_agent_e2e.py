"""Real-agent-loop parity lock for the ``tolokaforge agent`` subprocess.

Spawns ``tolokaforge agent`` with ``runtime="shared" conductor="in_process"``
against a live local runner and a cheap real LLM, and asserts the ADR wire
contract end-to-end: exactly one ``result`` line, exit 0, and a grade matching
what ``tolokaforge.run_trial(...)`` produces in-process for the same task +
models (modulo timestamps / cost jitter). This is the only tier that drives the
real agent loop (``register_trial`` / ``execute_tool`` / ``grade_trial``) across
the subprocess boundary — the acceptance criterion that a downstream harness can
spawn ``tolokaforge agent`` and observe the ADR-specified messages.

**Gated.** Needs Docker + a live runner (``make docker-up``) and a real LLM
provider key. Skip-guarded on all three; runs in the push/nightly/gate lane,
not on every PR.

**Cost.** One tool-use trial (max ~9 turns) on a Claude Sonnet model, twice
(subprocess + in-process baseline) — a few cents.
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

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.trial import TrialResult
from tolokaforge.secrets import get_default

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_api,
    pytest.mark.requires_docker,
    pytest.mark.llm,
    pytest.mark.slow,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TASK_YAML = (
    _REPO_ROOT
    / "examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
)
_AGENT_CMD = [sys.executable, "-m", "tolokaforge.cli.main", "agent"]


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


@pytest.mark.skipif(not _docker_running(), reason="Docker daemon not available")
@pytest.mark.skipif(not _runner_reachable(), reason="No runner reachable (run `make docker-up`)")
@pytest.mark.skipif(
    _pick_provider() is None,
    reason="Neither ANTHROPIC_API_KEY nor OPENROUTER_API_KEY is set",
)
def test_agent_subprocess_matches_run_trial_real_agent_loop() -> None:
    import tolokaforge

    provider, model = _pick_provider()  # type: ignore[misc]
    task, task_dir = load_task_yaml(_TASK_YAML)

    start = {
        "v": 1,
        "type": "start",
        "task": task.model_dump(mode="json"),
        "models": _models(provider, model),
        "runtime": "shared",
        "conductor": "in_process",
    }
    # A wire task carries no source_dir, so file assets resolve against the
    # subprocess cwd — spawn at the task-pack root. The subprocess inherits the
    # provider key from os.environ (exported by scripts/with_env.sh).
    proc = subprocess.run(
        _AGENT_CMD,
        input=json.dumps(start) + "\n",
        cwd=str(task_dir),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, (
        f"agent subprocess failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one stdout line, got: {proc.stdout!r}"
    envelope = json.loads(lines[0])
    assert envelope["type"] == "result", envelope
    got = TrialResult.model_validate(envelope["result"])

    expected = tolokaforge.run_trial(
        task=task,
        models=_models(provider, model),
        runtime="shared",
        conductor="in_process",
    )

    assert got.trajectory.status == expected.trajectory.status
    assert got.trajectory.grade is not None
    assert expected.trajectory.grade is not None
    assert got.trajectory.grade.binary_pass == expected.trajectory.grade.binary_pass
    assert got.trajectory.grade.score == pytest.approx(expected.trajectory.grade.score)
