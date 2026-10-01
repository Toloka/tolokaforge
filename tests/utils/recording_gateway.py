"""A loopback HTTP server that plays a deployment's LLM gateway for wire tests.

It serves ``GET /v1/models`` from :attr:`RecordingGateway.catalog` (500 when
``None``, i.e. an unreadable catalog) and records every ``POST
/v1/chat/completions`` with its headers and body. Replies come from
:attr:`RecordingGateway.scripts`, a per-model queue, so a test can attribute
each request to a caller by the ``model`` its body names and decide what that
caller sees; a model without a queued reply gets :func:`text_reply` ``"done"``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = [
    "GatewayReply",
    "RecordedRequest",
    "RecordingGateway",
    "auth_error_reply",
    "server_error_reply",
    "serving_recording_gateway",
    "synthetic_error_reply",
    "text_reply",
    "tool_call_reply",
]


@dataclass(frozen=True)
class RecordedRequest:
    """One chat-completion request as it arrived; header names are lowercased."""

    headers: dict[str, str]
    body: dict[str, Any]

    @property
    def model(self) -> str:
        return str(self.body["model"])


@dataclass(frozen=True)
class GatewayReply:
    status: int
    payload: dict[str, Any]


def _completion(message: dict[str, Any], finish_reason: str) -> GatewayReply:
    return GatewayReply(
        200,
        {
            "id": "chatcmpl-loopback",
            "object": "chat.completion",
            "created": 0,
            "model": "loopback",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
        },
    )


def text_reply(content: str) -> GatewayReply:
    return _completion({"role": "assistant", "content": content}, "stop")


def tool_call_reply(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> GatewayReply:
    return _completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        "tool_calls",
    )


def synthetic_error_reply() -> GatewayReply:
    """A 200 whose ``finish_reason`` is an upstream-error marker: litellm keeps it as
    ``native_finish_reason`` and the engine's outer retry re-attempts the call."""
    return _completion({"role": "assistant", "content": ""}, "ERROR")


def server_error_reply() -> GatewayReply:
    return GatewayReply(500, {"error": "loopback upstream failure"})


def auth_error_reply() -> GatewayReply:
    return GatewayReply(
        401, {"error": {"message": "invalid loopback key", "code": "invalid_api_key"}}
    )


class RecordingGateway(ThreadingHTTPServer):
    """The server; a test sets :attr:`catalog` and :attr:`scripts` and reads :attr:`requests`."""

    catalog: list[str] | None
    requests: list[RecordedRequest]
    scripts: dict[str, list[GatewayReply]]

    def __init__(
        self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler]
    ) -> None:
        super().__init__(server_address, handler)
        self.reset()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"

    def reset(self) -> None:
        self.catalog = None
        self.requests = []
        self.scripts = {}


class _GatewayHandler(BaseHTTPRequestHandler):
    server: RecordingGateway

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        catalog = self.server.catalog
        if self.path != "/v1/models" or catalog is None:
            self._reply(GatewayReply(500, {"error": "catalog unavailable"}))
            return
        self._reply(GatewayReply(200, {"data": [{"id": route} for route in catalog]}))

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._reply(GatewayReply(404, {"error": f"unexpected path {self.path}"}))
            return
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        headers = {name.lower(): value for name, value in self.headers.items()}
        self.server.requests.append(RecordedRequest(headers=headers, body=body))
        queue = self.server.scripts.get(body["model"])
        self._reply(queue.pop(0) if queue else text_reply("done"))

    def _reply(self, reply: GatewayReply) -> None:
        data = json.dumps(reply.payload).encode()
        self.send_response(reply.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextmanager
def serving_recording_gateway() -> Iterator[RecordingGateway]:
    """Run a :class:`RecordingGateway` on a free loopback port for the block."""
    server = RecordingGateway(("127.0.0.1", 0), _GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
