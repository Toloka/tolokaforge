"""Client-locations API for the multi_service_helpdesk_workflow example.

A thin FastAPI service over the postgres `app-db` substrate. The agent drives
it over HTTP (service name `client-locations:8000`) to learn a receiving
site's staffed window and its on-site capabilities (cold storage, specialist).
Connects as the read/write `app` role via asyncpg, using the DSN in
`APP_DB_DSN`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await asyncpg.create_pool(os.environ["APP_DB_DSN"])
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Client Locations API", lifespan=lifespan)


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in records]


async def _fetch_one(query: str, *args: Any) -> dict[str, Any]:
    record = await app.state.pool.fetchrow(query, *args)
    if record is None:
        raise HTTPException(status_code=404, detail="not found")
    return dict(record)


@app.get("/health")
async def health() -> dict[str, str]:
    await app.state.pool.fetchval("SELECT 1")
    return {"status": "ok"}


@app.get("/sites")
async def list_sites() -> list[dict[str, Any]]:
    return _rows(await app.state.pool.fetch("SELECT * FROM sites ORDER BY customer_id"))


@app.get("/sites/{customer_id}")
async def get_site(customer_id: str) -> dict[str, Any]:
    return await _fetch_one("SELECT * FROM sites WHERE customer_id = $1", customer_id)
