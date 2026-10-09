"""A made-up world service answering the app world administration protocol (ADR-0058).

:func:`fake_app_world` builds a small ASGI app holding one help-desk world: a
``tickets`` table behind a vendor-shaped REST API, administered under ``/_admin``
behind ``X-Admin-Token``. It claims the first admin token presented, answers every
vendor request ``503`` until tables are loaded, and resolves a vendor request's
``Authorization: Bearer <token>`` to the caller the runner loaded for that token.
Served with :func:`tests.utils.loopback_asgi.serve_asgi_on_loopback`, it is a real
HTTP service for the runner's client and for ``http_request`` alike.

:data:`MCP_WORLD_SERVER` is the same world behind a stdio MCP server, the in-process
holder an MCP pack uses, so one golden path can be graded through both holders.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

ADMIN_HEADER = "x-admin-token"

INITIAL_TABLES: dict[str, list[dict[str, Any]]] = {
    "tickets": [{"id": 1, "subject": "seeded ticket", "requester": "default"}],
}
"""The world every test starts from, as tables."""

DEFAULT_CALLER = "default"
"""The caller a token loaded as ``null`` resolves to."""


def next_ticket(tickets: list[dict[str, Any]], subject: str, requester: str) -> dict[str, Any]:
    """The ticket a create appends: sequential ids, as a seeded world numbers them."""
    return {
        "id": max((ticket["id"] for ticket in tickets), default=0) + 1,
        "subject": subject,
        "requester": requester,
    }


@dataclass
class FakeAppWorld:
    """The service's own state, inspectable by a test."""

    admin_token: str | None = None
    callers: dict[str, str | None] = field(default_factory=dict)
    tables: dict[str, list[dict[str, Any]]] | None = None
    admin_calls: list[str] = field(default_factory=list)

    def build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/_health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.put("/_admin/tokens")
        async def load_tokens(request: Request) -> dict[str, int]:
            self._admit(request, claiming=True)
            self.callers = await request.json()
            return {"loaded": len(self.callers)}

        @app.put("/_admin/tables")
        async def load_tables(request: Request) -> dict[str, int]:
            self._admit(request)
            self.tables = copy.deepcopy(await request.json())
            return {"tables": len(self.tables)}

        @app.get("/_admin/tables")
        def read_tables(request: Request) -> dict[str, Any]:
            self._admit(request)
            return self._world()

        @app.get("/api/tickets")
        def list_tickets(authorization: str | None = Header(default=None)) -> list[dict]:
            self._caller(authorization)
            return self._world()["tickets"]

        @app.post("/api/tickets", status_code=201)
        async def create_ticket(
            request: Request, authorization: str | None = Header(default=None)
        ) -> dict[str, Any]:
            caller = self._caller(authorization)
            body = await request.json()
            tickets = self._world()["tickets"]
            ticket = next_ticket(tickets, body["subject"], caller)
            tickets.append(ticket)
            return ticket

        @app.get("/api/debug/echo")
        def echo(authorization: str | None = Header(default=None)) -> dict[str, Any]:
            """A careless vendor endpoint quoting the request's credential back."""
            self._caller(authorization)
            return {"authorization": authorization}

        return app

    def _admit(self, request: Request, *, claiming: bool = False) -> None:
        presented = request.headers.get(ADMIN_HEADER)
        self.admin_calls.append(f"{request.method} {request.url.path}")
        if presented is None:
            raise HTTPException(status_code=403, detail="administration needs X-Admin-Token")
        if self.admin_token is None and claiming:
            self.admin_token = presented
        if presented != self.admin_token:
            raise HTTPException(status_code=403, detail="not this world's admin token")

    def _world(self) -> dict[str, list[dict[str, Any]]]:
        if self.tables is None:
            raise HTTPException(status_code=503, detail="no world loaded yet")
        return self.tables

    def _caller(self, authorization: str | None) -> str:
        self._world()
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing credentials")
        token = authorization.removeprefix("Bearer ")
        if token not in self.callers:
            raise HTTPException(status_code=401, detail="unknown token")
        return self.callers[token] or DEFAULT_CALLER


@dataclass
class HeaderRecorder:
    """A host outside the world: records the credential each request carried."""

    seen: list[str | None] = field(default_factory=list)

    def build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/anything")
        def anything(authorization: str | None = Header(default=None)) -> dict[str, bool]:
            self.seen.append(authorization)
            return {"credential": authorization is not None}

        return app


MCP_WORLD_SERVER = f"""
import json, sys

STATE = {INITIAL_TABLES!r}

def reply(request_id, result):
    print(json.dumps({{"jsonrpc": "2.0", "id": request_id, "result": result}}), flush=True)

def text(value):
    return {{"content": [{{"type": "text", "text": json.dumps(value)}}]}}

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        reply(request["id"], {{}})
        continue
    name = request["params"]["name"]
    arguments = request["params"]["arguments"]
    if name == "_tolokaforge_get_state_":
        reply(request["id"], text(STATE))
    elif name == "_tolokaforge_set_state_":
        STATE = json.loads(arguments["state_json"])
        reply(request["id"], text({{"ok": True}}))
    elif name == "create_ticket":
        tickets = STATE["tickets"]
        ticket = {{
            "id": max((t["id"] for t in tickets), default=0) + 1,
            "subject": arguments["subject"],
            "requester": {DEFAULT_CALLER!r},
        }}
        tickets.append(ticket)
        reply(request["id"], text(ticket))
"""
"""The same world as a stdio MCP server: ``create_ticket`` plus the two state tools."""
