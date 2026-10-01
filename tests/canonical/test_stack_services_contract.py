"""Pin the runner's stack-service surface to its version (ADR-0052 § Stack services).

``tolokaforge.core.search.stack_services`` is the declared boundary between the
runner and the stack services a search backend uses. Its evolution rule: every change
to the declared names or to a member of a handle Protocol increments
``STACK_SERVICES_API_VERSION`` and is recorded in the ADR. This module holds the rule:

* the surface as built is compared with the surface pinned for the current version,
  so a member added, changed or removed without the bump fails here;
* :class:`StackServices` has one field per declared service and nothing else;
* the runner's own client conforms to the declared handle by signature, not only by
  member presence, so the runner cannot hand backends something the contract does
  not describe.

When the surface changes on purpose: bump the version, add the entry to the ADR's
version table, and replace ``_PINNED`` below with the new surface.
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from typing import Any

import pytest

from tolokaforge.core.search.stack_services import (
    DECLARED_STACK_SERVICES,
    STACK_SERVICES_API_VERSION,
    StackServices,
)
from tolokaforge.runner.rag_client import RAGServiceClient

pytestmark = pytest.mark.canonical

_PINNED: dict[str, Any] = {
    "version": 1,
    "services": {
        "rag_service": {
            "protocol": "RagServiceHandle",
            "members": {
                "base_url": "property -> str",
                "timeout": "property -> float",
                "index_documents": (
                    "async (self, trial_id: 'str', domain_name: 'str', "
                    "documents: 'list[Document]') -> 'IndexResponse'"
                ),
                "search": (
                    "async (self, trial_id: 'str', query: 'str', limit: 'int' = 5, "
                    "alpha: 'float' = 0.5, timeout: 'float | None' = None) -> 'SearchResponse'"
                ),
            },
        }
    },
}

_RUNNER_HANDLES: dict[str, Any] = {"rag_service": RAGServiceClient("http://rag-service:8001")}
"""The object the runner hands a backend for each declared service."""


def _member(protocol: type, name: str) -> str:
    value = inspect.getattr_static(protocol, name)
    if isinstance(value, property):
        return f"property -> {inspect.signature(value.fget).return_annotation}"
    prefix = "async " if inspect.iscoroutinefunction(value) else ""
    return f"{prefix}{inspect.signature(value)}"


def _surface() -> dict[str, Any]:
    return {
        "version": STACK_SERVICES_API_VERSION,
        "services": {
            name: {
                "protocol": service.protocol.__name__,
                "members": {
                    member: _member(service.protocol, member)
                    for member in sorted(service.protocol.__protocol_attrs__)
                },
            }
            for name, service in DECLARED_STACK_SERVICES.items()
        },
    }


def test_the_surface_is_the_one_pinned_for_its_version() -> None:
    assert _surface() == _PINNED, (
        "the stack-service surface changed: bump STACK_SERVICES_API_VERSION, record the "
        "change in ADR-0052 § Stack services, and pin the new surface here"
    )


def test_the_container_has_one_field_per_declared_service() -> None:
    assert [field.name for field in fields(StackServices)] == list(DECLARED_STACK_SERVICES)


def test_every_declared_service_has_the_runners_handle_listed_here() -> None:
    assert set(_RUNNER_HANDLES) == set(DECLARED_STACK_SERVICES)


@pytest.mark.parametrize("name", sorted(DECLARED_STACK_SERVICES))
def test_the_runners_handle_conforms_to_the_declared_protocol_by_signature(name: str) -> None:
    service = DECLARED_STACK_SERVICES[name]
    handle = _RUNNER_HANDLES[name]
    assert isinstance(handle, service.protocol)
    for member in sorted(service.protocol.__protocol_attrs__):
        declared = inspect.getattr_static(service.protocol, member)
        if isinstance(declared, property):
            getattr(handle, member)
            continue
        implemented = getattr(type(handle), member)
        kinds = (inspect.iscoroutinefunction(implemented), inspect.iscoroutinefunction(declared))
        assert kinds[0] == kinds[1], f"{name}.{member}: sync/async differs from the handle"
        assert _shape(implemented) == _shape(declared), (
            f"{name}.{member}: the runner's {type(handle).__name__} takes "
            f"{_shape(implemented)}, the declared handle {_shape(declared)}"
        )


def _shape(function: Any) -> list[tuple[str, Any, Any]]:
    """Parameter names, kinds and defaults: what a caller of the member relies on."""
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(function).parameters.values()
    ]
