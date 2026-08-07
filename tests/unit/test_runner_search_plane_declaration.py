"""A task says which plane serves its corpus, and the runner takes it at its word.

The address a knowledge base is reached at belongs to the stack; which plane
serves that knowledge base belongs to the task. Once the address stops being a
per-task fact, nothing else in the description distinguishes a TypeSense corpus
from a rag-service one — so the task declares it, and the declaration is what the
registration gate reads.

Until every adapter declares one, a task carrying connection details and no plane
is read as TypeSense; the runner reports that it worked the plane out rather than
being told. An adapter part-way through that migration — the corpus declared, the
address dropped, the plane not yet declared — is refused rather than registered,
because registering it would hand the trial a ``search_policy`` tool with nothing
behind it.

``RunnerServiceImpl`` is real and drives its real ``RegisterTrial``; only the
mcp_core search registry — absent from this repo — is a stand-in, installed at
the import site the runner reads.
"""

from __future__ import annotations

import base64
import sys
import types
from typing import Any

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.secrets import SecretManager, init_default_from

pytestmark = pytest.mark.unit

DOMAIN = "retail_v3"
CORPUS = {"docindex/returns_policy.md": b"# Returns\nRefunds within 30 days.\n"}

STACK_HOST = "stack-typesense"
STACK_PORT = "9108"
STACK_KEY = "KEY-THE-STACK-REGISTERED"

TASK_HOST = "typesense"
TASK_PORT = 8108
TASK_KEY = "KEY-THE-TASK-CARRIES"
TASK_CONNECTION = {"host": TASK_HOST, "port": TASK_PORT, "api_key": TASK_KEY}


class _Registry:
    """A stand-in ``initialize_typesense_for_domain`` recording the connection it got."""

    def __init__(self, *, client: Any) -> None:
        self._client = client
        self.connections: list[tuple[str, int, str | None]] = []

    def __call__(
        self, *, domain: str, snippets: list[str], host: str, port: int, api_key: str | None
    ) -> Any:
        self.connections.append((host, port, api_key))
        return self._client


class _UsableClient:
    """What the registry hands back — the runner only reads ``is_available``."""

    is_available = True


def _install_registry(monkeypatch: pytest.MonkeyPatch, client: Any = _UsableClient) -> _Registry:
    """Put the stand-in where ``_init_typesense_for_trial`` imports it from."""
    registry = _Registry(client=client)
    module = types.ModuleType("mcp_core.search.typesense_registry")
    module.initialize_typesense_for_domain = registry
    monkeypatch.setitem(sys.modules, "mcp_core", types.ModuleType("mcp_core"))
    monkeypatch.setitem(sys.modules, "mcp_core.search", types.ModuleType("mcp_core.search"))
    monkeypatch.setitem(sys.modules, "mcp_core.search.typesense_registry", module)
    return registry


def _stack_offers_typesense(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the runner in the container a stack built: address in env, key in the manager."""
    monkeypatch.setenv("TYPESENSE_HOST", STACK_HOST)
    monkeypatch.setenv("TYPESENSE_PORT", STACK_PORT)
    init_default_from(SecretManager.from_dict({"TYPESENSE_API_KEY": STACK_KEY}))


def _task(search: dict[str, Any]) -> dict:
    return {
        "task_id": "kb_task",
        "name": "Knowledge Base Task",
        "category": "test",
        "description": "Declares a knowledge base some plane must serve",
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


@pytest.fixture
def service(db_client, isolated_secret_manager) -> Any:
    """A runner with no rag_client — the core-stack shape."""
    impl = RunnerServiceImpl(db_client)
    assert impl.rag_client is None
    try:
        yield impl
    finally:
        impl.shutdown()


def _register(service: Any, context: Any, trial_id: str, search: dict[str, Any]) -> Any:
    return service.RegisterTrial(
        register_request(trial_spec_json(_task(search), trial_id=trial_id), trial_id=trial_id),
        context,
    )


def test_a_task_declaring_typesense_registers_against_the_stack_address(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion: a knowledge base with no address of its own.

    Nothing in this ``search`` block names a server. The plane declaration is the
    only thing that reaches the TypeSense gate, and the address comes from the
    stack — which is the whole point of moving it there.
    """
    _stack_offers_typesense(monkeypatch)
    registry = _install_registry(monkeypatch)

    response = _register(
        service,
        mock_grpc_context,
        "declared_typesense:0",
        {"plane": "typesense", "domain_name": DOMAIN, "documents_path": "docindex"},
    )

    assert response.success is True, response.error
    assert registry.connections == [(STACK_HOST, int(STACK_PORT), STACK_KEY)]


@pytest.mark.parametrize(
    ("row", "connection"),
    [
        ("still-carrying-the-address-it-has-not-dropped-yet", TASK_CONNECTION),
        ("carrying-nothing-but-the-declaration", {}),
    ],
)
def test_a_task_declaring_the_rag_plane_does_no_typesense_work(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    connection: dict[str, Any],
) -> None:
    """A rag corpus stays on its own plane in a run that offers TypeSense as well.

    Before the plane was declarable this shape could not arise — a rag task never
    carried a host — and the moment the address stops being per-task, every rag
    corpus in a TypeSense run looks exactly like a TypeSense one to a gate that
    reads the address. The declaration is what keeps them apart, including while
    an adapter still emits an address alongside it.

    The corpus rides in the artifacts, so this also holds the bundle-disagreement
    refusal to its own condition: this task's ``documents_path`` *is* declared,
    and a branch that fired here would say otherwise.
    """
    _stack_offers_typesense(monkeypatch)
    registry = _install_registry(monkeypatch)

    response = _register(
        service,
        mock_grpc_context,
        f"declared_rag_{row}:0",
        {
            "enabled": True,
            "plane": "rag_service",
            "domain_name": DOMAIN,
            "documents_path": "docindex",
            **connection,
        },
    )

    assert registry.connections == []
    assert response.success is False
    assert "RAG service not configured" in response.error
    assert "disagree" not in response.error
    assert "documents_path is unset" not in response.error


@pytest.mark.parametrize(
    ("row", "search", "basis"),
    [
        (
            "the-task-said-so",
            {"plane": "typesense", "domain_name": DOMAIN, "documents_path": "docindex"},
            "declared",
        ),
        (
            "the-runner-worked-it-out",
            {"domain_name": DOMAIN, "documents_path": "docindex", **TASK_CONNECTION},
            "derived_from_connection_details",
        ),
    ],
)
def test_a_refusal_names_the_plane_and_whether_the_task_declared_it(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    search: dict[str, Any],
    basis: str,
) -> None:
    """The transition is visible: an operator reads whether a plane was declared.

    A task carrying connection details and no plane is served, and the runner
    says it inferred that rather than being told — the two rows differ only in
    which of those the task did, so a message that reported one basis for both
    would fail one of them. Neither trial id names a basis, so the assertion
    cannot be satisfied by the prefix the message opens with.
    """
    _stack_offers_typesense(monkeypatch)
    _install_registry(monkeypatch, client=None)

    response = _register(service, mock_grpc_context, f"plane_basis_{row}:0", search)

    assert response.success is False
    assert "unreachable or refused the collection" in response.error
    assert f"search plane typesense ({basis})" in response.error


@pytest.mark.parametrize(
    ("row", "enabled", "expected", "unexpected"),
    [
        ("no-plane-serves-it", False, "search.plane", "RAG service not configured"),
        ("the-rag-plane-serves-it", True, "RAG service not configured", "search.plane"),
    ],
)
def test_a_corpus_with_no_plane_and_no_address_of_its_own(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    enabled: bool,
    expected: str,
    unexpected: str,
) -> None:
    """An adapter that drops its address before declaring a plane is refused, not skipped.

    The migration order is declare-then-drop; reversed, it produces a task the
    derivation cannot place — a knowledge base, a run whose stack offers
    TypeSense, and nothing saying the two belong together. Registering it would
    succeed with no search client behind ``search_policy``.

    ``enabled`` is the whole difference between that and a rag corpus from an
    adapter that never declared a plane either: that one *is* served, so a
    refusal covering both shapes would break it in every run that also configures
    TypeSense.
    """
    _stack_offers_typesense(monkeypatch)
    registry = _install_registry(monkeypatch)

    response = _register(
        service,
        mock_grpc_context,
        f"plane_less_{row}:0",
        {"enabled": enabled, "domain_name": DOMAIN, "documents_path": "docindex"},
    )

    assert response.success is False
    assert expected in response.error
    assert unexpected not in response.error
    assert registry.connections == []
