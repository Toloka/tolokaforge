"""Delivery-tracker API for the multi_service_helpdesk_workflow example.

A thin FastAPI service over the postgres `app-db` substrate. The agent drives
it over HTTP (service name `delivery-tracker:8000`) to read a delivery and
annotate it with the chosen resolution. Connects as the read/write `app` role
via asyncpg, using the DSN in `APP_DB_DSN`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict


class DeliveryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution_path: str
    resolution_note: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await asyncpg.create_pool(os.environ["APP_DB_DSN"])
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Delivery Tracker API", lifespan=lifespan)


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


@app.get("/deliveries")
async def list_deliveries() -> list[dict[str, Any]]:
    return _rows(await app.state.pool.fetch("SELECT * FROM deliveries ORDER BY delivery_id"))


@app.get("/deliveries/{delivery_id}")
async def get_delivery(delivery_id: int) -> dict[str, Any]:
    return await _fetch_one("SELECT * FROM deliveries WHERE delivery_id = $1", delivery_id)


@app.patch("/deliveries/{delivery_id}")
async def patch_delivery(delivery_id: int, body: DeliveryPatch) -> dict[str, Any]:
    return await _fetch_one(
        "UPDATE deliveries SET resolution_path = $1, resolution_note = $2 "
        "WHERE delivery_id = $3 RETURNING *",
        body.resolution_path,
        body.resolution_note,
        delivery_id,
    )
