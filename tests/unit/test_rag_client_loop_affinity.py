"""``RAGServiceClient`` binds its cached httpx client to the running event loop.

The runner shares one ``RAGServiceClient`` between two loops: the server's
startup/shutdown health probes run on the main server loop, while trial handlers
run on :class:`RunnerServiceImpl`'s dedicated loop thread. httpx binds its
connection-pool primitives to the loop a client is first driven on, so a client
cached on the probe loop and reused on the servicer loop raised
``<asyncio.locks.Event ...> is bound to a different event loop`` and aborted
every rag trial at ``register_trial``. The client must rebuild on whichever loop
is currently running.
"""

from __future__ import annotations

import asyncio

import pytest

from tolokaforge.runner.rag_client import RAGServiceClient

pytestmark = pytest.mark.unit


async def test_same_loop_reuses_the_cached_client() -> None:
    client = RAGServiceClient("http://rag-service:8001")
    try:
        first = await client._get_client()
        assert await client._get_client() is first, "same loop must reuse one client"
    finally:
        await client.close()


def test_a_loop_change_rebuilds_the_client() -> None:
    """Reusing the probe loop's client on the servicer loop is the exact
    `register_trial` failure; `_get_client` must hand back a fresh client bound
    to the running loop instead of the stale cross-loop one."""
    client = RAGServiceClient("http://rag-service:8001")
    probe_loop = asyncio.new_event_loop()
    servicer_loop = asyncio.new_event_loop()
    try:
        on_probe_loop = probe_loop.run_until_complete(client._get_client())
        on_servicer_loop = servicer_loop.run_until_complete(client._get_client())
        assert on_servicer_loop is not on_probe_loop, (
            "client must rebuild when the running loop changes — reusing the "
            "probe-loop client on the servicer loop is what raised "
            "'bound to a different event loop'"
        )
    finally:
        probe_loop.run_until_complete(on_probe_loop.aclose())
        servicer_loop.run_until_complete(on_servicer_loop.aclose())
        probe_loop.close()
        servicer_loop.close()


def test_close_drops_a_foreign_loop_client_without_raising() -> None:
    """At shutdown ``close`` runs on the server loop while the cached client may
    be bound to the servicer loop; it must drop it rather than await a
    cross-loop ``aclose`` (which would raise)."""
    client = RAGServiceClient("http://rag-service:8001")
    servicer_loop = asyncio.new_event_loop()
    shutdown_loop = asyncio.new_event_loop()
    try:
        bound = servicer_loop.run_until_complete(client._get_client())
        # close() on a different loop than the client is bound to must not raise.
        shutdown_loop.run_until_complete(client.close())
        assert client._client is None
    finally:
        servicer_loop.run_until_complete(bound.aclose())
        servicer_loop.close()
        shutdown_loop.close()
