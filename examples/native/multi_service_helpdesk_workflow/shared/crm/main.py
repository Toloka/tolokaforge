"""CRM API for the multi_service_helpdesk_workflow example.

A thin FastAPI service over the postgres `app-db` substrate. The agent drives
it over HTTP (service name `crm:8000`) to record the resolution as a case.
Grading reads the `crm_cases` table directly through a read-only role, so the
`POST /cases` write is the only thing that can populate it. Connects as the
read/write `app` role via asyncpg, using the DSN in `APP_DB_DSN`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict


class NewCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_id: int
    customer_id: str
    resolution_path: str
    summary: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await asyncpg.create_pool(os.environ["APP_DB_DSN"])
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="CRM API", lifespan=lifespan)


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


@app.get("/cases")
async def list_cases() -> list[dict[str, Any]]:
    return _rows(await app.state.pool.fetch("SELECT * FROM crm_cases ORDER BY case_id"))


@app.get("/cases/{case_id}")
async def get_case(case_id: int) -> dict[str, Any]:
    return await _fetch_one("SELECT * FROM crm_cases WHERE case_id = $1", case_id)


@app.post("/cases", status_code=201)
async def create_case(body: NewCase) -> dict[str, Any]:
    return await _fetch_one(
        "INSERT INTO crm_cases (delivery_id, customer_id, resolution_path, summary) "
        "VALUES ($1, $2, $3, $4) RETURNING *",
        body.delivery_id,
        body.customer_id,
        body.resolution_path,
        body.summary,
    )
