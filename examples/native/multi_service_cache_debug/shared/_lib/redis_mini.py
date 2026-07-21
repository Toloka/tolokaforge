"""Minimal read-only Redis client for the multi_service_cache_debug example.

Speaks the Redis RESP protocol over a raw stdlib socket so the pack's FastAPI
services can read cached values without a `redis` client in the runner image.
Read-only by design: exposes GET and KEYS only, never writes — the poisoned
cache state comes from the redis_dump seed, not from any runtime write.
"""

from __future__ import annotations

import socket
from typing import BinaryIO
from urllib.parse import urlparse


class RedisMini:
    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 6379

    def _command(self, *args: str) -> object:
        payload = f"*{len(args)}\r\n".encode()
        for arg in args:
            encoded = arg.encode()
            payload += f"${len(encoded)}\r\n".encode() + encoded + b"\r\n"
        with socket.create_connection((self._host, self._port), timeout=5) as sock:
            sock.sendall(payload)
            return _read_reply(sock.makefile("rb"))

    def get(self, key: str) -> str | None:
        reply = self._command("GET", key)
        if reply is None or isinstance(reply, str):
            return reply
        raise RuntimeError(f"GET {key!r} returned non-string reply {reply!r}")

    def keys(self, pattern: str = "*") -> list[str]:
        reply = self._command("KEYS", pattern)
        if isinstance(reply, list):
            return reply
        raise RuntimeError(f"KEYS {pattern!r} returned non-array reply {reply!r}")


def _read_reply(reader: BinaryIO) -> object:
    line = reader.readline()
    prefix, rest = line[:1], line[1:].strip()
    if prefix == b"+":
        return rest.decode()
    if prefix == b"-":
        raise RuntimeError(rest.decode())
    if prefix == b":":
        return int(rest)
    if prefix == b"$":
        length = int(rest)
        if length == -1:
            return None
        data = reader.read(length)
        reader.read(2)  # trailing CRLF
        return data.decode()
    if prefix == b"*":
        count = int(rest)
        if count == -1:
            return None
        return [_read_reply(reader) for _ in range(count)]
    raise RuntimeError(f"unexpected RESP prefix {prefix!r}")
