"""End-to-end proof that the ``depends_on`` + healthcheck + ``--wait``
start-order chain holds under a deliberately slow dependency.

Drives the shipped ``examples/native/multi_service_slow_start`` pack as a
real subprocess at ``repeats: 1``. The pack's ``app-db`` runs a trailing
``SELECT pg_sleep(25)`` in its ``docker-entrypoint-initdb.d`` init script,
so postgres is genuinely TCP-unreachable for ~25 s, and its healthcheck
probes TCP (``pg_isready -h``) so the container reports ``starting`` for
the whole window. ``PerTrialRuntimeBackend.provision`` runs ``docker
compose up -d --wait``, which blocks until every service (incl. the slow
``app-db``) is healthy before the trial's first RPC. The agent then reads
widget 1's ``slow_start_ok`` name over PostgREST and writes it to a
submission file; deterministic ``state_checks`` grading asserts the file
contains it.

A passing grade is proof the chain held: if ``depends_on:
service_healthy`` did not gate the order, ``app-service`` (PostgREST) would
start before postgres accepted TCP, and the agent's first tool call would
hit a refused connection, leaving the submission empty and ``state_checks``
at 0.

**Gated.** Requires a real Docker daemon (the per-trial backend brings up a
fresh compose stack) and a real LLM provider key (the agent must emit real
tool calls; the ``mock`` provider emits none). ``requires_api`` auto-skips
without a key (see ``tests/conftest.py``); the ``docker`` skip below covers
a missing daemon.

**Cost.** One trial of a single-GET-plus-write task (max 8 turns) on
``anthropic/claude-haiku-4-5`` via OpenRouter. Cents. Dominant wall-clock
cost is the ~25 s slow start plus first-run image build.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

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
_PACK = "examples/native/multi_service_slow_start"
_RUN_CONFIG = f"{_PACK}/run_config.yaml"

_MIN_PROVISION_SECONDS = 20.0

# StructuredLogger's console formatter prefixes every record with an
# `asctime` at this granularity (`tolokaforge/core/logging.py`).
_CONSOLE_TS = "%Y-%m-%d %H:%M:%S"
_PROVISION_START = "Provisioning trial env"
_PROVISION_DONE = "Trial env provisioned"

# A refused/failed first connection would surface as one of these in the
# captured output or trajectory. Grading already proves the chain held, so
# any of these appearing alongside a passing grade means the agent recovered
# from an error the start-order chain should have prevented — flag it.
_TOOL_CALL_ERROR_MARKERS = (
    "ConnectionRefused",
    "ProvisionError",
    "connection refused",
    "could not connect",
)


def _output_basename() -> str:
    """The configured ``output_dir`` basename the orchestrator suffixes
    with a run timestamp (``<basename>_<YYYYmmdd_HHMMSS>``)."""
    cfg = yaml.safe_load((_REPO_ROOT / _RUN_CONFIG).read_text())
    return Path(cfg["evaluation"]["output_dir"]).name


def _console_timestamp(combined: str, message: str) -> datetime:
    """Wall-clock timestamp of the single console record whose message is
    ``message``. ``repeats: 1`` guarantees exactly one match."""
    matches = [
        datetime.strptime(line[:19], _CONSOLE_TS)
        for line in combined.splitlines()
        if message in line and re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", line)
    ]
    assert len(matches) == 1, (
        f"expected exactly one console record containing {message!r} at repeats=1; "
        f"got {len(matches)}"
    )
    return matches[0]


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial backend needs it)",
)
def test_slow_start_chain_holds_end_to_end() -> None:
    """The trial provisions in ≥20 s (slow start fired), the first tool
    call succeeds, and grading passes — the start-order chain held."""
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
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"tolokaforge run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\n"
        f"stderr:\n{proc.stderr[-4000:]}"
    )

    assert "runtime.backend.selected" in combined and "PerTrialRuntimeBackend" in combined, (
        "run did not route onto PerTrialRuntimeBackend — the slow-start chain never fired.\n"
        f"stderr tail:\n{proc.stderr[-2000:]}"
    )

    provision_seconds = (
        _console_timestamp(combined, _PROVISION_DONE)
        - _console_timestamp(combined, _PROVISION_START)
    ).total_seconds()
    assert provision_seconds >= _MIN_PROVISION_SECONDS, (
        f"provision took {provision_seconds:.0f}s (< {_MIN_PROVISION_SECONDS:.0f}s floor) — "
        "the slow start did not fire; the app-db healthcheck likely flapped healthy on the "
        "socket-only init server instead of probing TCP.\n"
        f"stdout tail:\n{proc.stdout[-2000:]}"
    )

    after = set(results_root.glob(f"{basename}_*"))
    created = after - before
    assert len(created) == 1, (
        f"expected exactly one new run dir under {results_root} matching "
        f"{basename}_*; got {sorted(created)}"
    )
    run_dir = created.pop()

    try:
        grade_path = run_dir / "trials" / "startup_probe" / "0" / "grade.yaml"
        assert grade_path.exists(), (
            f"missing grade.yaml at {grade_path}.\n"
            f"run dir contents: {sorted(p.name for p in run_dir.rglob('*'))[:50]}"
        )
        grade = yaml.safe_load(grade_path.read_text())
        assert grade["binary_pass"] is True, (
            "trial did not pass — the agent likely could not read 'slow_start_ok' over "
            "PostgREST, which means the start-order chain did not hold.\n"
            f"grade: {grade}"
        )
        assert grade["components"]["state_checks"] == 1.0, (
            "state_checks != 1.0 — the submission did not contain the seeded "
            "'slow_start_ok'; the first runner → app-service → postgres call failed.\n"
            f"grade: {grade}"
        )

        haystacks = [combined]
        haystacks += [p.read_text() for p in run_dir.rglob("trajectory.yaml")]
        found = sorted(
            {
                marker
                for marker in _TOOL_CALL_ERROR_MARKERS
                for hay in haystacks
                if marker.lower() in hay.lower()
            }
        )
        assert not found, (
            "grade passed but tool-call error markers appeared in the output/trajectory "
            f"({found}) — the agent recovered from a connection error the start-order chain "
            "should have prevented; the slow start may be racing.\n"
            f"stdout tail:\n{proc.stdout[-2000:]}"
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
