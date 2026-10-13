"""How a bare host list is read against a request url.

A host list (``http_request``'s ``allowed_hosts``, an app world's ``hosts``) names
each entry as ``host`` or ``host:port``: a bare ``host`` matches the host on every
port, ``host:port`` matches it on that explicit port alone. The tool reads it so
at request time (:func:`host_listed`) and task validation reads it the same way
when it asks whether an actor's credential could ever be presented
(:func:`unreachable_hosts`), so a declaration one accepts the other honours.

A leaf on purpose: ``core.models`` and ``tools.builtin`` both import it, and
neither may pull the other's package in.
"""

from __future__ import annotations

from collections.abc import Collection
from urllib.parse import urlparse


def host_listed(url: str, hosts: Collection[str]) -> bool:
    """Whether ``url``'s host, bare or with its explicit port, is one of ``hosts``."""
    parsed = urlparse(url)
    if parsed.hostname is None:
        return False
    if parsed.hostname in hosts:
        return True
    return parsed.port is not None and f"{parsed.hostname}:{parsed.port}" in hosts


def unreachable_hosts(hosts: Collection[str], allowed: Collection[str]) -> list[str]:
    """The entries of ``hosts`` no request ``allowed`` admits could carry a credential to.

    ``world`` is reachable through an allowed ``world:8080`` (a request to that
    port is admitted and credentialed alike) and ``world:8080`` through an
    allowed ``world``; ``world:8080`` is not reachable through ``world:9090``.
    """
    return sorted(host for host in hosts if not _reachable(host, allowed))


def _reachable(host: str, allowed: Collection[str]) -> bool:
    name, _, port = host.partition(":")
    if not port:
        return any(entry.partition(":")[0] == name for entry in allowed)
    return name in allowed or host in allowed
