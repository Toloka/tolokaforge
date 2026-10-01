"""A local OTLP/HTTP receiver for the exporter tests.

It records every request it is sent and answers the way a test scripts it, so a test counts the
posts that reached the wire, whichever OpenTelemetry SDK built the request. A redirect answer points
at :data:`REDIRECT_TARGET`, which answers 200 and is recorded like any other request.
"""

from __future__ import annotations

import socket
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# read the body, then close the connection without an answer: the receiver may have the batch
DROP = "drop"
# read the body and answer only when the receiver closes: the exporter's timeout fires first
STALL = "stall"
REDIRECT_TARGET = "/redirected"


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: dict[str, str]  # names lower-cased
    body: bytes


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _take(self) -> None:
        receiver = self.server.receiver
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        headers = {name.lower(): value for name, value in self.headers.items()}
        receiver.requests.append(Request(self.command, self.path, headers, body))
        answer = 200 if self.path == REDIRECT_TARGET else receiver.answer
        if answer == DROP:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if answer == STALL:
            receiver.closing.wait(30)
            answer = 200
        self.send_response(int(answer))
        if 300 <= int(answer) < 400:
            self.send_header("Location", REDIRECT_TARGET)
        self.send_header("Content-Length", "0")
        # one request per connection: no client reuses a socket this receiver is about to close
        self.send_header("Connection", "close")
        self.end_headers()

    do_GET = _take
    do_POST = _take


class _Server(ThreadingHTTPServer):
    receiver: Receiver

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        # a stalled answer lands on a connection its client already gave up on; anything else is
        # the receiver failing, which must not pass for a lost answer
        if not isinstance(error, ConnectionError):
            self.receiver.errors.append(repr(error))


class Receiver:
    """``with Receiver() as receiver:`` serves on a free local port until the block ends."""

    def __init__(self) -> None:
        self.answer: int | str = 200
        self.requests: list[Request] = []
        self.errors: list[str] = []
        self.closing = threading.Event()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.receiver = self
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def url(self, path: str = "/v1/traces") -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{path}"

    @property
    def posts(self) -> list[Request]:
        return [request for request in self.requests if request.method == "POST"]

    def __enter__(self) -> Receiver:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, *exc_info: object) -> None:
        self.closing.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if exc_type is None and self.errors:
            raise AssertionError(f"the receiver failed while answering: {self.errors}")
