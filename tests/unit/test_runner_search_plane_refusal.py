"""A trial whose declared search plane cannot work is refused, not run.

A task with a knowledge base whose ``search_policy`` calls cannot succeed would
spend its paid turns on them, and the run would grade the result as measured
agent behaviour (#926). ``RegisterTrial`` refuses instead, and the refusal costs
zero turns because ``Conductor._setup_trial`` turns it into a trial failure
before the agent loop.

Three classes of failure are locked here: the plane is broken (no client, no
server, an unusable client), the corpus is broken (a declared knowledge base
that did not survive bundling), and the declaration and the bundle disagree (a
``docindex/`` corpus arrived for a task declaring no ``documents_path``, so the
registration gate would skip the plane and the trial would run without one).
Every message names the trial, the domain and the address tried.

Equally locked is what must NOT happen: a task with no knowledge base does no
TypeSense work at all. The gate is a conjunction of a run-level and a task-level
half, and the half that is easy to lose is the task-level one — a knowledge-base
task in a TypeSense-disabled run must still register.

``RunnerServiceImpl`` is real and drives its real ``RegisterTrial``; only the
mcp_core search registry — absent from this repo — is a stand-in, installed at
the import site the runner reads.
"""

from __future__ import annotations

import base64
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit

DOMAIN = "retail_v3"
ADDRESS = "typesense:8108"
GOOD_CORPUS = {"docindex/returns_policy.md": b"# Returns\nRefunds within 30 days.\n"}
NO_CORPUS = {"tools/placeholder.py": b"# artifacts arrived, but no docindex/\n"}

# A knowledge-base task in a TypeSense-enabled run: both halves of the gate hold.
KB_SEARCH = {
    "enabled": False,
    "host": "typesense",
    "port": 8108,
    "domain_name": DOMAIN,
    "documents_path": "docindex",
}

# The run-level half alone: a TypeSense plane configured, no knowledge base declared.
CONNECTION_ONLY_SEARCH = {
    "enabled": False,
    "host": "typesense",
    "port": 8108,
    "domain_name": DOMAIN,
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
# The plane is broken — rows 1 to 4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "make_registry", "fragment"),
    [
        ("no-client", None, "cannot provide a search client"),
        ("no-server", lambda: _Registry(result=None), "unreachable or refused the collection"),
        (
            "init-raises",
            lambda: _Registry(raises=RuntimeError("connection refused")),
            "connection refused",
        ),
        (
            "unusable-client",
            lambda: _Registry(result=_Client(available=False)),
            "reports the server as",
        ),
    ],
)
def test_a_broken_search_plane_refuses_registration(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    make_registry: Callable[[], _Registry] | None,
    fragment: str,
) -> None:
    """Every way the client can fail to arrive costs the trial nothing."""
    if make_registry is not None:
        _install_registry(monkeypatch, make_registry())
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
# The corpus is broken — rows 5 and 6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "artifacts", "fragment"),
    [
        (
            "unreadable-document",
            {"docindex/broken.md": b"\xff\xfe not utf-8"},
            "cannot read",
        ),
        ("corpus-did-not-arrive", NO_CORPUS, "did not survive bundling"),
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


# ---------------------------------------------------------------------------
# The declaration and the bundle disagree
# ---------------------------------------------------------------------------


def test_a_bundled_corpus_the_task_never_declared_refuses_registration(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``docindex/`` corpus arrived, but nothing declared it, so nothing would serve it.

    Both halves of the gate would have to hold for a client to be registered.
    Only the run-level one does, so registration would otherwise succeed with a
    dead plane — the exact shape refusing exists to stop.
    """
    registry = _install_registry(monkeypatch, _Registry())

    response = _register(
        service, mock_grpc_context, "bundle_mismatch:0", _task(CONNECTION_ONLY_SEARCH, GOOD_CORPUS)
    )

    assert response.success is False
    assert "disagree" in response.error
    assert "documents_path" in response.error
    assert ADDRESS in response.error
    assert DOMAIN in response.error
    assert registry.calls == []


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
    ("shape", "search", "artifacts"),
    [
        ("no-search-block", None, GOOD_CORPUS),
        ("connection-configured-and-no-corpus-arrived", CONNECTION_ONLY_SEARCH, NO_CORPUS),
    ],
)
def test_a_task_without_both_halves_does_no_typesense_work(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    search: dict[str, Any] | None,
    artifacts: dict[str, bytes],
) -> None:
    """The gate is a conjunction: neither half alone reaches the search plane.

    The first shape carries a corpus with no search declaration at all, so the
    task never asked for one to fire; the second declares only a connection
    with no corpus arriving, so there is nothing to serve. Neither shape is
    the silently-broken class ``test_a_kb_task_in_a_no_plane_run_refuses`` locks.
    """
    registry = _install_registry(monkeypatch, _Registry())

    response = _register(service, mock_grpc_context, f"nokb_{shape}:0", _task(search, artifacts))

    assert response.success is True, response.error
    assert registry.calls == []


def test_a_kb_task_in_a_no_plane_run_refuses(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A knowledge-base task in a run that resolves no TypeSense plane must refuse loudly.

    An adapter mid-migration can produce this shape: ``search.documents_path``
    declares a corpus, ``search.plane`` is unset, and the run configured no
    connection details — the ``mode: disabled`` shape. Silent registration
    would let every ``search_policy`` call fail on paid turns and grade the
    agent for the misconfiguration.
    """
    registry = _install_registry(monkeypatch, _Registry())
    search = {"enabled": False, "domain_name": DOMAIN, "documents_path": "docindex"}

    response = _register(
        service,
        mock_grpc_context,
        "nokb_kb-task-in-a-typesense-disabled-run:0",
        _task(search, GOOD_CORPUS),
    )

    assert response.success is False
    assert "neither plane serves" in response.error
    assert "search.plane is unset" in response.error
    assert DOMAIN in response.error
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


def test_a_broken_typesense_plane_is_reported_before_the_missing_rag_service(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate ordering: a task declaring both planes reports the TypeSense failure.

    ``enabled: true`` on this rag_client-less runner would fail on its own. The
    TypeSense gate runs first, so the operator reads the nearer failure — the
    search client that never arrived — rather than a RAG error that is true but
    downstream.
    """
    _install_registry(monkeypatch, _Registry(result=None))
    search = {**KB_SEARCH, "enabled": True}

    response = _register(service, mock_grpc_context, "both_planes:0", _task(search, GOOD_CORPUS))

    assert response.success is False
    assert "unreachable or refused the collection" in response.error
    assert "RAG service not configured" not in response.error


@pytest.mark.parametrize(
    ("row", "search"),
    [
        ("plane-broken", KB_SEARCH),
        ("bundle-disagreement", CONNECTION_ONLY_SEARCH),
    ],
)
def test_a_refused_registration_drops_the_extracted_artifacts(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    search: dict[str, Any],
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

    response = _register(service, mock_grpc_context, f"cleanup_{row}:0", _task(search, GOOD_CORPUS))

    assert response.success is False
    assert len(extracted) == 1
    assert not extracted[0].exists()
    assert str(extracted[0]) not in sys.path
