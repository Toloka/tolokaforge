"""Runner-level gating test for the judge's per-trial KnowledgeSearch.

The grading unit tests inject ``kb_search`` straight into ``run_rubric_judge``,
so they bypass the runner-side gate that decides WHETHER the judge gets a KB at
all. That gate is the faithfulness contract: the judge gets a rag-service KB
**iff the agent got a rag ``search_kb``** (a ``RAGSearchToolWrapper`` was
reconstructed) and a rag client exists — bound to the SAME ``rag_client`` +
``trial_id`` the agent used. It must NOT key on ``search_config.enabled`` (the
decoupled TypeSense plane). These tests exercise that real gate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.kb_search import RagServiceKnowledgeSearch
from tolokaforge.runner.rag_client import RAGServiceClient
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.runner.tool_factory import RAGSearchToolWrapper, create_search_kb_schema

pytestmark = pytest.mark.unit


def _service(rag_client: RAGServiceClient | None) -> RunnerServiceImpl:
    """RunnerServiceImpl with a stub DB client and the given rag client."""
    return RunnerServiceImpl(db_client=MagicMock(), rag_client=rag_client)


def _rag_search_tool(rag_client: RAGServiceClient, trial_id: str) -> RAGSearchToolWrapper:
    """A real agent rag search tool, exactly as the factory reconstructs it."""
    return RAGSearchToolWrapper(create_search_kb_schema(), rag_client, trial_id)


def test_judge_kb_resolves_when_agent_has_rag_search_tool():
    rag_client = RAGServiceClient(base_url="http://rag-service:8001")
    service = _service(rag_client)
    trial_id = "rag_task:0"
    agent_tools = {
        "some_other_tool": MagicMock(),
        "search_kb": _rag_search_tool(rag_client, trial_id),
    }

    kb = service._resolve_judge_kb_search(trial_id, agent_tools)

    # The judge gets a rag-service KB bound to the SAME client + trial the agent used.
    assert isinstance(kb, RagServiceKnowledgeSearch)
    assert kb._trial_id == trial_id
    assert kb._base_url == "http://rag-service:8001"


def test_judge_kb_is_none_without_rag_search_tool():
    service = _service(RAGServiceClient(base_url="http://rag-service:8001"))
    # Agent has tools, but none is a RAGSearchToolWrapper (e.g. native / DB-only task).
    agent_tools = {"query_db": MagicMock(), "create_order": MagicMock()}

    assert service._resolve_judge_kb_search("db_task:0", agent_tools) is None


def test_judge_kb_is_none_when_no_rag_client_even_with_tool():
    # No container rag client → no judge KB, regardless of agent tools.
    service = _service(None)
    rag_client = RAGServiceClient(base_url="http://rag-service:8001")
    agent_tools = {"search_kb": _rag_search_tool(rag_client, "t:0")}

    assert service._resolve_judge_kb_search("t:0", agent_tools) is None
