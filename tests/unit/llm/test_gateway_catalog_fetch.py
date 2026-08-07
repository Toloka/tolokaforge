"""The fetch half of gateway routing, against a real local endpoint.

The resolver and the client wiring are pinned elsewhere; both patch this function
out, so without this file the HTTP handling, the auth and attribution headers, the
empty-answer mapping and the cache semantics are unpinned. A caching bug shipped
through exactly that gap once.

A local ``http.server`` rather than a mocked ``urlopen``: the point is to exercise
the request this code really sends.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tolokaforge.core.llm import gateway_route
from tolokaforge.core.llm.gateway_route import clear_catalog_cache, fetch_gateway_catalog
from tolokaforge.core.llm.proxy import ProxyConfig

pytestmark = pytest.mark.unit

SEEN_HEADERS: list[dict[str, str]] = []
RESPONSES: list[tuple[int, str]] = []
HITS: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        HITS.append(self.path)
        SEEN_HEADERS.append({k.lower(): v for k, v in self.headers.items()})
        status, body = RESPONSES.pop(0) if RESPONSES else (200, json.dumps({"data": []}))
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def _server() -> Iterator[str]:
    """One server for the file: shutdown() costs a poll interval each time."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()


@pytest.fixture
def gateway(_server: str) -> Iterator[str]:
    SEEN_HEADERS.clear()
    RESPONSES.clear()
    HITS.clear()
    clear_catalog_cache()
    yield _server
    clear_catalog_cache()


def _config(base: str, **kw: object) -> ProxyConfig:
    return ProxyConfig(base_url=base, **kw)  # type: ignore[arg-type]


class TestTheRequest:
    def test_it_asks_the_models_endpoint_and_returns_the_ids(self, gateway: str) -> None:
        RESPONSES.append(
            (200, json.dumps({"data": [{"id": "azure_ai/m"}, {"id": "openrouter/n"}]}))
        )
        assert fetch_gateway_catalog(_config(gateway)) == frozenset({"azure_ai/m", "openrouter/n"})
        assert HITS == ["/v1/models"]

    def test_the_gateway_key_and_headers_ride_along(self, gateway: str) -> None:
        """The catalog must be fetched through the same admission the calls use, or a
        header-admission gateway answers 403 and every model reads as absent."""
        RESPONSES.append((200, json.dumps({"data": [{"id": "m"}]})))
        fetch_gateway_catalog(_config(gateway, api_key="sk-gw", headers={"X-Admission": "tok"}))
        assert SEEN_HEADERS[0]["authorization"] == "Bearer sk-gw"
        assert SEEN_HEADERS[0]["x-admission"] == "tok"

    def test_entries_without_an_id_are_skipped(self, gateway: str) -> None:
        RESPONSES.append((200, json.dumps({"data": [{"id": "m"}, {}, {"id": ""}, "junk"]})))
        assert fetch_gateway_catalog(_config(gateway)) == frozenset({"m"})


class TestUnreadableAnswers:
    @pytest.mark.parametrize(
        ("status", "body", "case"),
        [
            (200, json.dumps({"data": []}), "empty list"),
            (200, json.dumps({"data": {}}), "data is not a list"),
            (200, "not json", "malformed body"),
            (403, json.dumps({"error": "forbidden"}), "rejected"),
            (500, "boom", "server error"),
        ],
    )
    def test_every_unreadable_shape_maps_to_none(
        self, gateway: str, status: int, body: str, case: str
    ) -> None:
        RESPONSES.append((status, body))
        assert fetch_gateway_catalog(_config(gateway)) is None, case

    def test_an_unreachable_host_maps_to_none_rather_than_raising(self) -> None:
        clear_catalog_cache()
        assert fetch_gateway_catalog(_config("http://127.0.0.1:1/v1"), timeout=2) is None


class TestCacheSemantics:
    def test_a_successful_read_is_cached(self, gateway: str) -> None:
        RESPONSES.append((200, json.dumps({"data": [{"id": "m"}]})))
        assert fetch_gateway_catalog(_config(gateway)) == frozenset({"m"})
        assert fetch_gateway_catalog(_config(gateway)) == frozenset({"m"})
        assert len(HITS) == 1

    def test_an_empty_answer_is_not_cached(self, gateway: str) -> None:
        """The bug this file was written for: an empty answer is called transient in
        the design, so caching it pinned a whole process on the degraded path."""
        RESPONSES.append((200, json.dumps({"data": []})))
        RESPONSES.append((200, json.dumps({"data": [{"id": "m"}]})))
        assert fetch_gateway_catalog(_config(gateway)) is None
        assert fetch_gateway_catalog(_config(gateway)) == frozenset({"m"})

    def test_a_failure_is_not_cached(self, gateway: str) -> None:
        RESPONSES.append((500, "boom"))
        RESPONSES.append((200, json.dumps({"data": [{"id": "m"}]})))
        assert fetch_gateway_catalog(_config(gateway)) is None
        assert fetch_gateway_catalog(_config(gateway)) == frozenset({"m"})

    def test_repeated_failure_stops_retrying(self, gateway: str) -> None:
        """A gateway that HANGS would otherwise cost the timeout on every client
        construction for the whole run."""
        for _ in range(gateway_route.MAX_CATALOG_ATTEMPTS):
            RESPONSES.append((500, "boom"))
            assert fetch_gateway_catalog(_config(gateway)) is None
        before = len(HITS)
        assert fetch_gateway_catalog(_config(gateway)) is None
        assert len(HITS) == before, "gave up, so no further request"

    def test_a_success_resets_the_failure_count(self, gateway: str) -> None:
        RESPONSES.append((500, "boom"))
        assert fetch_gateway_catalog(_config(gateway)) is None
        RESPONSES.append((200, json.dumps({"data": [{"id": "m"}]})))
        assert fetch_gateway_catalog(_config(gateway)) == frozenset({"m"})
        assert gateway_route._FAILURES.get(_config(gateway).base_url, 0) == 0
