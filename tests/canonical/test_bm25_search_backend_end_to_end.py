"""The built-in ``bm25`` backend, selected by ``initial_state.rag.backend``, end to end.

One native task declares a JSON corpus served by ``bm25`` with the text renderer,
behind a tool it names itself. The test follows it through the real components:

* the native adapter bundles the ``.json`` documents, builds the agent's schema
  from the declared name over the backend's parameters (``query`` alone), writes
  ``bm25`` into ``search.plane`` with the task's ``backend_config``, and leaves
  ``search.enabled`` false — no rag-service;
* the orchestrator's stack rule keeps such a task on the core stack;
* ``RegisterTrial`` on a runner with no rag-service client builds the trial's
  index in process from the extracted corpus;
* the agent's ``ExecuteTool`` call is answered in the configured text shape, hits
  numbered, titles and ids from the documents, a measured timing suffix;
* the judge's ``search_kb`` reads the same index object and gets whole documents.

Every document is synthetic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import write_yaml_file
from tests.utils.runner_requests import execute_request, register_request
from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.judge_tools import SearchKbTool
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.orchestrator import _tasks_need_full_stack
from tolokaforge.core.search.bm25 import Bm25KnowledgeSearch, Bm25SearchIndex, clear_index_cache
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.runner.tool_factory import SearchToolWrapper

pytestmark = pytest.mark.canonical

TRIAL_ID = "bm25_task:0"
_PLANTED_CODE = "ZK17-VELMAR-2208"

_DOCUMENTS = {
    "001_contacts.json": {
        "id": "contacts",
        "title": "On-call contacts",
        "content": "The on-call grid engineer is reached through the operations desk.",
    },
    "002_failover.json": {
        "id": "failover-halden",
        "title": "Halden substation emergency failover",
        "content": (
            f"Emergency failover at the Halden substation requires authorization code "
            f"{_PLANTED_CODE} entered at the control console."
        ),
    },
    "003_windows.json": {
        "id": "maintenance-windows",
        "title": "Maintenance windows",
        "content": "Routine maintenance windows open on the first Tuesday of each month.",
    },
}

_BACKEND_CONFIG: dict[str, Any] = {
    "ranking": {"top_k": 2},
    "render": {"kind": "text", "timing_suffix": "measured"},
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_index_cache()


def _pack(tmp_path: Path) -> tuple[NativeAdapter, Path]:
    task_dir = tmp_path / "tasks" / "bm25_task"
    corpus = task_dir / "kb"
    corpus.mkdir(parents=True)
    for name, document in _DOCUMENTS.items():
        (corpus / name).write_text(json.dumps(document), encoding="utf-8")
    (corpus / "_README.md").write_text("Notes about this corpus; not a document.\n")
    (task_dir / "system_prompt.md").write_text("You answer operations questions.\n")
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "bm25_task",
            "name": "bm25 task",
            "category": "kb_search",
            "description": "Find the failover code in the knowledge base",
            "initial_state": {
                "rag": {
                    "corpus_dir": "kb",
                    "backend": "bm25",
                    "backend_config": _BACKEND_CONFIG,
                    "tool": {"name": "search_docs", "description": "Search the operations docs."},
                }
            },
            "tools": {"agent": {"enabled": ["search_docs"]}, "user": {"enabled": []}},
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {"method": "weighted", "weights": {"transcript_rules": 1.0}},
            "components": {"transcript_rules": {"must_contain": [_PLANTED_CODE]}},
        },
    )
    adapter = NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})
    return adapter, task_dir / "task.yaml"


def _trial_spec_json(description: Any) -> str:
    """The spec as ``conductor.py`` puts it on the wire."""
    spec = TrialSpec(
        trial_id=TRIAL_ID,
        run_id="e2e_run",
        task=description,
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(db_url="http://db.test:8000", runner_url="http://r.test:50051"),
    )
    return spec.model_dump_json(exclude={"task": {"environment_manifest"}})


def test_bm25_serves_the_agent_and_the_judge_from_one_in_process_index(tmp_path: Path) -> None:
    adapter, task_yaml = _pack(tmp_path)

    task, _ = load_task_yaml(task_yaml)
    assert _tasks_need_full_stack([task]) is False, "bm25 declares no stack service"

    description = adapter.to_task_description("bm25_task")
    (tool,) = description.agent_tools
    assert (tool.name, tool.description, tool.source) == (
        "search_docs",
        "Search the operations docs.",
        None,
    )
    assert tool.parameters["properties"].keys() == {"query"}
    wire = json.loads(description.model_dump_json())
    assert wire["search"]["plane"] == "bm25"
    assert wire["search"]["enabled"] is False
    assert wire["search"]["tool_name"] == "search_docs"
    assert wire["search"]["backend_config"] == _BACKEND_CONFIG
    assert set(wire["tool_artifacts"]) == {f"kb/{name}" for name in _DOCUMENTS} | {"kb/_README.md"}

    service = RunnerServiceImpl(db_client=MagicMock())  # no rag-service client at all
    context = MagicMock()
    try:
        registered = service.RegisterTrial(
            register_request(_trial_spec_json(description), trial_id=TRIAL_ID), context
        )
        assert registered.success is True, registered.error

        trial = service.trials[TRIAL_ID]
        agent_tool = trial.agent_tools["search_docs"]
        assert isinstance(agent_tool, SearchToolWrapper)
        index = agent_tool.index
        assert isinstance(index, Bm25SearchIndex)
        assert [d.id for d in index.corpus.documents] == [
            "contacts",
            "failover-halden",
            "maintenance-windows",
        ], "file-name order, the _README skipped"

        query = "Halden substation failover authorization code"
        answer = service.ExecuteTool(
            execute_request(TRIAL_ID, "search_docs", json.dumps({"query": query}), call_id="c1"),
            context,
        )
        assert answer.status == pb2.EXECUTION_STATUS_SUCCESS, answer.error
        body, timing = answer.output.rsplit("\n\n[Timing: ", 1)
        assert re.fullmatch(r"retrieval=\d+ms, total=\d+ms\]", timing), timing
        first, second = body.split("\n\n")
        assert first.startswith("1. Halden substation emergency failover\n   ID: failover-halden\n")
        assert re.search(r"\n   Score: \d+\.\d{4}\n", first)
        assert first.endswith(f"{_PLANTED_CODE} entered at the control console.")
        assert second.startswith("2. ")
        assert len(body.split("\n\n")) == 2, "top_k: 2"

        empty = service.ExecuteTool(
            execute_request(TRIAL_ID, "search_docs", json.dumps({"query": "  "}), call_id="c2"),
            context,
        )
        assert empty.status == pb2.EXECUTION_STATUS_SUCCESS, empty.error
        assert empty.output.startswith("No results found.\n\n[Timing: ")

        judge_search = trial.resolve_kb_search()
        assert isinstance(judge_search, Bm25KnowledgeSearch)
        assert judge_search.index is index, "the judge must read the index the agent read"
        judged = SearchKbTool(judge_search).execute(query=query)
        assert judged.success is True
        assert "failover-halden" in judged.output
        top, *_rest = judge_search.search(query, top_k=1)
        assert top.doc_id == "failover-halden"
        assert top.text == _DOCUMENTS["002_failover.json"]["content"], "whole documents"
    finally:
        service.shutdown()


def test_a_backend_config_bm25_refuses_is_one_refusal_at_load(tmp_path: Path) -> None:
    """The orchestrator-side build reads the declaration; the agent schema is never built."""
    adapter, _ = _pack(tmp_path)
    task_yaml = tmp_path / "tasks" / "bm25_task" / "task.yaml"
    raw = task_yaml.read_text()
    task_yaml.write_text(raw.replace("top_k: 2", "top_k: 0"))

    with pytest.raises(Exception, match="top_k") as excinfo:
        adapter.to_task_description("bm25_task")
    assert "bm25" in str(excinfo.value)
