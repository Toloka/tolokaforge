"""The native adapter builds the search tool and ``search`` block from the declared backend.

``initial_state.rag`` names a backend (``backend``), its opaque ``backend_config`` and
the agent's tool (``tool.name`` / ``tool.description``). The adapter carries the backend
on the wire as ``search.plane``, builds the agent's schema from the declared name and
description over the backend's own ``tool_parameters()``, and reads the declared name
wherever it read the literal ``search_kb`` before: the "corpus ⇒ tool enabled" check
and the grading tool inventory.

The default path — no ``backend``, no ``tool`` — is pinned by
``test_native_adapter_rag_search.py`` and the canonical snapshots; this module drives
a registered in-memory backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import write_yaml_file
from tests.utils.search_backends import register_search_backends
from tolokaforge.adapters._task_loader import build_tool_inventory, load_task_yaml
from tolokaforge.adapters.native import NativeAdapter, NativeAdapterMisconfigurationError
from tolokaforge.core.plugin_registry import ReservedNameError, UnknownImplementationError
from tolokaforge.testing.search_backends import (
    InMemorySearchBackend,
    SearchBackendDefects,
    in_memory_search_backend_factory,
)

pytestmark = pytest.mark.unit


def _no_query_factory(context: Any) -> InMemorySearchBackend:
    return InMemorySearchBackend(
        context, defects=SearchBackendDefects(parameters_without_query=True)
    )


@pytest.fixture(autouse=True)
def _in_memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    register_search_backends(
        monkeypatch, in_memory=in_memory_search_backend_factory, no_query=_no_query_factory
    )


def _task_dir(tmp_path: Path, *, rag: dict[str, Any], agent_tools: list[str]) -> Path:
    task_dir = tmp_path / "tasks" / "kb_task"
    corpus = task_dir / "kb"
    corpus.mkdir(parents=True)
    (corpus / "returns.md").write_text("# Returns\n\nThe refund window is 30 days.\n")
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "kb_task",
            "name": "kb task",
            "category": "kb_search",
            "description": "kb task",
            "initial_state": {"json_db": "initial_state.json", "rag": rag},
            "tools": {"agent": {"enabled": agent_tools}, "user": {"enabled": []}},
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {"method": "weighted", "weights": {"state_checks": 1.0}},
            "components": {"state_checks": {"jsonpaths": []}},
        },
    )
    return task_dir


def _describe(tmp_path: Path, **kwargs: Any) -> Any:
    _task_dir(tmp_path, **kwargs)
    adapter = NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})
    return adapter.to_task_description("kb_task")


_DECLARED = {
    "corpus_dir": "kb",
    "backend": "in_memory",
    "backend_config": {"ranking": "overlap"},
    "tool": {"name": "lookup_docs", "description": "Look a policy up."},
}


def test_the_declared_backend_rides_in_search_plane(tmp_path: Path) -> None:
    td = _describe(tmp_path, rag=_DECLARED, agent_tools=["lookup_docs"])

    assert td.search.plane == "in_memory"
    assert td.search.enabled is False, "in_memory declares no rag-service stack service"
    assert td.search.documents_path == "kb"
    assert td.search.backend_config == {"ranking": "overlap"}
    assert td.search.tool_name == "lookup_docs"
    wire = td.model_dump(mode="json")["search"]
    assert wire["backend_config"] == {"ranking": "overlap"}
    assert wire["tool_name"] == "lookup_docs"
    assert "kb/returns.md" in td.tool_artifacts


def test_the_agent_schema_is_the_declared_tool_over_the_backends_parameters(
    tmp_path: Path,
) -> None:
    td = _describe(tmp_path, rag=_DECLARED, agent_tools=["lookup_docs"])

    (tool,) = td.agent_tools
    assert tool.name == "lookup_docs"
    assert tool.description == "Look a policy up."
    assert tool.parameters == {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Words to look up"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    assert tool.source is None, "the runner binds the search tool by name, not by a source"


def test_a_search_kb_tool_with_another_backend_is_that_backends_tool(tmp_path: Path) -> None:
    """The name is the declaration's, not a builtin's: ``search_kb`` over in_memory."""
    rag = {"corpus_dir": "kb", "backend": "in_memory"}
    td = _describe(tmp_path, rag=rag, agent_tools=["search_kb"])

    (tool,) = td.agent_tools
    assert tool.name == "search_kb"
    assert set(tool.parameters["properties"]) == {"query"}
    assert td.search.plane == "in_memory"
    assert "tool_name" not in td.model_dump(mode="json")["search"]


def test_a_corpus_without_the_declared_tool_is_refused_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no actor enables the 'lookup_docs' tool"):
        _describe(tmp_path, rag=_DECLARED, agent_tools=["calculator"])


def test_search_kb_beside_a_renamed_search_tool_is_no_builtin(tmp_path: Path) -> None:
    """Once the task names its tool, ``search_kb`` is just an unknown source-less name."""
    with pytest.raises(
        NativeAdapterMisconfigurationError, match="Tool 'search_kb' has no source configuration"
    ):
        _describe(tmp_path, rag=_DECLARED, agent_tools=["lookup_docs", "search_kb"])


def test_the_inventory_carries_the_declared_tools_parameters(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path, rag=_DECLARED, agent_tools=["lookup_docs"])
    task, _ = load_task_yaml(task_dir / "task.yaml")

    inventory = build_tool_inventory(task, task_dir)

    assert inventory.declared == {"lookup_docs"}
    assert set(inventory.parameters["lookup_docs"]["properties"]) == {"query"}


def test_an_unregistered_backend_is_refused(tmp_path: Path) -> None:
    rag = {"corpus_dir": "kb", "backend": "ghost"}
    with pytest.raises(UnknownImplementationError, match="'ghost'"):
        _describe(tmp_path, rag=rag, agent_tools=["search_kb"])


def test_typesense_is_not_a_backend_a_native_task_can_declare(tmp_path: Path) -> None:
    """``typesense`` is the plane an adapter that indexes host-side declares itself."""
    rag = {"corpus_dir": "kb", "backend": "typesense"}
    with pytest.raises(ReservedNameError):
        _describe(tmp_path, rag=rag, agent_tools=["search_kb"])


def test_a_backend_declaring_no_query_parameter_is_refused(tmp_path: Path) -> None:
    """The runner reads ``query`` off the agent's call; a schema without it searches nothing."""
    rag = {"corpus_dir": "kb", "backend": "no_query"}
    with pytest.raises(ValueError, match="no 'query' property"):
        _describe(tmp_path, rag=rag, agent_tools=["search_kb"])
