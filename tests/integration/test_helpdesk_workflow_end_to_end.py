"""End-to-end proof that the ``multi_service_helpdesk_workflow`` multi-container
infrastructure and trace capture work.

Drives the shipped ``examples/native/multi_service_helpdesk_workflow`` pack as a
real subprocess. The stack is ``agent -> 5 FastAPI services (delivery-tracker,
product-catalog, client-locations, crm, policy-search) -> postgres:16
(app-db)``, with ``app-db`` declared ``isolation: ephemeral`` (which routes the
whole run to the per-trial backend). This test validates the *infrastructure*,
not agent task correctness:

- the compose stack boots and the agent (in the runner container) reaches the
  FastAPI services over the internal compose network — 2xx ``http_request`` s to
  at least three distinct app hostnames, including ``policy-search:8000`` and
  ``crm:8000`` (the cross-service internal-network path; three-of-five is the
  infra bar, not agent thoroughness);
- the agent's CRM mutation reached postgres through FastAPI — a 2xx ``POST`` to
  ``http://crm:8000/cases`` (the agent -> crm -> postgres write path);
- the ``db_probes`` grading primitive connects to ``app-db`` as the read-only
  ``grader`` role via ``asyncpg`` and runs its SELECT without a driver/connection
  error (the grade reasons carry a ``DB probes:`` segment and none of the asyncpg
  connection/auth/query error signatures);
- the run captures full traces (trajectory, grade, metrics) to disk.

Deliberately *out of scope*: ``resolution_path == reschedule``, ``binary_pass``,
``state_checks == 1.0``, and judge success. Those are agent-correctness and
separate-bug concerns — the task is adversarial by design (the agent must reason
across policy + site capabilities to pick the one valid path and may not), and
the Sonnet-4-6 judge has a known OpenRouter 401 (tracked separately). This test
must not couple the infrastructure signal to any of them.

**Gated.** Requires a real Docker daemon (the per-trial runtime brings up the
compose stack) and a real LLM provider key (the agent must emit real tool calls).
``requires_api`` auto-skips without a key (see ``tests/conftest.py``); the
``docker`` skip below covers a missing daemon.

**Cost.** One trial of an 18-turn cross-service task on
``anthropic/claude-haiku-4-5`` via OpenRouter. ~$0.30-1.00.
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
_PACK = "examples/native/multi_service_helpdesk_workflow"
_RUN_CONFIG = f"{_PACK}/run_configs/dev.yaml"
_TASK_ID = "helpdesk_01"

# The five FastAPI app services the agent reaches over the internal compose
# network. The infra bar is 2xx to >= 3 distinct ones, including these two.
_APP_HOSTNAMES = (
    "delivery-tracker:8000",
    "product-catalog:8000",
    "client-locations:8000",
    "crm:8000",
    "policy-search:8000",
)
_REQUIRED_HOSTNAMES = ("policy-search:8000", "crm:8000")
_MIN_DISTINCT_HOSTNAMES = 3
_CRM_CREATE_URL = "http://crm:8000/cases"

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


def _status_by_tool_call_id(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Map each ``tool_call_id`` to the HTTP status parsed from its tool-result
    message content (``http_request`` output begins ``Status: <code>``). Lets a
    2xx be tied back to the specific ``http_request`` that produced it."""
    statuses: dict[str, int] = {}
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tool_call_id = msg.get("tool_call_id")
        if not tool_call_id:
            continue
        match = re.search(r"Status:\s*(\d{3})", str(msg.get("content") or ""))
        if match:
            statuses[tool_call_id] = int(match.group(1))
    return statuses


def _app_hostnames_with_2xx(trajectory: dict[str, Any]) -> set[str]:
    """The set of app hostnames the agent hit with a 2xx ``http_request`` —
    each ``http_request`` tool call correlated to its result status by
    ``tool_call_id``. Proves the agent-in-container -> FastAPI network path per
    distinct service."""
    messages = trajectory.get("messages") or []
    statuses = _status_by_tool_call_id(messages)
    reached: set[str] = set()
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            if call.get("name") != "http_request":
                continue
            status = statuses.get(call.get("id"))
            if status is None or not (200 <= status < 300):
                continue
            url = str((call.get("arguments") or {}).get("url", ""))
            reached.update(host for host in _APP_HOSTNAMES if host in url)
    return reached


def _has_2xx_post_to(trajectory: dict[str, Any], url_fragment: str) -> bool:
    """True iff the agent issued a ``POST`` ``http_request`` whose URL contains
    *url_fragment* and whose result reported a 2xx status."""
    messages = trajectory.get("messages") or []
    statuses = _status_by_tool_call_id(messages)
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            if call.get("name") != "http_request":
                continue
            args = call.get("arguments") or {}
            if str(args.get("method", "")).upper() != "POST":
                continue
            if url_fragment not in str(args.get("url", "")):
                continue
            status = statuses.get(call.get("id"))
            if status is not None and 200 <= status < 300:
                return True
    return False


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
    reached = _app_hostnames_with_2xx(trajectory)
    assert len(reached) >= _MIN_DISTINCT_HOSTNAMES, (
        f"agent reached fewer than {_MIN_DISTINCT_HOSTNAMES} distinct app hostnames "
        f"with a 2xx http_request — the multi-service internal-network path did not "
        f"work.\nreached: {sorted(reached)}\nmessages: {trajectory.get('messages')}"
    )
    missing = [host for host in _REQUIRED_HOSTNAMES if host not in reached]
    assert not missing, (
        f"agent did not reach {missing} with a 2xx http_request — the cross-service "
        f"path to those services did not work.\nreached: {sorted(reached)}"
    )

    assert _has_2xx_post_to(trajectory, _CRM_CREATE_URL), (
        f"no successful (2xx) POST to {_CRM_CREATE_URL} in the trajectory — the "
        f"agent -> crm -> postgres write path did not work.\n"
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

    metrics = yaml.safe_load(metrics_path.read_text())
    calls_detail = (
        f"metrics report no tool calls — the tool orchestration did not run.\nmetrics: {metrics}"
    )
    assert metrics["tool_calls"] > 0, calls_detail
    cost_detail = f"metrics report zero cost — the agent LLM did not run.\nmetrics: {metrics}"
    assert metrics["cost_usd"] > 0, cost_detail


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial runtime needs it)",
)
def test_helpdesk_workflow_infrastructure_end_to_end() -> None:
    """The full ``tolokaforge run`` exits 0 and its captured traces prove the
    multi-container stack, agent-in-container -> multi-FastAPI network path
    (>= 3 distinct services incl. policy-search + crm), the CRM write path to
    postgres, the db_probe grading primitive, and trace capture all worked."""
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
    # (with all traces, incl. per-service Docker logs) on disk for post-mortem.
    shutil.rmtree(run_dir, ignore_errors=True)
