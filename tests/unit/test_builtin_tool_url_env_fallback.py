"""Builtin tools that connect to side services must honor the runner's
env vars when no explicit URL is passed (#123).

Surfaced by running ``examples/tool_use/`` end-to-end after #110/#121
unblocked the agent's tool calls. The runner container sets
``DB_SERVICE_URL`` / ``RAG_SERVICE_URL`` to point at the actual
docker-network hostnames, but the tool defaults pointed at
``json-db:8000`` / ``rag-service:8001`` which do not exist on
``runner-net``. Result: every ``db_query``/``db_update``/``search_kb``
call from the runner path failed with ``Name or service not known``.

The fix: tool ``__init__`` defaults read the env var first, falling
back to the previous literal default. The executor service path
(which passes ``env_state.json_db_url`` / ``env_state.rag_service_url``
explicitly) is unaffected.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("DB_SERVICE_URL", raising=False)
    monkeypatch.delenv("RAG_SERVICE_URL", raising=False)


def test_db_query_default_honors_db_service_url_env(monkeypatch, clean_env):
    from tolokaforge.tools.builtin.db_json import DBQueryTool

    monkeypatch.setenv("DB_SERVICE_URL", "http://tolokaforge-db-service:8000")
    tool = DBQueryTool()
    assert tool.db_url == "http://tolokaforge-db-service:8000"


def test_db_query_falls_back_when_env_unset(clean_env):
    from tolokaforge.tools.builtin.db_json import DBQueryTool

    tool = DBQueryTool()
    assert tool.db_url == "http://json-db:8000"


def test_db_query_explicit_url_wins_over_env(monkeypatch, clean_env):
    from tolokaforge.tools.builtin.db_json import DBQueryTool

    monkeypatch.setenv("DB_SERVICE_URL", "http://from-env:8000")
    tool = DBQueryTool(db_url="http://explicit:9999")
    assert tool.db_url == "http://explicit:9999"


def test_db_update_default_honors_db_service_url_env(monkeypatch, clean_env):
    from tolokaforge.tools.builtin.db_json import DBUpdateTool

    monkeypatch.setenv("DB_SERVICE_URL", "http://tolokaforge-db-service:8000")
    tool = DBUpdateTool()
    assert tool.db_url == "http://tolokaforge-db-service:8000"


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
