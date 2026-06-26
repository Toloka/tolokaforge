"""The runner reads ``RAG_SERVICE_URL`` from its container env with NO
default, so a RAG client is built iff a rag-service is actually running.

Stage 1 of the judge-KB-resolver fix (issue #95) removes the unconditional
``RAG_SERVICE_URL`` from the core-stack runner env and the runner Dockerfile.
That relocation only fixes the bug if the runner does not re-introduce a
fallback value of its own: a localhost default would keep
``if self.rag_service_url:`` truthy on the core stack, so the runner would
build a RAG client and the judge would be offered a ``search_kb`` tool that
fails at runtime (no rag-service on this stack) — grading silently without
reading the KB. These tests pin the honest absence.
"""

from __future__ import annotations

import pytest

from tolokaforge.runner.__main__ import get_config

pytestmark = pytest.mark.unit


def test_rag_service_url_absent_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core-stack runner: no RAG_SERVICE_URL in env => config carries None,
    so no RAG client is built and the judge gets no unreachable search_kb."""
    monkeypatch.delenv("RAG_SERVICE_URL", raising=False)
    assert get_config()["rag_service_url"] is None


def test_rag_service_url_present_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full-stack runner: the URL the stack injects flows through unchanged,
    so the agent/judge RAG client points at the running rag-service."""
    monkeypatch.setenv("RAG_SERVICE_URL", "http://tolokaforge-rag-service:8001")
    assert get_config()["rag_service_url"] == "http://tolokaforge-rag-service:8001"
