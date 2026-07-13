"""Minimal audited allowlisting HTTP CONNECT proxy for BYOH containers."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from datetime import datetime, timezone
from urllib.parse import urlsplit


class Allowlist:
    def __init__(self, entries: list[str]) -> None:
        self.hosts = {entry.lower() for entry in entries if "/" not in entry}
        self.networks = [
            ipaddress.ip_network(entry, strict=False) for entry in entries if "/" in entry
        ]

    async def permits(self, hostname: str) -> bool:
        value = hostname.rstrip(".").lower()
        if value in self.hosts or any(
            entry.startswith("*.") and value.endswith(entry[1:]) for entry in self.hosts
        ):
            return True
        try:
            addresses = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: {
                    item[4][0] for item in socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)
                },
            )
        except socket.gaierror:
            return False
        return bool(addresses) and all(
            any(ipaddress.ip_address(address) in network for network in self.networks)
            for address in addresses
        )


def audit(host: str, port: int, status: str) -> None:
    print(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "destination": f"{host}:{port}",
                "status": status,
            }
        ),
        flush=True,
    )


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, allowlist: Allowlist
) -> None:
    try:
        first_line = await asyncio.wait_for(reader.readline(), timeout=15)
        method, target, version = first_line.decode("latin-1").strip().split(" ", 2)
        headers: list[bytes] = []
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            headers.append(line)

        if method.upper() == "CONNECT":
            host, _, raw_port = target.rpartition(":")
            port = int(raw_port or "443")
        else:
            parsed = urlsplit(target)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)

        if not host or not await allowlist.permits(host):
            audit(host or "invalid", port, "denied")
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 38\r\nConnection: close\r\n\r\n"
                b"Destination denied by agent allowlist\n"
            )
            await writer.drain()
            writer.close()
            return

        upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        audit(host, port, "allowed")
        if method.upper() == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            parsed = urlsplit(target)
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            upstream_writer.write(f"{method} {path} {version}\r\n".encode("latin-1"))
            for header in headers:
                if not header.lower().startswith(b"proxy-"):
                    upstream_writer.write(header)
            upstream_writer.write(b"\r\n")
            await upstream_writer.drain()
        await asyncio.gather(
            pipe(reader, upstream_writer),
            pipe(upstream_reader, writer),
            return_exceptions=True,
        )
    except Exception as exc:
        audit("proxy-error", 0, type(exc).__name__)
        writer.close()


async def main() -> None:
    entries = json.loads(os.environ.get("TOLOKAFORGE_PROXY_ALLOWLIST", "[]"))
    allowlist = Allowlist(entries)
    server = await asyncio.start_server(
        lambda reader, writer: handle(reader, writer, allowlist), "0.0.0.0", 8080
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
