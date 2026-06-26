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

from tolokaforge.core.grading.judge_tools import DelegatingReadTool
from tolokaforge.core.grading.kb_search import RagServiceKnowledgeSearch
from tolokaforge.runner.models import ToolSchema
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


# ---------------------------------------------------------------------------
# TypeSense plane: the judge reuses the agent's reconstructed ``search_policy``
# tool via a read-only passthrough, bridged through the runner event loop.
# ---------------------------------------------------------------------------


class _FakeReconstructedSearchPolicy:
    """A minimal stand-in for the agent's reconstructed ``search_policy`` tool.

    Mirrors the ``ToolWrapper`` surface the runner relies on: ``.tool_schema``
    (a real ``ToolSchema`` with ``name``/``description``/``parameters``) and an
    ASYNC ``execute(arguments) -> str``. No mcp_core, no TypeSense.
    """

    def __init__(self) -> None:
        self.tool_schema = ToolSchema(
            name="search_policy",
            description="Search the TypeSense policy KB",
            parameters={
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
            category="read",
        )
        self.name = "search_policy"
        self.received: list[dict] = []

    async def execute(self, arguments: dict) -> str:
        self.received.append(arguments)
        return f"policy-doc-for:{arguments['question']}"


def _trial_context_with(agent_tools: dict):
    ctx = MagicMock()
    ctx.agent_tools = agent_tools
    return ctx


def test_search_policy_passthrough_offered_only_when_agent_had_it():
    service = _service(None)
    try:
        # Agent had search_policy → exactly one passthrough, with its real schema.
        agent_tool = _FakeReconstructedSearchPolicy()
        tools = service._build_judge_search_policy_tools(
            _trial_context_with({"search_policy": agent_tool, "other": MagicMock()})
        )
        assert len(tools) == 1
        passthrough = tools[0]
        assert isinstance(passthrough, DelegatingReadTool)
        # The judge sees the agent tool's EXACT schema (so it calls it correctly).
        assert passthrough.get_schema()["function"] == {
            "name": "search_policy",
            "description": "Search the TypeSense policy KB",
            "parameters": agent_tool.tool_schema.parameters,
        }

        # Agent lacked search_policy → no passthrough (mirror the agent).
        assert (
            service._build_judge_search_policy_tools(_trial_context_with({"query_db": MagicMock()}))
            == []
        )
    finally:
        service.shutdown()


def test_search_policy_passthrough_bridges_async_execute_and_passes_args():
    """The sync judge tool drives the agent tool's ASYNC execute via _run_async."""
    service = _service(None)
    try:
        agent_tool = _FakeReconstructedSearchPolicy()
        tools = service._build_judge_search_policy_tools(
            _trial_context_with({"search_policy": agent_tool})
        )
        result = tools[0].execute(question="refund window")

        assert result.success is True
        assert result.output == "policy-doc-for:refund window"
        # Args were passed through verbatim to the agent's async tool.
        assert agent_tool.received == [{"question": "refund window"}]
    finally:
        service.shutdown()


def test_search_policy_passthrough_fails_loud_when_agent_tool_raises():
    service = _service(None)

    class _Boom(_FakeReconstructedSearchPolicy):
        async def execute(self, arguments: dict) -> str:
            raise RuntimeError("typesense unavailable")

    try:
        tools = service._build_judge_search_policy_tools(
            _trial_context_with({"search_policy": _Boom()})
        )
        result = tools[0].execute(question="x")
        assert result.success is False
        assert "typesense unavailable" in (result.error or "")
    finally:
        service.shutdown()
