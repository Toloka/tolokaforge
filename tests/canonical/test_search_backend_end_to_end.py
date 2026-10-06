"""A registered search backend, selected by ``search.plane``, end to end (ADR-0054).

One native task declares a corpus served by a backend the engine does not ship
(the in-memory reference, registered for the test) behind a tool it names itself.
The test follows it the whole way, through the real components:

* the native adapter builds the agent's schema from the declared name and
  description over the backend's parameters, and writes the backend into
  ``search.plane``;
* the trial spec crosses to the runner as ``conductor.py`` serialises it, and
  ``RegisterTrial`` builds the trial's index with that backend;
* the agent's ``ExecuteTool`` call is answered by that index, in the backend's own
  rendering;
* the judge's ``search_kb`` reads the same index object the agent's call read.

Nothing here names ``search_kb`` for the agent or ``rag_service``: the binding is the
task's declaration, at every step.

The default runs the same way: the shipped ``kb_lookup_01`` pack, which declares its
corpus alone, is served by ``rag_service`` through the runner's own rag-service client
(a stand-in answering the client and the judge's search from one store), and the
agent reads the JSON the rag-service backend renders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import write_yaml_file
from tests.utils.fake_rag_service import FakeRagService
from tests.utils.runner_requests import execute_request, register_request
from tests.utils.search_backends import register_search_backends
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.execution_mode import ExecutionMode
from tolokaforge.core.grading.judge_tools import SearchKbTool
from tolokaforge.core.grading.kb_search import RagServiceKnowledgeSearch
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.search.backend import SearchBackendContext
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.runner.tool_factory import SearchToolWrapper
from tolokaforge.testing.search_backends import InMemoryKnowledgeSearch, InMemorySearchBackend

pytestmark = pytest.mark.canonical

TRIAL_ID = "kb_task:0"
_RAG_PACK = Path(__file__).resolve().parents[2] / "examples" / "native" / "rag_search"
_RETURNS = "# Returns\n\nThe refund window is thirty days from delivery.\n"


@pytest.fixture
def built_backends(monkeypatch: pytest.MonkeyPatch) -> list[InMemorySearchBackend]:
    """Register ``in_memory`` and record every backend the engine builds from it."""
    built: list[InMemorySearchBackend] = []

    def factory(context: SearchBackendContext) -> InMemorySearchBackend:
        backend = InMemorySearchBackend(context)
        built.append(backend)
        return backend

    register_search_backends(monkeypatch, in_memory=factory)
    return built


def _pack(tmp_path: Path) -> NativeAdapter:
    task_dir = tmp_path / "tasks" / "kb_task"
    (task_dir / "kb").mkdir(parents=True)
    (task_dir / "kb" / "returns.md").write_text(_RETURNS)
    (task_dir / "kb" / "shipping.md").write_text("# Shipping\n\nOrders ship in two days.\n")
    (task_dir / "system_prompt.md").write_text("You answer policy questions.\n")
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "kb_task",
            "name": "kb task",
            "category": "kb_search",
            "description": "Answer from the knowledge base",
            "initial_state": {
                "rag": {
                    "corpus_dir": "kb",
                    "backend": "in_memory",
                    "backend_config": {"flavour": "overlap"},
                    "tool": {"name": "lookup_policy", "description": "Look a policy up."},
                }
            },
            "tools": {"agent": {"enabled": ["lookup_policy"]}, "user": {"enabled": []}},
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {"method": "weighted", "weights": {"transcript_rules": 1.0}},
            "components": {"transcript_rules": {"must_contain": ["thirty days"]}},
        },
    )
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})


def _trial_spec_json(description: Any, trial_id: str = TRIAL_ID) -> str:
    """The spec as ``conductor.py`` puts it on the wire."""
    spec = TrialSpec(
        trial_id=trial_id,
        run_id="e2e_run",
        task=description,
        execution_mode=ExecutionMode.ENGINE_LOOP,
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(db_url="http://db.test:8000", runner_url="http://r.test:50051"),
    )
    return spec.model_dump_json(exclude={"task": {"environment_manifest"}})


def test_the_declared_backend_serves_the_agent_and_the_judge_from_one_index(
    tmp_path: Path, built_backends: list[InMemorySearchBackend]
) -> None:
    description = _pack(tmp_path).to_task_description("kb_task")

    (tool,) = description.agent_tools
    assert (tool.name, tool.description, tool.source) == (
        "lookup_policy",
        "Look a policy up.",
        None,
    )
    assert set(tool.parameters["properties"]) == {"query"}
    wire_search = json.loads(description.model_dump_json())["search"]
    assert wire_search["plane"] == "in_memory"
    assert wire_search["tool_name"] == "lookup_policy"
    assert wire_search["backend_config"] == {"flavour": "overlap"}
    assert wire_search["enabled"] is False

    service = RunnerServiceImpl(db_client=MagicMock())
    context = MagicMock()
    try:
        registered = service.RegisterTrial(
            register_request(_trial_spec_json(description), trial_id=TRIAL_ID), context
        )
        assert registered.success is True, registered.error

        (runner_backend,) = [b for b in built_backends if b.context.trial_id == TRIAL_ID]
        assert runner_backend.backend_config == {"flavour": "overlap"}
        assert runner_backend.context.tool_name == "lookup_policy"
        assert runner_backend.context.tool_description == "Look a policy up."
        (index,) = runner_backend.indexes
        assert [doc_id for doc_id, _, _ in index.documents] == ["returns", "shipping"]

        answer = service.ExecuteTool(
            execute_request(
                TRIAL_ID, "lookup_policy", json.dumps({"query": "refund window"}), call_id="c1"
            ),
            context,
        )
        assert answer.status == pb2.EXECUTION_STATUS_SUCCESS, answer.error
        assert answer.output == f"[returns] {_RETURNS}"
        assert runner_backend.call_log.searches == [("refund window", {"query": "refund window"})]

        trial = service.trials[TRIAL_ID]
        agent_tool = trial.agent_tools["lookup_policy"]
        assert isinstance(agent_tool, SearchToolWrapper)
        assert agent_tool.index is index

        judge_search = trial.resolve_kb_search()
        assert isinstance(judge_search, InMemoryKnowledgeSearch)
        assert judge_search.index is index, "the judge must read the index the agent read"
        judged = SearchKbTool(judge_search).execute(query="refund window")
        assert judged.success is True
        assert "returns" in judged.output
        assert runner_backend.call_log.knowledge_searches == [("refund window", 5)]
    finally:
        service.shutdown()


def test_the_default_backend_serves_the_shipped_rag_pack_through_the_runners_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag_service = FakeRagService()
    rag_service.route_judge_posts(monkeypatch)
    adapter = NativeAdapter({"base_dir": str(_RAG_PACK), "tasks_glob": "**/task.yaml"})
    description = adapter.to_task_description("kb_lookup_01")
    wire_search = json.loads(description.model_dump_json())["search"]
    assert wire_search["plane"] == "rag_service"
    assert wire_search["enabled"] is True
    assert "backend_config" not in wire_search and "tool_name" not in wire_search

    trial_id = "kb_lookup_01:0"
    service = RunnerServiceImpl(db_client=MagicMock(), rag_client=rag_service.client())
    context = MagicMock()
    try:
        registered = service.RegisterTrial(
            register_request(_trial_spec_json(description, trial_id), trial_id=trial_id), context
        )
        assert registered.success is True, registered.error
        indexed = sorted(doc["source"] for doc in rag_service.indexes[trial_id])
        assert indexed == ["contacts.md", "maintenance_windows.md", "substation_procedures.md"]

        query = "Halden substation emergency failover authorization code"
        answer = service.ExecuteTool(
            execute_request(trial_id, "search_kb", json.dumps({"query": query}), call_id="c1"),
            context,
        )
        assert answer.status == pb2.EXECUTION_STATUS_SUCCESS, answer.error
        rendered = json.loads(answer.output)
        assert list(rendered) == ["results", "total", "query"]
        assert rendered["query"] == query
        assert rendered["total"] == len(rendered["results"]) >= 1
        assert {hit["source"] for hit in rendered["results"]} <= set(indexed)
        assert ("search", trial_id, {"query": query, "top_k": 5, "alpha": 0.5}) in (
            rag_service.requests
        )

        judge_search = service.trials[trial_id].resolve_kb_search()
        assert isinstance(judge_search, RagServiceKnowledgeSearch)
        judged = SearchKbTool(judge_search).execute(query=query)
        assert judged.success is True
        assert rendered["results"][0]["doc_id"] in judged.output
    finally:
        service.shutdown()
