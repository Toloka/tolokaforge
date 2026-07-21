"""Subprocess behaviour lock for ``tolokaforge agent`` — the acceptance
behaviour-lock, Docker/LLM-free, every PR.

Spawns a real ``tolokaforge agent`` subprocess and asserts the stdout
JSON-Lines stream for the six wire outcomes. ``runtime="in_memory"
conductor="in_memory"`` drives the whole path deterministically with no Docker
and no LLM key: ``InMemoryConductor`` returns a synthetic trajectory and never
touches the RPC surface, and registry resolution goes through installed
entry-point metadata (the same dependency as ``test_run_trial_composition``).

The real agent loop across the subprocess boundary is locked separately in
``tests/integration/test_agent_e2e.py`` (gated: real runner + real LLM).
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge import run_trial
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import TaskConfig
from tolokaforge.core.trial import TrialResult

pytestmark = pytest.mark.canonical

_AGENT = {"provider": "openai", "name": "gpt-4"}
_AGENT_CMD = [sys.executable, "-m", "tolokaforge.cli.main", "agent"]


@pytest.fixture
def flat_pack(tmp_path: Path) -> Path:
    """A flat-layout, MCP-free pack (the ``test_run_trial_composition`` shape).

    Returns the task directory — the subprocess is spawned with ``cwd`` here so
    a wire task (which carries no ``source_dir``) resolves ``initial_state.json``
    / ``grading.yaml`` against it.
    """
    task_dir = tmp_path / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "flat",
            "name": "flat",
            "category": "tool_use",
            "description": "flat",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "grading": "grading.yaml",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 1.0,
            },
        },
    )
    return task_dir


def _load_task(task_dir: Path) -> TaskConfig:
    base_dir = task_dir.parents[1]
    adapter = NativeAdapter({"base_dir": str(base_dir), "tasks_glob": "tasks/**/task.yaml"})
    return adapter.get_task("flat")


def _start_line(task: TaskConfig, **overrides: object) -> str:
    message: dict[str, object] = {
        "v": 1,
        "type": "start",
        "task": task.model_dump(mode="json"),
        "models": {"agent": _AGENT},
        "runtime": "in_memory",
        "conductor": "in_memory",
    }
    message.update(overrides)
    return json.dumps(message) + "\n"


def _spawn(stdin: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _AGENT_CMD, input=stdin, cwd=str(cwd), capture_output=True, text=True, timeout=120
    )


def _sole_stdout_envelope(proc: subprocess.CompletedProcess[str]) -> dict:
    """Assert stdout carried exactly one non-empty line and return it parsed."""
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got: {proc.stdout!r}"
    return json.loads(lines[0])


def test_happy_path_matches_run_trial(flat_pack: Path) -> None:
    task = _load_task(flat_pack)
    proc = _spawn(_start_line(task), cwd=flat_pack)

    assert proc.returncode == 0, f"stderr:\n{proc.stderr[-2000:]}"
    envelope = _sole_stdout_envelope(proc)
    assert envelope["v"] == 1
    assert envelope["type"] == "result"

    got = TrialResult.model_validate(envelope["result"])
    expected = run_trial(
        task=task, models={"agent": _AGENT}, runtime="in_memory", conductor="in_memory"
    )
    # start_ts / end_ts are per-run wall-clock; every other trajectory field
    # (grade included) is deterministic for the in_memory conductor, and
    # trial_id / worker_id derive from the canonical spec, so they match exactly.
    exclude = {"start_ts", "end_ts"}
    assert got.trajectory.model_dump(exclude=exclude) == expected.trajectory.model_dump(
        exclude=exclude
    )
    assert (got.trial_id, got.worker_id) == (expected.trial_id, expected.worker_id)


def test_malformed_input_is_protocol_error(flat_pack: Path) -> None:
    proc = _spawn("{not json\n", cwd=flat_pack)
    assert proc.returncode != 0
    assert _sole_stdout_envelope(proc)["error_type"] == "ProtocolError"


def test_premature_eof_is_cancelled(flat_pack: Path) -> None:
    proc = _spawn("", cwd=flat_pack)
    assert proc.returncode != 0
    assert _sole_stdout_envelope(proc)["error_type"] == "cancelled"


def test_synchronous_cancel_is_cancelled(flat_pack: Path) -> None:
    proc = _spawn('{"v":1,"type":"cancel"}\n', cwd=flat_pack)
    assert proc.returncode != 0
    assert _sole_stdout_envelope(proc)["error_type"] == "cancelled"


def test_unknown_runtime_name_lists_known_names(flat_pack: Path) -> None:
    task = _load_task(flat_pack)
    proc = _spawn(_start_line(task, runtime="bogus"), cwd=flat_pack)
    assert proc.returncode != 0
    envelope = _sole_stdout_envelope(proc)
    assert envelope["error_type"] == "UnknownImplementationError"
    assert "in_memory" in envelope["message"]


def test_sigterm_while_blocked_is_cancelled(flat_pack: Path) -> None:
    """SIGTERM while the agent blocks reading stdin → one ``cancelled`` line.

    The SIGTERM/SIGINT handler installs only after the (heavy)
    ``tolokaforge.cli.main`` import and Click dispatch; a SIGTERM arriving
    before that kills the process by default disposition
    (``returncode == -SIGTERM``). To stay non-flaky on slow CI we escalate the
    warmup across fresh spawns until the handler is provably active (a clean
    ``cancelled`` line, exit 1), rather than betting on one fixed sleep.
    """
    outcome: tuple[int, str] | None = None
    for warmup_s in (1.0, 2.0, 4.0, 6.0, 8.0):
        proc = subprocess.Popen(
            _AGENT_CMD,
            cwd=str(flat_pack),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(warmup_s)
        proc.send_signal(signal.SIGTERM)
        try:
            stdout, _stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            continue
        outcome = (proc.returncode, stdout)
        if proc.returncode == 1 and stdout.strip():
            break

    assert outcome is not None, "agent never produced a terminal outcome"
    returncode, stdout = outcome
    assert returncode == 1, f"signal handler never became active (last rc={returncode})"
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["error_type"] == "cancelled"
