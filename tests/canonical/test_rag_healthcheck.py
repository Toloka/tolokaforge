"""Guards the rag-service Docker HEALTHCHECK grace period.

rag-service eagerly loads the ``all-MiniLM-L6-v2`` embedding model at startup
(~45s standalone, longer under container contention). Docker only begins
counting probe failures toward ``--retries`` once ``--start-period`` elapses, so
a too-short grace makes ``docker compose up --wait`` declare the still-loading
container unhealthy and abort the whole stack bring-up. This pins the grace at
a model-load-safe floor so a regression to the old 5s trips CI without a Docker
daemon.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RAG_DOCKERFILE = _REPO_ROOT / "tolokaforge" / "docker" / "dockerfiles" / "rag.Dockerfile"

_MIN_START_PERIOD_SECONDS = 90

_START_PERIOD_RE = re.compile(r"--start-period=(\d+)s\b")


def test_rag_healthcheck_start_period_covers_model_load() -> None:
    text = _RAG_DOCKERFILE.read_text()
    assert "HEALTHCHECK" in text, f"{_RAG_DOCKERFILE} has no HEALTHCHECK to guard"

    match = _START_PERIOD_RE.search(text)
    assert match is not None, (
        f"{_RAG_DOCKERFILE.name} HEALTHCHECK has no `--start-period=<n>s` — the probe would "
        "start enforcing immediately and fail the stack while the embedding model loads"
    )

    start_period = int(match.group(1))
    assert start_period >= _MIN_START_PERIOD_SECONDS, (
        f"{_RAG_DOCKERFILE.name} HEALTHCHECK --start-period={start_period}s is below the "
        f"{_MIN_START_PERIOD_SECONDS}s model-load floor — `compose up --wait` would mark rag "
        "unhealthy before all-MiniLM-L6-v2 finishes loading"
    )
