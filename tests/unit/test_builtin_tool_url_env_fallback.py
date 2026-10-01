"""Builtin tools that hold a side-service URL honor the runner's env var
when no explicit URL is passed.

The runner container sets ``DB_SERVICE_URL`` / ``RAG_SERVICE_URL`` to the
docker-network hostnames on ``runner-net``; a tool constructed with no URL
reads the env var first and falls back to its literal default.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("DB_SERVICE_URL", raising=False)
    monkeypatch.delenv("RAG_SERVICE_URL", raising=False)


def test_sql_query_default_honors_db_service_url_env(monkeypatch, clean_env):
    from tolokaforge.tools.builtin.db_json import SQLQueryTool

    monkeypatch.setenv("DB_SERVICE_URL", "http://tolokaforge-db-service:8000")
    tool = SQLQueryTool()
    assert tool.db_url == "http://tolokaforge-db-service:8000"


def test_sql_schema_default_honors_db_service_url_env(monkeypatch, clean_env):
    from tolokaforge.tools.builtin.db_json import SQLSchemaToolDB

    monkeypatch.setenv("DB_SERVICE_URL", "http://tolokaforge-db-service:8000")
    tool = SQLSchemaToolDB()
    assert tool.db_url == "http://tolokaforge-db-service:8000"


def test_search_kb_default_honors_rag_service_url_env(monkeypatch, clean_env):
    from tolokaforge.tools.builtin.rag_search import SearchKBTool

    monkeypatch.setenv("RAG_SERVICE_URL", "http://tolokaforge-rag-service:8001")
    tool = SearchKBTool()
    assert tool.rag_url == "http://tolokaforge-rag-service:8001"


def test_search_kb_falls_back_when_env_unset(clean_env):
    from tolokaforge.tools.builtin.rag_search import SearchKBTool

    tool = SearchKBTool()
    assert tool.rag_url == "http://rag-service:8001"
