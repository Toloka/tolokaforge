"""Which plane serves a task's corpus, where this runner reaches TypeSense, and who said so.

The plane is a property of the task: a corpus is served by TypeSense or by
rag-service, and the task description says which. The address is a property of
the running stack: the container is created already knowing it, in
``TYPESENSE_HOST`` / ``TYPESENSE_PORT``. A task description may still carry
connection details of its own — adapters emitted them before the stack did, and
a runner nobody started for this run (``auto_start_services: false``, worker
mode) has no stack variables at all — so the two address sources coexist and the
stack wins.

Both are resolved once per registration and returned with the basis they came
from, never re-derived at a message site, so every log line and refusal names the
plane and the source the client was actually built from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from tolokaforge.runner.models import SearchConfig, SearchPlane
from tolokaforge.secrets import get_default

_STACK_HOST_VAR = "TYPESENSE_HOST"
_STACK_PORT_VAR = "TYPESENSE_PORT"
_API_KEY_SECRET = "TYPESENSE_API_KEY"

# The port the TypeSense image listens on, and what a task naming a host but no
# port meant.
_DEFAULT_PORT = 8108


class SearchPlaneBasis(str, Enum):
    """Whether the task named its plane or the runner worked it out."""

    DECLARED = "declared"
    """The task description's ``search.plane``."""

    DERIVED_FROM_CONNECTION_DETAILS = "derived_from_connection_details"
    """The task carried a TypeSense address instead of a plane."""


@dataclass(frozen=True)
class ResolvedSearchPlane:
    """The plane serving this task's corpus, and whether the task said so."""

    plane: SearchPlane
    basis: SearchPlaneBasis


def resolve_search_plane(search_config: SearchConfig) -> ResolvedSearchPlane | None:
    """Resolve which plane serves this task's corpus, or ``None`` if nothing says.

    Deriving from the task's own connection details serves the transition only,
    and derives ``TYPESENSE`` alone: rag indexing is gated on ``enabled`` and no
    run changes which rag work it does because a plane was worked out for it.
    """
    if search_config.plane is not None:
        return ResolvedSearchPlane(plane=search_config.plane, basis=SearchPlaneBasis.DECLARED)
    if search_config.host is not None:
        return ResolvedSearchPlane(
            plane=SearchPlane.TYPESENSE,
            basis=SearchPlaneBasis.DERIVED_FROM_CONNECTION_DETAILS,
        )
    return None


class TypeSenseAddressBasis(str, Enum):
    """Which source the resolved address came from."""

    STACK_ENV = "stack_env"
    """The stack the run built: ``TYPESENSE_HOST`` / ``TYPESENSE_PORT``."""

    TASK_SEARCH_CONFIG = "task_search_config"
    """The task description's own ``search`` block."""


class PartialTypeSenseAddressError(ValueError):
    """The stack declared one half of an address and not the other."""


@dataclass(frozen=True)
class ResolvedTypeSenseBinding:
    """The connection a registration will use, and the source that supplied it.

    ``api_key`` is ``None`` when the source named none: a ``mode: remote`` run
    injects an address and registers no key, and a server that does not require
    one is the server's own answer to give. Client initialisation asks it and
    reports what it says.
    """

    host: str
    port: int
    api_key: str | None
    basis: TypeSenseAddressBasis


def resolve_typesense_binding(search_config: SearchConfig) -> ResolvedTypeSenseBinding | None:
    """Resolve the run's TypeSense connection, or ``None`` if it has no plane.

    Raises:
        PartialTypeSenseAddressError: the stack set one of the two variables and
            not the other, or a port that is not a number. Falling back to the
            task's own details would hand the trial the very address the stack
            was configured to replace, so a half-configured stack stops the
            trial instead.
    """
    stack_host = os.environ.get(_STACK_HOST_VAR, "").strip()
    stack_port = os.environ.get(_STACK_PORT_VAR, "").strip()

    if stack_host and stack_port:
        return ResolvedTypeSenseBinding(
            host=stack_host,
            port=_parse_stack_port(stack_host, stack_port),
            api_key=get_default().get_secret(_API_KEY_SECRET),
            basis=TypeSenseAddressBasis.STACK_ENV,
        )
    if stack_host or stack_port:
        raise _half_declared("the stack declared half a TypeSense address", stack_host, stack_port)

    if search_config.host is None:
        return None
    return ResolvedTypeSenseBinding(
        host=search_config.host,
        port=search_config.port or _DEFAULT_PORT,
        api_key=search_config.api_key,
        basis=TypeSenseAddressBasis.TASK_SEARCH_CONFIG,
    )


def _parse_stack_port(host: str, raw_port: str) -> int:
    try:
        return int(raw_port)
    except ValueError as exc:
        raise _half_declared(
            "the stack declared a TypeSense port that is not a number", host, raw_port
        ) from exc


def _half_declared(reason: str, host: str, port: str) -> PartialTypeSenseAddressError:
    return PartialTypeSenseAddressError(
        f"{reason}: {_STACK_HOST_VAR}={host!r}, {_STACK_PORT_VAR}={port!r}. A stack sets both "
        f"or neither, and the runner does not fall back to the connection details the task "
        f"carries — those name the address the stack was configured to replace."
    )
