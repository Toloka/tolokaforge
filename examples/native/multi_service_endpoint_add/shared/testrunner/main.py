"""Test-runner infra for the multi_service_endpoint_add example.

Bind-mounted read-only at /srv/app/main.py — NOT part of the resettable source.
``POST /run-tests`` runs the seeded suite in /workspace (the shared volume the
agent edits) via the stdlib unittest runner, writes a PASS/FAIL marker back into
the volume, and returns the captured output so the agent can read failures and
iterate. The runner image ships no pytest; unittest + Starlette's TestClient
(from fastapi/httpx) suffice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI

_WORKSPACE = Path("/workspace")
_MARKER = _WORKSPACE / "test_result.txt"

app = FastAPI(title="Test Runner")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run-tests")
def run_tests() -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(_WORKSPACE / "tests"),
            "-t",
            str(_WORKSPACE),
        ],
        capture_output=True,
        text=True,
    )
    passed = result.returncode == 0
    _MARKER.write_text("PASS" if passed else "FAIL")
    return {
        "passed": passed,
        "returncode": result.returncode,
        "output": result.stdout + result.stderr,
    }
