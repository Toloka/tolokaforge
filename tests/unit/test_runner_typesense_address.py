"""The runner resolves one TypeSense address per registration and says where it came from.

Two sources can name an address: the stack the run built (``TYPESENSE_HOST`` /
``TYPESENSE_PORT`` on the runner container, with the key in the SecretManager)
and the task description's own ``search`` block. Both are live — a runner nobody
started for this run has no stack variables — so precedence is a fact worth
locking, and so is the runner *reporting* which source answered: an operator
reading a refusal against the wrong address cannot tell the two apart otherwise.

The tasks here carry connection details in every row, including the rows whose
address comes from the stack. That is what makes the precedence assertions bite:
a resolver that read the task config would produce a different address, not a
missing one.

``RunnerServiceImpl`` is real and drives its real ``RegisterTrial``; only the
mcp_core search registry — absent from this repo — is a stand-in, installed at
the import site the runner reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tests.utils.search_plane_harness import (
    DOMAIN,
    STACK_HOST,
    STACK_KEY,
    STACK_PORT,
    TASK_HOST,
    TASK_KEY,
    TASK_PORT,
    UsableClient,
    core_stack_runner,
    declare_stack_address,
    install_search_registry,
    register_kb_task,
)
from tolokaforge.runner.models import SearchConfig
from tolokaforge.runner.search_plane import (
    PartialTypeSenseAddressError,
    TypeSenseAddressBasis,
    resolve_typesense_binding,
)

pytestmark = pytest.mark.unit

STACK_ADDRESS = f"{STACK_HOST}:{STACK_PORT}"
TASK_ADDRESS = f"{TASK_HOST}:{TASK_PORT}"

# A knowledge-base task that names an address of its own in every scenario below.
KB_SEARCH = {
    "enabled": False,
    "host": TASK_HOST,
    "port": TASK_PORT,
    "api_key": TASK_KEY,
    "domain_name": DOMAIN,
    "documents_path": "docindex",
}


@pytest.fixture
def service(db_client, isolated_secret_manager) -> Any:
    with core_stack_runner(db_client) as impl:
        yield impl


@dataclass(frozen=True)
class _Source:
    """One row of the precedence table: what the stack declares, and what must follow."""

    stack: tuple[str | None, str | None, str | None]
    connection: tuple[str, int, str | None]
    basis: TypeSenseAddressBasis
    address: str
    address_of_the_other_source: str


# One row per source, over the same task — the task's own details are present either way.
_SOURCES = [
    pytest.param(
        _Source(
            stack=(STACK_HOST, STACK_PORT, STACK_KEY),
            connection=(STACK_HOST, int(STACK_PORT), STACK_KEY),
            basis=TypeSenseAddressBasis.STACK_ENV,
            address=STACK_ADDRESS,
            address_of_the_other_source=TASK_ADDRESS,
        ),
        id="the-stack-declared-one",
    ),
    pytest.param(
        _Source(
            stack=(None, None, None),
            connection=(TASK_HOST, TASK_PORT, TASK_KEY),
            basis=TypeSenseAddressBasis.TASK_SEARCH_CONFIG,
            address=TASK_ADDRESS,
            address_of_the_other_source=STACK_HOST,
        ),
        id="the-stack-declared-none",
    ),
]


@pytest.mark.parametrize("source", _SOURCES)
def test_the_resolved_address_is_the_one_the_client_is_initialised_against(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch, source: _Source
) -> None:
    """The stack outranks the task, and the whole connection travels — key included."""
    declare_stack_address(monkeypatch, *source.stack)
    registry = install_search_registry(monkeypatch, client=UsableClient())

    response = register_kb_task(service, mock_grpc_context, "resolved_address:0", KB_SEARCH)

    assert response.success is True, response.error
    assert registry.connections == [source.connection]


@pytest.mark.parametrize("source", _SOURCES)
def test_a_refusal_names_the_resolved_address_and_the_source_it_came_from(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch, source: _Source
) -> None:
    """The basis reaches the message the operator reads, not just the client.

    The address the other source names is asserted absent: a refusal that quotes
    a task's connection details while the client was built from the stack's sends
    the operator to debug a server the trial never touched.

    The trial id names neither source. The message opens with it, so an id built
    from the basis would satisfy the basis assertion on its own.
    """
    declare_stack_address(monkeypatch, *source.stack)
    install_search_registry(monkeypatch, client=None)

    response = register_kb_task(service, mock_grpc_context, "refused_plane:0", KB_SEARCH)

    assert response.success is False
    assert "unreachable or refused the collection" in response.error
    assert source.address in response.error
    assert source.basis.value in response.error
    assert source.address_of_the_other_source not in response.error


@pytest.mark.parametrize(
    ("row", "host", "port"),
    [
        ("port-missing", STACK_HOST, None),
        ("host-missing", None, STACK_PORT),
        ("port-unparseable", STACK_HOST, "auto"),
    ],
)
def test_a_half_declared_stack_address_refuses_the_trial_and_does_not_fall_back(
    service: Any,
    mock_grpc_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    host: str | None,
    port: str | None,
) -> None:
    """A half-configured stack is operator error, and the task's address is not a repair.

    Falling back would hand the trial exactly the address the stack was
    configured to replace — reachable from the host, and inside ``runner-net``
    the runner itself — so the plane is never touched.
    """
    declare_stack_address(monkeypatch, host, port, STACK_KEY)
    registry = install_search_registry(monkeypatch, client=UsableClient())

    response = register_kb_task(service, mock_grpc_context, f"partial_{row}:0", KB_SEARCH)

    assert response.success is False
    assert "TYPESENSE_HOST" in response.error
    assert "TYPESENSE_PORT" in response.error
    assert registry.connections == []


def test_an_address_with_no_api_key_reaches_client_initialisation(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``mode: remote`` stack registers no key; the server's own answer is the authority.

    The resolver does not second-guess a server that may not require one, so the
    binding carries ``api_key=None`` and lands on the loud client-init refusal
    rather than on a refusal the resolver invented.
    """
    declare_stack_address(monkeypatch, STACK_HOST, STACK_PORT, None)
    registry = install_search_registry(monkeypatch, client=None)

    response = register_kb_task(service, mock_grpc_context, "no_key:0", KB_SEARCH)

    assert registry.connections == [(STACK_HOST, int(STACK_PORT), None)]
    assert response.success is False
    assert "unreachable or refused the collection" in response.error


def test_neither_source_resolves_no_address_at_all(
    monkeypatch: pytest.MonkeyPatch, isolated_secret_manager: None
) -> None:
    """No stack variables and no task details is a run with no TypeSense plane.

    The gate reads this as "do no TypeSense work" — locked end to end by
    ``test_a_task_without_both_halves_does_no_typesense_work`` in
    ``tests/unit/test_runner_search_plane_refusal.py``.
    """
    declare_stack_address(monkeypatch, None, None, None)

    assert resolve_typesense_binding(SearchConfig(documents_path="docindex")) is None


def test_the_resolver_raises_rather_than_returning_a_half_declared_address(
    monkeypatch: pytest.MonkeyPatch, isolated_secret_manager: None
) -> None:
    """The contract callers depend on: a partial declaration is an error, not a ``None``."""
    declare_stack_address(monkeypatch, STACK_HOST, None, None)

    with pytest.raises(PartialTypeSenseAddressError, match="TYPESENSE_PORT"):
        resolve_typesense_binding(SearchConfig(host=TASK_HOST, port=TASK_PORT))
