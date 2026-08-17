#!/usr/bin/env python3
"""Tiny stdlib HTTP proxy that forwards to *upstream* injecting body / header fields.

Ships with the terminal-bench adapter and lands inside every trial image whose
harness declares :attr:`HarnessSpec.request_middleware`. The CLI's provider
base-URL env var is rewritten to ``http://localhost:<port>`` so its requests
reach this proxy first; the proxy deep-merges the configured JSON body
overrides, adds the configured headers, and forwards.

The single use case today: OpenRouter provider-preference injection —
``moonshotai/kimi-k2.7-code`` on OpenRouter routes to 14 possible providers,
mostly INT4/FP4 third-parties that return empty completions on tool-call
continuation. Forcing Moonshot AI first-party routing via
``{"provider": {"only": ["moonshotai"], "allow_fallbacks": false}}`` in every
request body fixes it. Any provider-preference / header-injection / body-repair
concern that a vendor CLI does not surface its own knob for lands here.

Runs on stdlib alone (``http.server`` + ``urllib``) so no ``pip install`` step
gets bolted onto every image.

Invocation::

    python3 middleware_proxy.py \\
        --port 8899 \\
        --upstream https://openrouter.ai/api/v1 \\
        --body-inject '{"provider":{"only":["moonshotai"]}}' \\
        --daemon

``--daemon`` double-forks and returns once the listener is bound so a shell
preamble can wait for it deterministically before starting the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)


def _deep_merge(base: Any, overlay: Any) -> Any:
    """Recursively merge *overlay* into *base*.

    Dict values merge key-by-key; other types replace. Overlay wins on
    conflicts. A missing base key gains the overlay value verbatim.
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = _deep_merge(merged.get(key), value) if key in merged else value
        return merged
    return overlay


def _make_handler(
    upstream: str,
    body_inject: dict[str, Any],
    header_inject: dict[str, str],
    path_filter: str | None,
) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class closed over the middleware config.

    A class rather than instance because :class:`ThreadingHTTPServer` wants a
    handler *type* to instantiate per request."""

    class Handler(BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler's default is a chatty timestamped stderr line
        # per request — noise the trial's own logs already carry.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _proxy(self, method: str) -> None:
            request_body = b""
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                request_body = self.rfile.read(length)

            # Body-inject only on the configured path (defaults: every JSON
            # POST). A path filter avoids mangling health probes / GETs / OPTIONS.
            should_inject = (
                method == "POST"
                and body_inject
                and (path_filter is None or self.path.startswith(path_filter))
                and request_body
            )
            if should_inject:
                try:
                    parsed = json.loads(request_body)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    parsed = _deep_merge(parsed, body_inject)
                    request_body = json.dumps(parsed).encode("utf-8")

            target = upstream.rstrip("/") + self.path
            req = urllib_request.Request(target, data=request_body, method=method)
            for name, value in self.headers.items():
                if name.lower() in _HOP_BY_HOP:
                    continue
                req.add_header(name, value)
            for name, value in header_inject.items():
                req.add_header(name, value)
            # Only set Content-Length ourselves so a re-encoded body's length
            # matches; the loop above skipped both Content-Length and Host.
            if request_body:
                req.add_header("Content-Length", str(len(request_body)))

            try:
                response = urllib_request.urlopen(req, timeout=600)
                status = response.status
                headers = list(response.getheaders())
                body = response.read()
            except urllib_error.HTTPError as exc:
                status = exc.code
                headers = list(exc.headers.items()) if exc.headers else []
                body = exc.read()
            except urllib_error.URLError as exc:
                self.send_error(502, f"upstream unreachable: {exc}")
                return

            self.send_response(status)
            for name, value in headers:
                if name.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # POST / GET / PUT / DELETE / PATCH all forward through the same proxy.
        def do_POST(self) -> None:  # noqa: N802
            self._proxy("POST")

        def do_GET(self) -> None:  # noqa: N802
            self._proxy("GET")

        def do_PUT(self) -> None:  # noqa: N802
            self._proxy("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._proxy("DELETE")

        def do_PATCH(self) -> None:  # noqa: N802
            self._proxy("PATCH")

    return Handler


def _wait_port(port: int, timeout_s: float = 5.0) -> None:
    """Block until *port* accepts a TCP connection or *timeout_s* elapses."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.05)
    raise TimeoutError(f"middleware_proxy: port {port} did not open in {timeout_s}s")


def _daemonize() -> None:
    """Detach from the current process group so the caller's shell exits cleanly.

    Double-fork so the daemon has no controlling terminal — a single fork
    leaves it re-attached under some init systems. stdout / stderr are
    redirected to ``/dev/null`` because the trial's log capture reads the
    CLI's streams, not the proxy's.
    """
    if os.fork() > 0:
        os._exit(0)  # noqa: SLF001
    os.setsid()
    if os.fork() > 0:
        os._exit(0)  # noqa: SLF001
    devnull = os.open(os.devnull, os.O_RDWR)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        os.dup2(devnull, stream.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tolokaforge middleware proxy")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", required=True, help="Upstream base URL to forward to.")
    parser.add_argument(
        "--body-inject",
        default="{}",
        help="JSON object deep-merged into every POST request body on --path-filter.",
    )
    parser.add_argument(
        "--header-inject",
        default="{}",
        help="JSON object; each key/value added as a request header.",
    )
    parser.add_argument(
        "--path-filter",
        default=None,
        help="Only inject on paths starting with this prefix (default: every POST).",
    )
    parser.add_argument("--daemon", action="store_true", help="Fork and return when port is open.")
    args = parser.parse_args(argv)

    try:
        body_inject = json.loads(args.body_inject)
        header_inject = json.loads(args.header_inject)
    except json.JSONDecodeError as exc:
        print(
            f"middleware_proxy: --body-inject/--header-inject must be JSON: {exc}", file=sys.stderr
        )
        return 2
    if not isinstance(body_inject, dict) or not isinstance(header_inject, dict):
        print(
            "middleware_proxy: --body-inject and --header-inject must be JSON objects",
            file=sys.stderr,
        )
        return 2

    handler_cls = _make_handler(args.upstream, body_inject, header_inject, args.path_filter)

    if args.daemon:
        # Parent process: fork a child that runs the server, wait for its port,
        # then return so the caller can invoke the CLI knowing the proxy is
        # listening.
        pid = os.fork()
        if pid > 0:
            _wait_port(args.port)
            return 0
        # Child: detach then serve.
        _daemonize()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_cls)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
