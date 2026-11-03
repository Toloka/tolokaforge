"""A state request's own ``timeout`` reaches the HTTP request.

The runner bounds each grading read by its own deadline, which on a large world
outlasts the client's default. A read that dropped the deadline would be cut at
the default instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tolokaforge.runner.db_client import DBServiceClient

_STATE = {"data": {}, "version": 1, "full_hash": "f", "stable_hash": "s"}
_STABLE = {"data": {}, "version": 1, "stable_hash": "s", "filtered_fields": []}
_QUERY = {"results": [], "count": 0}
_MUTATE = {"status": "ok", "version": 2, "affected_rows": 0, "new_hash": "h"}


class _RecordingClient:
    """Answers every request and records the ``timeout`` it was sent with."""

    def __init__(self) -> None:
        self.timeouts: list[Any] = []

    def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> httpx.Response:
        self.timeouts.append(kwargs.get("timeout"))
        if method == "PATCH":
            body = _MUTATE
        elif url.endswith("/query"):
            body = _QUERY
        else:
            body = _STABLE if url.endswith("/stable") else _STATE
        return httpx.Response(200, json=body, request=httpx.Request(method, f"http://t{url}"))

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("GET", url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("POST", url, kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("PATCH", url, kwargs)


_READS = [
    pytest.param(lambda c, t: c.get_state("t:0", ["users"], timeout=t), id="get_state"),
    pytest.param(lambda c, t: c.get_stable_state("t:0", timeout=t), id="get_stable_state"),
    pytest.param(lambda c, t: c.query("t:0", "$.users", timeout=t), id="query"),
    pytest.param(lambda c, t: c.mutate("t:0", "users", [], timeout=t), id="mutate"),
]


def _client() -> tuple[DBServiceClient, _RecordingClient]:
    client = DBServiceClient("http://testserver", timeout=30.0)
    recording = _RecordingClient()
    client.set_test_client(recording)
    return client, recording


@pytest.mark.parametrize("read", _READS)
def test_a_reads_own_timeout_reaches_the_request(read) -> None:
    client, recording = _client()

    asyncio.run(read(client, 310.0))

    [timeout] = recording.timeouts
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == timeout.write == timeout.pool == 310.0
    # Reaching the service is no slower for a large state: connecting keeps the default.
    assert timeout.connect == 30.0


@pytest.mark.parametrize("read", _READS)
def test_a_read_without_one_keeps_the_clients_default(read) -> None:
    client, recording = _client()

    asyncio.run(read(client, None))

    assert recording.timeouts == [30.0]
