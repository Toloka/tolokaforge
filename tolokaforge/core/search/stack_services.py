"""The runner's declared surface to the stack services a search backend uses (ADR-0054).

A stack service is a service of the run's stack that the runner reaches over the
network on a search backend's behalf; rag-service is the one declared. A backend
names the service it needs in :attr:`~tolokaforge.core.search.backend.SearchBackend.stack_service`,
the orchestrator starts the stack that runs it, and at ``RegisterTrial`` the runner
hands the backend its handle in
:attr:`~tolokaforge.core.search.backend.SearchBackendContext.stack_services`.

This module is that boundary's contract:

* :data:`DECLARED_STACK_SERVICES` — every service a backend may declare, by name,
  each a :class:`StackService` naming the Protocol its handle satisfies;
* the handle Protocols — :class:`RagServiceHandle` — which carry exactly the
  members a backend may use, and nothing else of the runner's client;
* :class:`StackServices` — the runner's handles for one trial, one field per
  declared service. :meth:`StackServices.get` returns a handle typed by its
  Protocol, and refuses a service this engine does not declare and one this runner
  does not reach.

**Versioning.** :data:`STACK_SERVICES_API_VERSION` numbers the surface: the declared
names and every member of every handle Protocol. Each change to it — a service
declared or withdrawn, a member added to, changed on or removed from a handle —
increments the version and is recorded in ADR-0054 § Stack services, and
``tests/canonical/test_stack_services_contract.py`` pins the surface to the version,
so a change without the bump fails CI. Adding a service or a member is
compatible: a backend that needs it compares the version. Changing or removing a
member breaks the backends that use it, so the ADR entry names what replaces it.

The module imports only the standard library. The rag-service request and response
models :class:`RagServiceHandle` names are the runner client's
(:mod:`tolokaforge.runner.rag_client`), imported for type checkers only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.runner.rag_client import Document, IndexResponse, SearchResponse

__all__ = [
    "DECLARED_STACK_SERVICES",
    "RAG_SERVICE",
    "RAG_SERVICE_STACK_SERVICE",
    "STACK_SERVICES_API_VERSION",
    "RagServiceHandle",
    "StackService",
    "StackServiceUnavailableError",
    "StackServices",
    "UndeclaredStackServiceError",
    "declared_stack_service",
]

STACK_SERVICES_API_VERSION = 1
"""The stack-service surface this engine declares; see the module docstring.

Version 1 declares ``rag_service`` with :class:`RagServiceHandle`: ``base_url``,
``timeout``, ``index_documents`` and ``search``.
"""

RAG_SERVICE_STACK_SERVICE = "rag_service"
"""The rag-service stack service, as a backend names it in ``stack_service``.

The orchestrator starts ``full_stack`` for a task whose backend declares it, and the
wire's ``search.enabled`` says whether a task's backend declares it. It names a stack
service, not a backend: the ``rag_service`` backend shares the spelling.
"""


@runtime_checkable
class RagServiceHandle(Protocol):
    """The runner's handle on rag-service, as a search backend may use it.

    The runner's :class:`~tolokaforge.runner.rag_client.RAGServiceClient` satisfies
    it; its other members (``delete_index``, ``health_check``, ``close``) are the
    runner's own and not part of the surface. Every method raises
    :class:`~tolokaforge.runner.rag_client.RAGServiceError` on a failed request.
    """

    @property
    def base_url(self) -> str:
        """The service's address, for a synchronous reader such as the judge's search."""
        ...

    @property
    def timeout(self) -> float:
        """The default per-request budget in seconds."""
        ...

    async def index_documents(
        self, trial_id: str, domain_name: str, documents: list[Document]
    ) -> IndexResponse:
        """Create or replace the trial's index over ``documents``."""
        ...

    async def search(
        self,
        trial_id: str,
        query: str,
        limit: int = 5,
        alpha: float = 0.5,
        timeout: float | None = None,
    ) -> SearchResponse:
        """Search the trial's index; ``timeout`` overrides :attr:`timeout` for this call."""
        ...


@dataclass(frozen=True)
class StackService[Handle]:
    """One declared stack service: the name a backend declares, and its handle's Protocol.

    ``protocol`` is the runtime-checkable Protocol a handle satisfies; the type
    parameter is the same Protocol, so :meth:`StackServices.get` returns a handle
    typed by it. ``reached_by`` says how a runner reaches the service: it completes
    the refusal an operator reads when one does not.
    """

    name: str
    protocol: type
    reached_by: str


RAG_SERVICE: StackService[RagServiceHandle] = StackService(
    name=RAG_SERVICE_STACK_SERVICE,
    protocol=RagServiceHandle,
    reached_by=(
        "the runner reads rag-service's address from RAG_SERVICE_URL, which the full "
        "stack sets (`tolokaforge docker up --profile full`, or a run whose task needs "
        "it), and none was set when this runner started"
    ),
)
"""rag-service, the hybrid BM25 + dense service (``tolokaforge/env/rag_service``)."""

DECLARED_STACK_SERVICES: Mapping[str, StackService[Any]] = MappingProxyType(
    {RAG_SERVICE.name: RAG_SERVICE}
)
"""Every stack service a backend may declare, keyed by the name it declares."""


class UndeclaredStackServiceError(LookupError):
    """A backend named a stack service this engine does not declare."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"stack service {name!r} is not declared by this engine; the declared stack "
            f"services are {sorted(DECLARED_STACK_SERVICES)} "
            f"(stack-services API version {STACK_SERVICES_API_VERSION})"
        )
        self.name = name


class StackServiceUnavailableError(LookupError):
    """A declared stack service this runner does not reach."""

    def __init__(self, service: StackService[Any]) -> None:
        super().__init__(
            f"stack service {service.name!r} is not reachable from this runner: "
            f"{service.reached_by}"
        )
        self.service = service


def declared_stack_service(name: str) -> StackService[Any]:
    """The declared stack service ``name`` names.

    Raises:
        UndeclaredStackServiceError: this engine declares no stack service by that name.
    """
    service = DECLARED_STACK_SERVICES.get(name)
    if service is None:
        raise UndeclaredStackServiceError(name)
    return service


@dataclass(frozen=True)
class StackServices:
    """The runner's handles on the stack services it reaches, one field per declared service.

    A field is ``None`` for a service this runner does not reach: its stack is not
    running, or the runner started without its address. A context built
    orchestrator-side to read what a backend declares holds none. A handle that does
    not satisfy its service's Protocol is refused at construction.
    """

    rag_service: RagServiceHandle | None = None

    def __post_init__(self) -> None:
        for service in DECLARED_STACK_SERVICES.values():
            handle = getattr(self, service.name)
            if handle is not None and not isinstance(handle, service.protocol):
                raise TypeError(
                    f"stack service {service.name!r}: {type(handle).__name__} does not "
                    f"satisfy {service.protocol.__name__}, the handle the stack-services "
                    f"API version {STACK_SERVICES_API_VERSION} declares for it"
                )

    def get[H](self, service: StackService[H]) -> H:
        """This runner's handle on ``service``, typed by its Protocol.

        Raises:
            UndeclaredStackServiceError: ``service`` is not one of
                :data:`DECLARED_STACK_SERVICES`.
            StackServiceUnavailableError: this runner does not reach ``service``.
        """
        if DECLARED_STACK_SERVICES.get(service.name) != service:
            raise UndeclaredStackServiceError(service.name)
        handle = getattr(self, service.name)
        if handle is None:
            raise StackServiceUnavailableError(service)
        return cast(H, handle)
