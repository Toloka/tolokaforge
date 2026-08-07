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

from typing import Any

import pytest

from tests.utils.search_plane_harness import (
    DOMAIN,
    STACK_HOST,
    STACK_KEY,
    STACK_PORT,
    TASK_CONNECTION,
    UsableClient,
    core_stack_runner,
    declare_stack_address,
    install_search_registry,
    register_kb_task,
)

pytestmark = pytest.mark.unit


def _stack_offers_typesense(monkeypatch: pytest.MonkeyPatch) -> None:
    declare_stack_address(monkeypatch, STACK_HOST, STACK_PORT, STACK_KEY)


@pytest.fixture
def service(db_client, isolated_secret_manager) -> Any:
    with core_stack_runner(db_client) as impl:
        yield impl


def test_a_task_declaring_typesense_registers_against_the_stack_address(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion: a knowledge base with no address of its own.

    Nothing in this ``search`` block names a server. The plane declaration is the
    only thing that reaches the TypeSense gate, and the address comes from the
    stack — which is the whole point of moving it there.
    """
    _stack_offers_typesense(monkeypatch)
    registry = install_search_registry(monkeypatch, client=UsableClient())

    response = register_kb_task(
        service,
        mock_grpc_context,
        "declared_typesense:0",
        {"plane": "typesense", "domain_name": DOMAIN, "documents_path": "docindex"},
    )

    assert response.success is True, response.error
    assert registry.connections == [(STACK_HOST, int(STACK_PORT), STACK_KEY)]


def test_a_declared_typesense_corpus_in_a_run_with_no_plane_registers_no_client(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run-level half governs: a run with no TypeSense plane does no TypeSense work.

    Neither the stack nor the task names an address here, so no address resolves
    and the gate's third condition holds the client back — the declared variant
    of the kb-task-in-a-typesense-disabled-run shape, which succeeds for the
    same reason. The declaration answers the *task-level* question (which plane
    serves this corpus); whether the run has that plane at all is the stack's
    answer, and a run that answers "no plane" registers the trial without a
    search client rather than refusing a task that is correctly declared.
    """
    registry = install_search_registry(monkeypatch, client=UsableClient())

    response = register_kb_task(
        service,
        mock_grpc_context,
        "declared_no_run_plane:0",
        {"plane": "typesense", "domain_name": DOMAIN, "documents_path": "docindex"},
    )

    assert response.success is True, response.error
    assert registry.connections == []


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
    registry = install_search_registry(monkeypatch, client=UsableClient())

    response = register_kb_task(
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


def test_a_rag_corpus_that_disabled_its_own_indexing_registers_no_client(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``enabled: false`` on a declared rag corpus switches its indexing off.

    The declaration keeps the corpus off the TypeSense plane, and ``enabled``
    is the rag plane's own gate — false means the task asked for no rag
    indexing, so registration has nothing to build and succeeds with no search
    client of either kind.
    """
    _stack_offers_typesense(monkeypatch)
    registry = install_search_registry(monkeypatch, client=UsableClient())

    response = register_kb_task(
        service,
        mock_grpc_context,
        "declared_rag_indexing_off:0",
        {
            "enabled": False,
            "plane": "rag_service",
            "domain_name": DOMAIN,
            "documents_path": "docindex",
        },
    )

    assert response.success is True, response.error
    assert registry.connections == []


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
    install_search_registry(monkeypatch, client=None)

    response = register_kb_task(service, mock_grpc_context, f"plane_basis_{row}:0", search)

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
    registry = install_search_registry(monkeypatch, client=UsableClient())

    response = register_kb_task(
        service,
        mock_grpc_context,
        f"plane_less_{row}:0",
        {"enabled": enabled, "domain_name": DOMAIN, "documents_path": "docindex"},
    )

    assert response.success is False
    assert expected in response.error
    assert unexpected not in response.error
    assert registry.connections == []
