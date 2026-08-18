"""Scaffolding for tests that drive ``RegisterTrial`` through the search plane.

Shared by the runner search-plane test modules: a recording stand-in for the
mcp_core search registry (absent from this repo), the knowledge-base task shape,
and the two ways an address reaches the runner — stack variables plus a
SecretManager key, or the task's own ``search`` block.

``tests/unit/test_runner_search_plane_refusal.py`` keeps its own copies of these
pieces by design; extend this module, not that one.
"""

from __future__ import annotations

import base64
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.secrets import SecretManager, init_default_from

DOMAIN = "retail_v3"
CORPUS = {"docindex/returns_policy.md": b"# Returns\nRefunds within 30 days.\n"}

STACK_HOST = "stack-typesense"
STACK_PORT = "9108"
STACK_KEY = "KEY-THE-STACK-REGISTERED"

TASK_HOST = "typesense"
TASK_PORT = 8108
TASK_KEY = "KEY-THE-TASK-CARRIES"
TASK_CONNECTION = {"host": TASK_HOST, "port": TASK_PORT, "api_key": TASK_KEY}


class RecordingSearchRegistry:
    """A stand-in ``initialize_typesense_for_domain`` recording the connection it got."""

    def __init__(self, *, client: Any) -> None:
        self._client = client
        self.connections: list[tuple[str, int, str | None]] = []

    def __call__(
        self, *, domain: str, snippets: list[str], host: str, port: int, api_key: str | None
    ) -> Any:
        self.connections.append((host, port, api_key))
        return self._client


class UsableClient:
    """What the registry hands back — the runner only reads ``is_available``."""

    is_available = True


def install_search_registry(
    monkeypatch: pytest.MonkeyPatch, *, client: Any
) -> RecordingSearchRegistry:
    """Put a recording stand-in where ``_init_typesense_for_trial`` imports it from."""
    registry = RecordingSearchRegistry(client=client)
    module = types.ModuleType("mcp_core.search.typesense_registry")
    module.initialize_typesense_for_domain = registry
    monkeypatch.setitem(sys.modules, "mcp_core", types.ModuleType("mcp_core"))
    monkeypatch.setitem(sys.modules, "mcp_core.search", types.ModuleType("mcp_core.search"))
    monkeypatch.setitem(sys.modules, "mcp_core.search.typesense_registry", module)
    return registry


def declare_stack_address(
    monkeypatch: pytest.MonkeyPatch, host: str | None, port: str | None, api_key: str | None
) -> None:
    """Put the runner in the container a stack built: address in env, key in the manager."""
    for name, value in (("TYPESENSE_HOST", host), ("TYPESENSE_PORT", port)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    payload = {} if api_key is None else {"TYPESENSE_API_KEY": api_key}
    init_default_from(SecretManager.from_dict(payload))


def kb_task(search: dict[str, Any]) -> dict:
    """A task declaring a knowledge base, its corpus riding in the artifacts."""
    return {
        "task_id": "kb_task",
        "name": "Knowledge Base Task",
        "category": "test",
        "description": "Declares a knowledge base a search plane must serve",
        "adapter_type": "tlk_mcp_core",
        "system_prompt": "You are a test assistant.",
        "initial_state": {"tables": {}, "schemas": []},
        "agent_tools": [],
        "user_tools": [],
        "search": search,
        "tool_artifacts": {
            path: base64.b64encode(content).decode() for path, content in CORPUS.items()
        },
    }


@contextmanager
def core_stack_runner(db_client: Any) -> Iterator[RunnerServiceImpl]:
    """A runner with no rag_client — the core-stack shape."""
    impl = RunnerServiceImpl(db_client)
    assert impl.rag_client is None
    try:
        yield impl
    finally:
        impl.shutdown()


def register_kb_task(service: Any, context: Any, trial_id: str, search: dict[str, Any]) -> Any:
    """Drive the real ``RegisterTrial`` with a knowledge-base task."""
    return service.RegisterTrial(
        register_request(trial_spec_json(kb_task(search), trial_id=trial_id), trial_id=trial_id),
        context,
    )
