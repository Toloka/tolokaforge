"""End-to-end proof that the ``multi_service_lot_ops`` multi-container
infrastructure and trace capture work.

Drives the shipped ``examples/native/multi_service_lot_ops`` pack as a real
subprocess. The stack is ``agent -> FastAPI (app-service) -> postgres:16
(app-db)``, all ``isolation: shared``. This test validates the *infrastructure*,
not agent task correctness:

- the compose stack boots and the agent (in the runner container) reaches the
  FastAPI service over the internal compose network (a 2xx ``http_request``);
- the ``db_probes`` grading primitive connects to ``app-db`` as the read-only
  ``grader`` role via ``asyncpg`` and runs its SELECT without a driver/connection
  error;
- the agent's mutation reached postgres through the FastAPI service (the probe's
  read-only-grader SELECT reads back the ``corrective_actions`` row);
- the run captures full traces (trajectory, grade, metrics) to disk.

Deliberately *out of scope*: ``binary_pass``, ``state_checks == 1.0``, and judge
success. Those are agent-behaviour and separate-bug concerns (the shipped agent
model is non-deterministic and may, e.g., open a duplicate corrective action or
hit a transient judge API error) — this test must not couple the infrastructure
signal to them.

**Gated.** Requires a real Docker daemon (the shared runtime brings up the
compose stack) and a real LLM provider key (the agent must emit real tool calls).
``requires_api`` auto-skips without a key (see ``tests/conftest.py``); the
``docker`` skip below covers a missing daemon.

**Cost.** One trial of a single GET-plus-POST-plus-write task (max 10 turns) on
``anthropic/claude-haiku-4-5`` via OpenRouter. ~$0.05.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.utils.docker_helpers import is_docker_daemon_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.docker,
    pytest.mark.requires_api,
    pytest.mark.llm,
    pytest.mark.slow,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK = "examples/native/multi_service_lot_ops"
_RUN_CONFIG = f"{_PACK}/run_configs/dev.yaml"
_TASK_ID = "lot_ops_01"
_APP_SERVICE = "app-service:8000"

# Substrings a failed asyncpg connect/auth/query surfaces. Their absence from the
# db_probe reasons proves the grader-role DSN + asyncpg evaluator worked; the
# probe reaching a PASS/FAIL verdict at all means it queried postgres.
_DB_ERROR_SIGNATURES = (
    "could not query postgres",
    "connection refused",
    "connectionrefusederror",
    "authentication failed",
    "password authentication",
    "does not exist",
    "could not connect",
)


def _output_basename() -> str:
    """The configured ``output_dir`` basename the orchestrator suffixes with a
    run timestamp (``<basename>_<YYYYmmdd_HHMMSS>``)."""
    cfg = yaml.safe_load((_REPO_ROOT / _RUN_CONFIG).read_text())
    return Path(cfg["evaluation"]["output_dir"]).name


def _has_2xx_http_request_to_app_service(trajectory: dict[str, Any]) -> bool:
    """True iff the agent both issued an ``http_request`` at ``app-service:8000``
    and a tool result reported a 2xx status — the agent-in-container -> FastAPI
    path over the internal compose network."""
    messages = trajectory.get("messages") or []
    called_app_service = any(
        (call.get("name") == "http_request")
        and (_APP_SERVICE in str((call.get("arguments") or {}).get("url", "")))
        for msg in messages
        for call in (msg.get("tool_calls") or [])
    )
    got_2xx = any(
        msg.get("role") == "tool" and re.search(r"Status:\s*2\d\d", str(msg.get("content") or ""))
        for msg in messages
    )
    return called_app_service and got_2xx


def _assert_infrastructure(run_dir: Path) -> None:
    """Assert the run's artifacts prove the multi-container infrastructure and
    trace capture worked. Split out so the assertions can be dry-run against a
    preserved run dir without spending an LLM run."""
    trial_dir = run_dir / "trials" / _TASK_ID / "0"

    grade_path = trial_dir / "grade.yaml"
    trajectory_path = trial_dir / "trajectory.yaml"
    metrics_path = trial_dir / "metrics.yaml"
    for path in (grade_path, trajectory_path, metrics_path):
        assert path.exists(), (
            f"missing {path.name} at {path}.\n"
            f"run dir contents: {sorted(p.name for p in run_dir.rglob('*'))[:50]}"
        )

    trajectory = yaml.safe_load(trajectory_path.read_text())
    assert _has_2xx_http_request_to_app_service(trajectory), (
        "no successful (2xx) http_request to app-service:8000 in the trajectory — "
        "the agent-in-container -> FastAPI network path did not work.\n"
        f"messages: {trajectory.get('messages')}"
    )

    grade = yaml.safe_load(grade_path.read_text())
    reasons = grade["reasons"]
    assert "DB probes:" in reasons, (
        "grade reasons carry no 'DB probes:' segment — the db_probes primitive "
        f"did not run.\nreasons: {reasons}"
    )
    lowered = reasons.lower()
    hit = next((sig for sig in _DB_ERROR_SIGNATURES if sig in lowered), None)
    assert hit is None, (
        f"db_probe hit a connection/driver error ({hit!r}) — the grader-role DSN "
        f"or asyncpg evaluator failed.\nreasons: {reasons}"
    )

    # The mutation reached postgres through FastAPI: the probe's read-only-grader
    # SELECT read back the corrective_actions row the agent POSTed (reason_code
    # and status assertions PASS => a matching substrate row exists). This is the
    # independent-oracle substrate check; a separate live connection is not
    # possible because the shared runtime tears app-db down when the run exits.
    assert "PASS: corrective action uses the contamination reason code" in reasons, (
        "db_probe did not read back a corrective_actions row with the contamination "
        f"reason code — the mutation did not reach postgres.\nreasons: {reasons}"
    )
    open_detail = f"db_probe did not read back an open corrective_actions row.\nreasons: {reasons}"
    assert "PASS: the action is open" in reasons, open_detail

    metrics = yaml.safe_load(metrics_path.read_text())
    calls_detail = (
        f"metrics report no tool calls — the tool orchestration did not run.\nmetrics: {metrics}"
    )
    assert metrics["tool_calls"] > 0, calls_detail
    cost_detail = f"metrics report zero cost — the agent LLM did not run.\nmetrics: {metrics}"
    assert metrics["cost_usd"] > 0, cost_detail


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (shared runtime needs it)",
)
def test_lot_ops_infrastructure_end_to_end() -> None:
    """The full ``tolokaforge run`` exits 0 and its captured traces prove the
    multi-container stack, agent-in-container -> FastAPI network path, substrate
    mutation, db_probe grading primitive, and trace capture all worked."""
    basename = _output_basename()
    results_root = _REPO_ROOT / "results"
    before = set(results_root.glob(f"{basename}_*")) if results_root.exists() else set()

    proc = subprocess.run(
        ["uv", "run", "tolokaforge", "run", "--config", _RUN_CONFIG],
        cwd=str(_REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, (
        f"tolokaforge run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\n"
        f"stderr:\n{proc.stderr[-4000:]}"
    )

    after = set(results_root.glob(f"{basename}_*"))
    created = after - before
    assert len(created) == 1, (
        f"expected exactly one new run dir under {results_root} matching "
        f"{basename}_*; got {sorted(created)}"
    )
    run_dir = created.pop()

    _assert_infrastructure(run_dir)

    # Only reached once every assertion passed — a red run leaves the run dir
    # (with all traces) on disk for post-mortem.
    shutil.rmtree(run_dir, ignore_errors=True)
