"""``SearchKBTool`` honors the runner's ``RAG_SERVICE_URL`` when no explicit URL is passed.

The runner container sets ``RAG_SERVICE_URL`` to the docker-network hostname
on ``runner-net``; a tool constructed with no URL reads the env var first and
falls back to its literal default.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("RAG_SERVICE_URL", raising=False)


def test_search_kb_default_honors_rag_service_url_env(monkeypatch, clean_env):
    from tolokaforge.tools.builtin.rag_search import SearchKBTool

    monkeypatch.setenv("RAG_SERVICE_URL", "http://tolokaforge-rag-service:8001")
    tool = SearchKBTool()
    assert tool.rag_url == "http://tolokaforge-rag-service:8001"


def test_search_kb_falls_back_when_env_unset(clean_env):
    from tolokaforge.tools.builtin.rag_search import SearchKBTool

    tool = SearchKBTool()
    assert tool.rag_url == "http://rag-service:8001"
