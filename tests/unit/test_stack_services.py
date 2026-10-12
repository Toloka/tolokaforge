"""The runner's declared stack-service surface (``tolokaforge.core.search.stack_services``).

A search backend reaches a stack service only through :class:`StackServices`: the
handle comes back typed by its service's Protocol, and a service the engine does not
declare, or one this runner does not reach, is refused with a message an operator can
act on. ``tests/canonical/test_stack_services_contract.py`` pins the surface itself.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.search.stack_services import (
    DECLARED_STACK_SERVICES,
    RAG_SERVICE,
    RAG_SERVICE_STACK_SERVICE,
    RagServiceHandle,
    StackService,
    StackServices,
    StackServiceUnavailableError,
    UndeclaredStackServiceError,
    declared_stack_service,
)
from tolokaforge.runner.rag_client import RAGServiceClient

pytestmark = pytest.mark.unit


def _client() -> RAGServiceClient:
    return RAGServiceClient("http://rag-service:8001")


def test_rag_service_is_the_declared_service_of_its_name() -> None:
    assert RAG_SERVICE.name == RAG_SERVICE_STACK_SERVICE == "rag_service"
    assert DECLARED_STACK_SERVICES == {"rag_service": RAG_SERVICE}
    assert declared_stack_service("rag_service") is RAG_SERVICE


def test_the_runners_client_is_a_rag_service_handle() -> None:
    assert isinstance(_client(), RagServiceHandle)


def test_get_returns_the_runners_handle_itself() -> None:
    client = _client()
    assert StackServices(rag_service=client).get(RAG_SERVICE) is client


def test_a_service_this_runner_does_not_reach_is_refused_saying_how_to_reach_it() -> None:
    with pytest.raises(StackServiceUnavailableError) as excinfo:
        StackServices().get(RAG_SERVICE)

    message = str(excinfo.value)
    assert message.startswith("stack service 'rag_service' is not reachable from this runner: ")
    assert "RAG_SERVICE_URL" in message
    assert "--profile full" in message
    assert excinfo.value.service is RAG_SERVICE


def test_an_undeclared_name_is_refused_naming_the_declared_ones() -> None:
    with pytest.raises(UndeclaredStackServiceError) as excinfo:
        declared_stack_service("elasticsearch")

    assert str(excinfo.value) == (
        "stack service 'elasticsearch' is not declared by this engine; the declared stack "
        "services are ['rag_service'] (stack-services API version 1)"
    )
    assert excinfo.value.name == "elasticsearch"


@pytest.mark.parametrize(
    "service",
    [
        StackService(name="elasticsearch", protocol=RagServiceHandle, reached_by="n/a"),
        StackService(name="rag_service", protocol=object, reached_by="n/a"),
    ],
    ids=["undeclared-name", "a-declared-name-with-another-protocol"],
)
def test_get_refuses_a_service_the_surface_does_not_declare(service: StackService) -> None:
    """Only the declared specs key the container, so a hand-made one cannot widen it."""
    with pytest.raises(UndeclaredStackServiceError, match=repr(service.name)):
        StackServices(rag_service=_client()).get(service)


def test_a_handle_that_is_not_the_declared_protocol_is_refused_at_construction() -> None:
    class _IndexOnly:
        async def index_documents(self, trial_id: str, domain_name: str, documents: list) -> None:
            return None

    with pytest.raises(TypeError) as excinfo:
        StackServices(rag_service=_IndexOnly())  # type: ignore[arg-type]

    assert "stack service 'rag_service': _IndexOnly does not satisfy RagServiceHandle" in str(
        excinfo.value
    )
