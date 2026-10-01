"""Serve a real ASGI app over HTTP on an ephemeral loopback port.

For a test whose requests must cross a real ``httpx`` transport. The in-process
``MockAsyncClient`` drops a per-request ``timeout=``, so a client that passes
its own budget per request, as the JSON-DB tool wrapper does, is only exercised
end to end here.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import uvicorn

__all__ = ["serve_asgi_on_loopback"]

_STARTUP_TIMEOUT_S = 10.0
_SHUTDOWN_TIMEOUT_S = 10.0


@contextmanager
def serve_asgi_on_loopback(app: Any) -> Iterator[str]:
    """Run ``app`` under uvicorn in a daemon thread and yield its ``http://127.0.0.1:<port>`` URL.

    The socket is bound before the thread starts, so the port is fixed and
    nothing else can take it. ``log_config=None`` keeps uvicorn from
    reconfiguring the process's logging, which other tests' ``caplog`` reads.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name=f"loopback-asgi-{port}", daemon=True
    )
    thread.start()
    try:
        _wait_until_started(server, thread)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=_SHUTDOWN_TIMEOUT_S)
        sock.close()
        if thread.is_alive():
            raise RuntimeError(f"loopback ASGI server on port {port} did not stop")


def _wait_until_started(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("loopback ASGI server thread exited before it started serving")
        if time.monotonic() > deadline:
            raise RuntimeError(f"loopback ASGI server did not start within {_STARTUP_TIMEOUT_S}s")
        time.sleep(0.01)
