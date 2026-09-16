"""Unit tests for the stdlib HTTP middleware proxy.

Drives the proxy handler against an in-process ``ThreadingHTTPServer`` acting
as the upstream so we can assert body/header rewrites without any network
call. Also covers the ``_deep_merge`` helper directly since it's the load-
bearing invariant behind every configured body injection, and the
``--usage-log`` token tap, whose invariant is that a request whose response
reports no usage records nothing at all.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from tolokaforge_coding_harnesses.middleware_proxy import (
    _build_parser,
    _deep_merge,
    _extract_token_counts,
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
        usage_log=middleware_kwargs.get("usage_log"),
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


_STREAMED_RESPONSE = b"""data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}

data: {"choices":[{"delta":{"content":" there"}}],"usage":null}

data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1200,\
"completion_tokens":34,"total_tokens":1234,"prompt_tokens_details":{"cached_tokens":1024},\
"completion_tokens_details":{"reasoning_tokens":8}}}

data: [DONE]

"""

_NON_STREAMED_RESPONSE = b"""{"id":"chatcmpl-1","choices":[{"message":{"role":"assistant",
"content":"done"},"finish_reason":"stop"}],"usage":{"prompt_tokens":900,"completion_tokens":21,
"total_tokens":921,"prompt_tokens_details":{"cached_tokens":768},
"completion_tokens_details":{"reasoning_tokens":5}}}"""


def _records(usage_log) -> list[dict]:
    """The NDJSON records written to *usage_log*, oldest first."""
    return [json.loads(line) for line in usage_log.read_text().splitlines() if line.strip()]


class TestExtractTokenCounts:
    """The usage shape the tap reads, straight from a response body.

    Every case here is a claim about what the record says: reported counts,
    reported zero, or nothing recorded at all.
    """

    def test_non_streamed_usage_block_is_read_field_by_field(self):
        assert _extract_token_counts(_NON_STREAMED_RESPONSE) == {
            "prompt_tokens": 900,
            "completion_tokens": 21,
            "total_tokens": 921,
            "cache_read_input_tokens": 768,
            "reasoning_tokens": 5,
        }

    def test_streamed_usage_comes_from_the_last_chunk_that_carries_it(self):
        """Every SSE chunk repeats ``usage`` as ``null`` until the final one."""
        assert _extract_token_counts(_STREAMED_RESPONSE) == {
            "prompt_tokens": 1200,
            "completion_tokens": 34,
            "total_tokens": 1234,
            "cache_read_input_tokens": 1024,
            "reasoning_tokens": 8,
        }

    def test_absent_detail_sub_objects_read_as_not_reported(self):
        body = b'{"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}'
        counts = _extract_token_counts(body)
        assert counts is not None
        assert counts["cache_read_input_tokens"] is None
        assert counts["reasoning_tokens"] is None

    def test_response_without_a_usage_block_reports_nothing(self):
        assert _extract_token_counts(b'{"choices":[{"message":{"content":"hi"}}]}') is None

    def test_usage_block_with_no_integer_counts_reports_nothing(self):
        """A usage block of nulls is as unreported as a missing one — a
        zero-filled record would claim the request spent nothing."""
        assert (
            _extract_token_counts(
                b'{"usage":{"prompt_tokens":null,"completion_tokens":"?","total_tokens":null}}'
            )
            is None
        )

    def test_boolean_counts_are_not_token_counts(self):
        assert _extract_token_counts(b'{"usage":{"prompt_tokens":true}}') is None

    def test_reported_zero_is_recorded_as_zero(self):
        counts = _extract_token_counts(
            b'{"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}'
        )
        assert counts is not None
        assert counts["prompt_tokens"] == 0

    def test_truncated_body_reports_nothing(self):
        assert _extract_token_counts(b'{"choices":[{"message":') is None

    def test_non_utf8_body_reports_nothing(self):
        assert _extract_token_counts(b"\xff\xfe\x00binary") is None


class TestUsageLogFlag:
    def test_flag_is_absent_by_default(self):
        args = _build_parser().parse_args(["--port", "1", "--upstream", "http://u"])
        assert args.usage_log is None

    def test_flag_carries_the_configured_path(self):
        args = _build_parser().parse_args(
            ["--port", "1", "--upstream", "http://u", "--usage-log", "/tmp/usage.ndjson"]
        )
        assert args.usage_log == "/tmp/usage.ndjson"


class TestProxyUsageLog:
    """The tap as the trial sees it: one NDJSON record per priced request.

    A harness trial runs its CLI as one tool call, so nothing but the wire
    knows what the trial spent when the CLI does not print its own totals.
    """

    def test_non_streamed_response_appends_one_record(self, tmp_path):
        usage_log = tmp_path / "usage.ndjson"
        with _RecordingUpstream(response_body=_NON_STREAMED_RESPONSE) as up:
            server, port = _serve_proxy(up.base_url, usage_log=str(usage_log))
            try:
                status, _ = _post_json(
                    f"http://127.0.0.1:{port}/chat/completions",
                    {"model": "moonshotai/kimi-k2.7-code", "messages": []},
                )
            finally:
                server.shutdown()
                server.server_close()
        assert status == 200
        records = _records(usage_log)
        assert len(records) == 1
        record = records[0]
        # The model comes from the request: this response body omits it.
        assert record["model"] == "moonshotai/kimi-k2.7-code"
        assert record["status"] == 200
        assert record["path"] == "/chat/completions"
        assert record["prompt_tokens"] == 900
        assert record["completion_tokens"] == 21
        assert record["total_tokens"] == 921
        assert record["cache_read_input_tokens"] == 768
        assert record["reasoning_tokens"] == 5
        assert record["timestamp"].endswith("+00:00")

    def test_streamed_response_records_the_final_chunks_usage(self, tmp_path):
        usage_log = tmp_path / "usage.ndjson"
        with _RecordingUpstream(response_body=_STREAMED_RESPONSE) as up:
            server, port = _serve_proxy(up.base_url, usage_log=str(usage_log))
            try:
                _post_json(
                    f"http://127.0.0.1:{port}/chat/completions",
                    {"model": "k", "messages": [], "stream": True},
                )
            finally:
                server.shutdown()
                server.server_close()
        records = _records(usage_log)
        assert len(records) == 1
        record = records[0]
        record.pop("timestamp")
        assert record == {
            "path": "/chat/completions",
            "status": 200,
            "model": "k",
            "prompt_tokens": 1200,
            "completion_tokens": 34,
            "total_tokens": 1234,
            "cache_read_input_tokens": 1024,
            "reasoning_tokens": 8,
        }

    def test_each_request_appends_its_own_record(self, tmp_path):
        usage_log = tmp_path / "usage.ndjson"
        with _RecordingUpstream(response_body=_NON_STREAMED_RESPONSE) as up:
            server, port = _serve_proxy(up.base_url, usage_log=str(usage_log))
            try:
                for _ in range(3):
                    _post_json(
                        f"http://127.0.0.1:{port}/chat/completions", {"model": "k", "messages": []}
                    )
            finally:
                server.shutdown()
                server.server_close()
        assert len(_records(usage_log)) == 3

    def test_missing_parent_directories_are_created(self, tmp_path):
        usage_log = tmp_path / "telemetry" / "run-1" / "usage.ndjson"
        with _RecordingUpstream(response_body=_NON_STREAMED_RESPONSE) as up:
            server, port = _serve_proxy(up.base_url, usage_log=str(usage_log))
            try:
                _post_json(f"http://127.0.0.1:{port}/chat/completions", {"model": "k"})
            finally:
                server.shutdown()
                server.server_close()
        assert len(_records(usage_log)) == 1

    def test_response_without_usage_records_nothing(self, tmp_path):
        """Not even an empty file: a zero-filled record would read as a
        request that spent nothing, which is a different claim from
        "this request's usage was never reported"."""
        usage_log = tmp_path / "usage.ndjson"
        with _RecordingUpstream(response_body=b'{"choices":[{"message":{"content":"hi"}}]}') as up:
            server, port = _serve_proxy(up.base_url, usage_log=str(usage_log))
            try:
                status, body = _post_json(
                    f"http://127.0.0.1:{port}/chat/completions", {"model": "k"}
                )
            finally:
                server.shutdown()
                server.server_close()
        assert status == 200
        assert body == b'{"choices":[{"message":{"content":"hi"}}]}'
        assert not usage_log.exists()

    def test_malformed_response_body_records_nothing_and_still_relays(self, tmp_path):
        usage_log = tmp_path / "usage.ndjson"
        malformed = b'{"choices":[{"message":'
        with _RecordingUpstream(response_body=malformed) as up:
            server, port = _serve_proxy(up.base_url, usage_log=str(usage_log))
            try:
                status, body = _post_json(
                    f"http://127.0.0.1:{port}/chat/completions", {"model": "k"}
                )
            finally:
                server.shutdown()
                server.server_close()
        assert (status, body) == (200, malformed)
        assert not usage_log.exists()

    def test_unwritable_usage_log_does_not_break_the_relay(self, tmp_path):
        """The tap is wrapped, the relay is not: a trial losing a usage
        record is an accounting gap, a trial losing its response is a lost
        trial."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        usage_log = blocker / "usage.ndjson"
        with _RecordingUpstream(response_body=_NON_STREAMED_RESPONSE) as up:
            server, port = _serve_proxy(up.base_url, usage_log=str(usage_log))
            try:
                status, body = _post_json(
                    f"http://127.0.0.1:{port}/chat/completions", {"model": "k"}
                )
            finally:
                server.shutdown()
                server.server_close()
        assert (status, body) == (200, _NON_STREAMED_RESPONSE)
        assert not usage_log.exists()

    def test_no_flag_writes_no_file_anywhere(self, tmp_path):
        with _RecordingUpstream(response_body=_NON_STREAMED_RESPONSE) as up:
            server, port = _serve_proxy(up.base_url)
            try:
                status, body = _post_json(
                    f"http://127.0.0.1:{port}/chat/completions", {"model": "k"}
                )
            finally:
                server.shutdown()
                server.server_close()
        assert (status, body) == (200, _NON_STREAMED_RESPONSE)
        assert list(tmp_path.iterdir()) == []

    def test_relayed_status_and_bytes_are_identical_with_the_tap_on(self, tmp_path):
        """The tap reads a buffer the proxy already holds; it must not change
        one byte of what the CLI receives."""
        request = {"model": "k", "messages": [{"role": "user", "content": "hi"}]}
        relayed = []
        for usage_log in (None, str(tmp_path / "usage.ndjson")):
            with _RecordingUpstream(response_body=_STREAMED_RESPONSE) as up:
                server, port = _serve_proxy(up.base_url, usage_log=usage_log)
                try:
                    relayed.append(_post_json(f"http://127.0.0.1:{port}/chat/completions", request))
                finally:
                    server.shutdown()
                    server.server_close()
        assert relayed[0] == relayed[1]
        assert relayed[1][1] == _STREAMED_RESPONSE
        assert len(_records(tmp_path / "usage.ndjson")) == 1

    def test_upstream_error_status_travels_with_the_record(self, tmp_path):
        """A 429 that still reports usage was still paid for; the status rides
        along so a consumer can tell it from a clean call."""
        usage_log = tmp_path / "usage.ndjson"
        with _RecordingUpstream(response_status=429, response_body=_NON_STREAMED_RESPONSE) as up:
            server, port = _serve_proxy(up.base_url, usage_log=str(usage_log))
            try:
                status, _ = _post_json(f"http://127.0.0.1:{port}/chat/completions", {"model": "k"})
            finally:
                server.shutdown()
                server.server_close()
        assert status == 429
        assert _records(usage_log)[0]["status"] == 429
