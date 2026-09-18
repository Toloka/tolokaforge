#!/usr/bin/env python3
"""Tiny stdlib HTTP proxy that forwards to *upstream* injecting body / header fields.

Lands inside every trial image whose harness declares
:attr:`HarnessSpec.request_middleware`. The CLI's provider
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

``--usage-log`` additionally taps the token usage of every response that
reports it into an NDJSON file. A harness trial runs its CLI as one tool call,
so the engine issues no LLM request and sees no token counts; a CLI that does
not print its own totals leaves the wire as the only place they exist, and the
proxy is already on it.

Runs on stdlib alone (``http.server`` + ``urllib``) so no ``pip install`` step
gets bolted onto every image.

Invocation::

    python3 middleware_proxy.py \\
        --port 8899 \\
        --upstream https://openrouter.ai/api/v1 \\
        --body-inject '{"provider":{"only":["moonshotai"]}}' \\
        --usage-log /opt/tolokaforge/usage.ndjson \\
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
from datetime import datetime, timezone
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


def _as_token_count(value: Any) -> int | None:
    """*value* when it is a plain integer token count, else ``None``.

    ``bool`` is an ``int`` subclass and is never a token count, so it is
    rejected rather than counted as 0 / 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _detail_token_count(details: Any, key: str) -> int | None:
    """*key* inside a ``*_tokens_details`` sub-object, when it holds a count."""
    if not isinstance(details, dict):
        return None
    return _as_token_count(details.get(key))


def _token_counts(usage: Any) -> dict[str, int | None] | None:
    """Token counts from an OpenAI Chat Completions ``usage`` block.

    ``None`` when *usage* is missing or carries no integer count anywhere:
    a zero-filled record would claim the request spent nothing, which is a
    different and false claim from "this request's usage was not reported".
    """
    if not isinstance(usage, dict):
        return None
    counts: dict[str, int | None] = {
        "prompt_tokens": _as_token_count(usage.get("prompt_tokens")),
        "completion_tokens": _as_token_count(usage.get("completion_tokens")),
        "total_tokens": _as_token_count(usage.get("total_tokens")),
    }
    if all(count is None for count in counts.values()):
        return None
    counts["cache_read_input_tokens"] = _detail_token_count(
        usage.get("prompt_tokens_details"), "cached_tokens"
    )
    counts["reasoning_tokens"] = _detail_token_count(
        usage.get("completion_tokens_details"), "reasoning_tokens"
    )
    return counts


def _response_payloads(body: bytes) -> list[dict[str, Any]]:
    """The JSON objects *body* carries, in wire order.

    A non-streamed response is a single object. A streamed one is a run of
    ``data: {…}`` SSE chunks whose terminal ``data: [DONE]`` and blank
    separators carry no JSON. Anything unparseable contributes nothing.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        return [payload]
    # ``streamGenerateContent`` without ``alt=sse`` answers with a JSON array
    # of the same chunks, which is neither a single object nor an SSE stream.
    # Unhandled, the tap writes nothing and the trial is silently unmetered.
    if isinstance(payload, list):
        return [chunk for chunk in payload if isinstance(chunk, dict)]

    payloads: list[dict[str, Any]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith(b"data:"):
            continue
        try:
            chunk = json.loads(stripped[len(b"data:") :])
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(chunk, dict):
            payloads.append(chunk)
    return payloads


def _extract_token_counts(body: bytes) -> dict[str, int | None] | None:
    """Token counts *body* reports, or ``None`` when it reports none.

    A streamed response repeats its usage block as ``null`` on every chunk
    but the final one, so the last payload carrying real counts wins.

    Two wire shapes, because the proxy meters whatever the harness routes
    through it: OpenAI Chat Completions reports ``usage``, and Google's
    ``generateContent`` reports ``usageMetadata``. A response carries one or
    the other, never both.
    """
    for payload in reversed(_response_payloads(body)):
        counts = _token_counts(payload.get("usage"))
        if counts is not None:
            return counts
        counts = _gemini_token_counts(payload.get("usageMetadata"))
        if counts is not None:
            return counts
    return None


def _gemini_token_counts(usage: Any) -> dict[str, int | None] | None:
    """Token counts from a Google ``generateContent`` ``usageMetadata`` block.

    Normalised onto the same basis the OpenAI shape reports, so a record's
    meaning does not depend on which CLI produced it:

    * ``promptTokenCount`` is already the whole prompt, cached part included,
      and ``cachedContentTokenCount`` names the cached portion of it — the
      same relationship OpenAI's ``prompt_tokens`` has to its
      ``cached_tokens`` detail.
    * ``candidatesTokenCount`` **excludes** thinking tokens, where OpenAI's
      ``completion_tokens`` includes them. They are added back here, so a
      consumer summing completion tokens is not silently under-counting the
      part of the answer the model charged for but did not show.

    ``None`` when *usage* is missing or carries no integer count anywhere: a
    zero-filled record would claim the request spent nothing, which is a
    different and false claim from "this request's usage was not reported".
    """
    if not isinstance(usage, dict):
        return None
    prompt = _as_token_count(usage.get("promptTokenCount"))
    candidates = _as_token_count(usage.get("candidatesTokenCount"))
    total = _as_token_count(usage.get("totalTokenCount"))
    reasoning = _as_token_count(usage.get("thoughtsTokenCount"))
    cached = _as_token_count(usage.get("cachedContentTokenCount"))
    if all(count is None for count in (prompt, candidates, total, reasoning, cached)):
        return None
    # Thinking tokens are part of what the model charged for. A block that
    # reports them without a candidates count would otherwise leave them out
    # of the completion total entirely, and the caller prices completion —
    # not reasoning, which every dialect already counts inside it.
    completion = candidates
    if reasoning is not None:
        completion = reasoning if completion is None else completion + reasoning
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_read_input_tokens": cached,
        "reasoning_tokens": reasoning,
    }


def _request_model(request_body: bytes) -> str | None:
    """The ``model`` the request asked for. The response may omit it."""
    try:
        parsed = json.loads(request_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    model = parsed.get("model")
    return model if isinstance(model, str) else None


def _append_usage_record(usage_log: str, record: dict[str, Any]) -> None:
    """Append one NDJSON *record*, creating parent directories as needed.

    Reopened per record so a run killed mid-trial keeps every record written
    before the kill. One append-mode write per record also keeps the
    concurrent handler threads from interleaving each other's lines.
    """
    parent = os.path.dirname(os.path.abspath(usage_log))
    os.makedirs(parent, exist_ok=True)
    with open(usage_log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _make_handler(
    upstream: str,
    body_inject: dict[str, Any],
    header_inject: dict[str, str],
    path_filter: str | None,
    usage_log: str | None,
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

            self._tap_usage(request_body, body, status)

        def _tap_usage(self, request_body: bytes, response_body: bytes, status: int) -> None:
            """Append this request's token usage to the configured NDJSON log.

            No-op without ``--usage-log``. Runs only after the relay above is
            complete, and swallows its own failures onto stderr: a trial
            losing a usage record is a gap in accounting, while a trial losing
            its response is a lost trial.

            Token counts and the model only. The bodies passing through carry
            the trial's prompts and travel with the provider credential.

            **One record per provider response, whatever it reported.** A
            response that carried no usage block still happened, and a trial
            whose every request was refused is the case that otherwise reads
            as an agent that worked and did badly: the CLI writes its error
            to stdout, the tool call returns, and the trial is scored against
            an untouched repository. The counts are merged in when present, so
            a reader summing tokens still sees only the requests that reported
            them.
            """
            if usage_log is None:
                return
            try:
                record: dict[str, Any] = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "path": self.path,
                    "status": status,
                    "model": _request_model(request_body),
                }
                counts = _extract_token_counts(response_body)
                if counts is not None:
                    record.update(counts)
                _append_usage_record(usage_log, record)
            except Exception as exc:
                print(f"middleware_proxy: usage tap failed: {exc}", file=sys.stderr)

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


def _build_parser() -> argparse.ArgumentParser:
    """Argv parser for the sidecar. Module-level so ``main`` does not rebuild
    it per call and so tests can import it if they want to inspect flag
    metadata."""
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
    parser.add_argument(
        "--usage-log",
        default=None,
        help=(
            "Append one NDJSON record of token usage per response that reports it "
            "(default: no usage accounting)."
        ),
    )
    parser.add_argument("--daemon", action="store_true", help="Fork and return when port is open.")
    return parser


_PARSER = _build_parser()


def main(argv: list[str] | None = None) -> int:
    args = _PARSER.parse_args(argv)

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

    handler_cls = _make_handler(
        args.upstream, body_inject, header_inject, args.path_filter, args.usage_log
    )

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
