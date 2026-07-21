"""Policy-search API for the multi_service_helpdesk_workflow example.

A thin FastAPI service over the postgres `app-db` substrate that exposes the
policy corpus via postgres full-text search. The agent drives it over HTTP
(service name `policy-search:8000`).

Search is a `POST /search` with the query in the JSON body (not a `GET` query
string) so the request URL is fixed and the grader's `required_actions` rule
can match it by exact URL + method. Connects as the read/write `app` role via
asyncpg, using the DSN in `APP_DB_DSN`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str
    top_k: int = 5


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await asyncpg.create_pool(os.environ["APP_DB_DSN"])
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Policy Search API", lifespan=lifespan)


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in records]


@app.get("/health")
async def health() -> dict[str, str]:
    await app.state.pool.fetchval("SELECT 1")
    return {"status": "ok"}


@app.post("/search")
async def search(body: SearchQuery) -> list[dict[str, Any]]:
    return _rows(
        await app.state.pool.fetch(
            "SELECT policy_id, title, body, "
            "ts_rank(ts, websearch_to_tsquery('english', $1)) AS rank "
            "FROM policy_docs "
            "WHERE ts @@ websearch_to_tsquery('english', $1) "
            "ORDER BY rank DESC, policy_id "
            "LIMIT $2",
            body.q,
            body.top_k,
        )
    )


@app.get("/policies/{policy_id}")
async def get_policy(policy_id: str) -> dict[str, Any]:
    record = await app.state.pool.fetchrow(
        "SELECT policy_id, title, body FROM policy_docs WHERE policy_id = $1", policy_id
    )
    if record is None:
        raise HTTPException(status_code=404, detail="not found")
    return dict(record)
