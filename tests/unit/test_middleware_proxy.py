"""Unit tests for the stdlib HTTP middleware proxy.

Drives the proxy handler against an in-process ``ThreadingHTTPServer`` acting
as the upstream so we can assert body/header rewrites without any network
call. Also covers the ``_deep_merge`` helper directly since it's the load-
bearing invariant behind every configured body injection.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from tolokaforge_adapter_terminal_bench.harness.middleware_proxy import (
    _deep_merge,
    _make_handler,
)

pytestmark = pytest.mark.unit


class _RecordingUpstream:
    """Captures the last request the proxy forwarded and returns a fixed response."""

    def __init__(self, response_status: int = 200, response_body: bytes = b'{"ok":true}'):
        self.response_status = response_status
        self.response_body = response_body
        self.last_path: str | None = None
        self.last_body: dict | None = None
        self.last_headers: dict[str, str] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def __enter__(self) -> _RecordingUpstream:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.last_path = self.path
                outer.last_body = json.loads(raw) if raw else None
                outer.last_headers = dict(self.headers.items())
                self.send_response(outer.response_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.response_body)))
                self.end_headers()
                self.wfile.write(outer.response_body)

        # Bind to an ephemeral port
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _serve_proxy(upstream_url: str, **middleware_kwargs) -> tuple[ThreadingHTTPServer, int]:
    """Boot the proxy on an ephemeral port bound to ``upstream_url``."""
    handler_cls = _make_handler(
        upstream=upstream_url,
        body_inject=middleware_kwargs.get("body_inject", {}),
        header_inject=middleware_kwargs.get("header_inject", {}),
        path_filter=middleware_kwargs.get("path_filter"),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _post_json(url: str, body: dict, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Simple stdlib POST — the tests don't need requests / httpx."""
    from urllib import error as urllib_error
    from urllib import request as urllib_request

    req = urllib_request.Request(url, method="POST", data=json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib_request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read()


class TestDeepMerge:
    """Overlay wins, dicts merge recursively, non-dicts replace."""

    def test_overlay_key_wins_on_conflict(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_missing_key_in_base_gains_the_overlay_value(self):
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_nested_dicts_merge_key_by_key(self):
        assert _deep_merge({"p": {"a": 1, "b": 2}}, {"p": {"b": 20, "c": 3}}) == {
            "p": {"a": 1, "b": 20, "c": 3}
        }

    def test_non_dict_overlay_replaces_a_dict_base_wholesale(self):
        """A caller sending ``provider: {"only":[...]}`` overlaid onto
        ``provider: "string"`` should keep the overlay's dict shape — the
        opposite direction ("string" replacing a dict) is symmetric."""
        assert _deep_merge({"provider": "openrouter"}, {"provider": {"only": ["x"]}}) == {
            "provider": {"only": ["x"]}
        }

    def test_non_dict_base_gains_the_overlay(self):
        assert _deep_merge(None, {"a": 1}) == {"a": 1}


class TestProxyBodyInjection:
    def test_configured_body_field_is_deep_merged_onto_request(self):
        with _RecordingUpstream() as up:
            server, port = _serve_proxy(
                up.base_url,
                body_inject={"provider": {"only": ["moonshotai"]}},
                path_filter="/chat/completions",
            )
            try:
                status, _ = _post_json(
                    f"http://127.0.0.1:{port}/chat/completions",
                    {"model": "k", "messages": [{"role": "user", "content": "hi"}]},
                )
                assert status == 200
                assert up.last_body is not None
                assert up.last_body["provider"] == {"only": ["moonshotai"]}
                assert up.last_body["model"] == "k"
            finally:
                server.shutdown()
                server.server_close()

    def test_body_injection_skipped_when_path_does_not_match_filter(self):
        with _RecordingUpstream() as up:
            server, port = _serve_proxy(
                up.base_url,
                body_inject={"provider": {"only": ["moonshotai"]}},
                path_filter="/chat/completions",
            )
            try:
                _post_json(f"http://127.0.0.1:{port}/embeddings", {"input": "hi"})
                assert up.last_body == {"input": "hi"}
            finally:
                server.shutdown()
                server.server_close()

    def test_empty_body_inject_is_passthrough_forwarder(self):
        with _RecordingUpstream() as up:
            server, port = _serve_proxy(up.base_url)
            try:
                _post_json(f"http://127.0.0.1:{port}/anything", {"a": 1})
                assert up.last_body == {"a": 1}
            finally:
                server.shutdown()
                server.server_close()


class TestProxyHeaderHandling:
    def test_configured_header_is_added_to_forwarded_request(self):
        with _RecordingUpstream() as up:
            server, port = _serve_proxy(up.base_url, header_inject={"X-Trace-Id": "tolokaforge-1"})
            try:
                _post_json(f"http://127.0.0.1:{port}/any", {"a": 1})
                assert up.last_headers.get("X-Trace-Id") == "tolokaforge-1"
            finally:
                server.shutdown()
                server.server_close()

    def test_content_length_matches_body_after_injection(self):
        """After the proxy deep-merges an injection, it recomputes
        ``Content-Length`` — otherwise urllib would either truncate the
        upstream body at the original length or fail with a mismatch."""
        with _RecordingUpstream() as up:
            server, port = _serve_proxy(
                up.base_url,
                body_inject={"provider": {"only": ["moonshotai"]}},
            )
            try:
                _post_json(f"http://127.0.0.1:{port}/any", {"model": "k"})
                assert up.last_body == {"model": "k", "provider": {"only": ["moonshotai"]}}
                # Content-Length must be the byte length of the injected body,
                # not the original one, otherwise upstream would see truncation.
                expected_len = len(json.dumps(up.last_body))
                assert int(up.last_headers.get("Content-Length", "-1")) == expected_len
            finally:
                server.shutdown()
                server.server_close()


class TestProxyErrorHandling:
    def test_unreachable_upstream_returns_502_bad_gateway(self):
        # Bind then immediately release a port so we know nothing listens there
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tmp:
            tmp.bind(("127.0.0.1", 0))
            dead_port = tmp.getsockname()[1]
        server, port = _serve_proxy(f"http://127.0.0.1:{dead_port}")
        try:
            status, _ = _post_json(f"http://127.0.0.1:{port}/x", {"a": 1})
            assert status == 502
        finally:
            server.shutdown()
            server.server_close()

    def test_upstream_http_error_status_and_body_are_passed_through(self):
        """A 4xx / 5xx from upstream is not the proxy's problem — the CLI
        sees the real status so it can retry or fail intelligently."""
        with _RecordingUpstream(
            response_status=401, response_body=b'{"error":"Unauthorized"}'
        ) as up:
            server, port = _serve_proxy(up.base_url)
            try:
                status, body = _post_json(f"http://127.0.0.1:{port}/x", {"a": 1})
                assert status == 401
                assert b"Unauthorized" in body
            finally:
                server.shutdown()
                server.server_close()
