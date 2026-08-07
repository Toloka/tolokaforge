"""A trial whose declared search plane cannot work is refused, not run.

``RegisterTrial`` used to warn and register anyway on every search-plane
failure, so a task with a knowledge base spent its paid turns on
``search_policy`` calls that could never succeed — and the run graded the result
as measured agent behaviour (#926). Registration now refuses, and the refusal
costs zero turns because ``Conductor._setup_trial`` turns it into a trial
failure before the agent loop.

Two classes of failure are locked here: the plane is broken (no client, no
server, an unusable client) and the corpus is broken (a declared knowledge base
that did not survive bundling). Both name the trial, the domain and the address
tried.

Equally locked is what must NOT happen: a task with no knowledge base does no
TypeSense work at all, on all three shapes that reach registration. The gate is
a conjunction of a run-level and a task-level half, and the half that is easy to
lose is the task-level one — a knowledge-base task in a TypeSense-disabled run
must still register.

``RunnerServiceImpl`` is real and drives its real ``RegisterTrial``; only the
mcp_core search registry — absent from this repo — is a stand-in, installed at
the import site the runner reads.
"""

from __future__ import annotations

import base64
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit

DOMAIN = "retail_v3"
ADDRESS = "typesense:8108"
GOOD_CORPUS = {"docindex/returns_policy.md": b"# Returns\nRefunds within 30 days.\n"}

# A knowledge-base task in a TypeSense-enabled run: both halves of the gate hold.
KB_SEARCH = {
    "enabled": False,
    "host": "typesense",
    "port": 8108,
    "domain_name": DOMAIN,
    "documents_path": "docindex",
}


class _Client:
    """What the registry hands back — the runner only reads ``is_available``."""

    def __init__(self, *, available: bool = True) -> None:
        self.is_available = available


_USABLE_CLIENT = object()
"""Default ``_Registry`` result — ``result=None`` is the row that returns no client."""


class _Registry:
    """A stand-in ``initialize_typesense_for_domain`` that records its calls."""

    def __init__(self, result: Any = _USABLE_CLIENT, raises: Exception | None = None) -> None:
        self._result = _Client() if result is _USABLE_CLIENT else result
        self._raises = raises
        self.calls: list[str] = []

    def __call__(self, *, domain: str, snippets: list[str], **_kwargs: Any) -> Any:
        self.calls.append(domain)
        if self._raises is not None:
            raise self._raises
        return self._result


def _install_registry(monkeypatch: pytest.MonkeyPatch, registry: _Registry) -> _Registry:
    """Put the stand-in where ``_init_typesense_for_trial`` imports it from."""
    module = types.ModuleType("mcp_core.search.typesense_registry")
    module.initialize_typesense_for_domain = registry
    monkeypatch.setitem(sys.modules, "mcp_core", types.ModuleType("mcp_core"))
    monkeypatch.setitem(sys.modules, "mcp_core.search", types.ModuleType("mcp_core.search"))
    monkeypatch.setitem(sys.modules, "mcp_core.search.typesense_registry", module)
    return registry


def _task(search: dict[str, Any] | None, artifacts: dict[str, bytes] | None = None) -> dict:
    task: dict[str, Any] = {
        "task_id": "kb_task",
        "name": "Knowledge Base Task",
        "category": "test",
        "description": "Declares a knowledge base the search plane must serve",
        "adapter_type": "tlk_mcp_core",
        "system_prompt": "You are a test assistant.",
        "initial_state": {"tables": {}, "schemas": []},
        "agent_tools": [],
        "user_tools": [],
    }
    if search is not None:
        task["search"] = search
    if artifacts is not None:
        task["tool_artifacts"] = {
            path: base64.b64encode(content).decode() for path, content in artifacts.items()
        }
    return task


@pytest.fixture
def service(db_client) -> Any:
    """A runner with no rag_client — the core-stack shape."""
    impl = RunnerServiceImpl(db_client)
    assert impl.rag_client is None
    try:
        yield impl
    finally:
        impl.shutdown()


def _register(service: Any, context: Any, trial_id: str, task: dict) -> Any:
    return service.RegisterTrial(
        register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id), context
    )


# ---------------------------------------------------------------------------
# The plane is broken — the six rows, part one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "registry", "fragment"),
    [
        ("no-client", None, "cannot provide a search client"),
        ("no-server", _Registry(result=None), "unreachable or refused the collection"),
        ("init-raises", _Registry(raises=RuntimeError("connection refused")), "connection refused"),
        ("unusable-client", _Registry(result=_Client(available=False)), "reports the server as"),
    ],
)
def test_a_broken_search_plane_refuses_registration(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    registry: _Registry | None,
    fragment: str,
) -> None:
    """Every way the client can fail to arrive costs the trial nothing."""
    if registry is not None:
        _install_registry(monkeypatch, registry)
    else:
        monkeypatch.delitem(sys.modules, "mcp_core", raising=False)

    response = _register(
        service, mock_grpc_context, f"plane_{row}:0", _task(KB_SEARCH, GOOD_CORPUS)
    )

    assert response.success is False
    assert fragment in response.error
    assert ADDRESS in response.error
    assert DOMAIN in response.error


# ---------------------------------------------------------------------------
# The corpus is broken — the six rows, part two
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "artifacts", "fragment"),
    [
        (
            "unreadable-document",
            {"docindex/broken.md": b"\xff\xfe not utf-8"},
            "cannot read",
        ),
        (
            "corpus-did-not-arrive",
            {"tools/placeholder.py": b"# no docindex/ shipped\n"},
            "did not survive bundling",
        ),
    ],
)
def test_a_broken_corpus_refuses_registration(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    artifacts: dict[str, bytes],
    fragment: str,
) -> None:
    """A declared knowledge base that did not arrive is an adapter bug, refused loudly."""
    _install_registry(monkeypatch, _Registry())

    response = _register(service, mock_grpc_context, f"corpus_{row}:0", _task(KB_SEARCH, artifacts))

    assert response.success is False
    assert fragment in response.error
    assert ADDRESS in response.error
    assert DOMAIN in response.error


def test_the_unreadable_document_is_named(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator is told which file, not just that one of them failed."""
    _install_registry(monkeypatch, _Registry())

    response = _register(
        service,
        mock_grpc_context,
        "corpus_named:0",
        _task(KB_SEARCH, {"docindex/broken.md": b"\xff\xfe not utf-8"}),
    )

    assert "broken.md" in response.error


# ---------------------------------------------------------------------------
# What must keep working
# ---------------------------------------------------------------------------


def test_a_usable_client_registers_the_trial(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: the gate fires, the registry is called, registration succeeds."""
    registry = _install_registry(monkeypatch, _Registry())

    response = _register(service, mock_grpc_context, "plane_ok:0", _task(KB_SEARCH, GOOD_CORPUS))

    assert response.success is True, response.error
    assert registry.calls == [DOMAIN]
    assert "plane_ok:0" in service.trials


@pytest.mark.parametrize(
    ("shape", "search"),
    [
        ("no-search-block", None),
        (
            "connection-configured-but-no-kb",
            {"enabled": False, "host": "typesense", "port": 8108, "domain_name": DOMAIN},
        ),
        (
            "kb-task-in-a-typesense-disabled-run",
            {"enabled": False, "domain_name": DOMAIN, "documents_path": "docindex"},
        ),
    ],
)
def test_a_task_without_both_halves_does_no_typesense_work(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    search: dict[str, Any] | None,
) -> None:
    """The gate is a conjunction: neither half alone reaches the search plane.

    The third shape is the one both rejected gate formulations got wrong — a
    knowledge-base task in a run that configured no TypeSense plane.
    """
    registry = _install_registry(monkeypatch, _Registry())

    response = _register(service, mock_grpc_context, f"nokb_{shape}:0", _task(search, GOOD_CORPUS))

    assert response.success is True, response.error
    assert registry.calls == []


def test_a_rag_corpus_task_reaches_the_rag_plane_not_the_typesense_one(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``enabled`` + documents_path with no host is the rag-service shape, not ours."""
    registry = _install_registry(monkeypatch, _Registry())
    search = {"enabled": True, "domain_name": DOMAIN, "documents_path": "docindex"}

    response = _register(service, mock_grpc_context, "rag_only:0", _task(search, GOOD_CORPUS))

    assert response.success is False
    assert "RAG service not configured" in response.error
    assert registry.calls == []


def test_a_refused_registration_drops_the_extracted_artifacts(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction ran before the gate, so a refusal must not leak the tmp dir."""
    _install_registry(monkeypatch, _Registry(result=None))
    extracted: list[Path] = []
    extract = service._extract_tool_artifacts

    def _record(trial_id: str, artifacts: dict[str, str]) -> Path:
        path = extract(trial_id, artifacts)
        extracted.append(path)
        return path

    monkeypatch.setattr(service, "_extract_tool_artifacts", _record)

    response = _register(service, mock_grpc_context, "cleanup:0", _task(KB_SEARCH, GOOD_CORPUS))

    assert response.success is False
    assert len(extracted) == 1
    assert not extracted[0].exists()
    assert str(extracted[0]) not in sys.path
