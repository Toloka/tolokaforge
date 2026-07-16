"""Orders API for the multi_service_cache_debug example.

Cache-first FastAPI service over two substrates: a redis cache (read through the
bundled read-only `redis_mini` RESP client) and a postgres `orders` table (the
source of truth, via asyncpg). The cache-invalidation bug lives here — a status
update writes postgres but never invalidates the cached `order:<id>` key, so
`GET /orders/{id}` keeps serving the stale cached value.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from redis_mini import RedisMini

_ORDER_COLUMNS = "order_id, customer_id, product, status, updated_at"


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await asyncpg.create_pool(os.environ["APP_DB_DSN"])
    app.state.cache = RedisMini(os.environ["REDIS_URL"])
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Orders API", lifespan=lifespan)


def _serialize(record: asyncpg.Record) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(record).items()}


async def _fetch_order(query: str, *args: Any) -> dict[str, Any]:
    record = await app.state.pool.fetchrow(query, *args)
    if record is None:
        raise HTTPException(status_code=404, detail="order not found")
    return _serialize(record)


@app.get("/health")
async def health() -> dict[str, str]:
    await app.state.pool.fetchval("SELECT 1")
    return {"status": "ok"}


@app.get("/orders/{order_id}")
async def get_order(order_id: int) -> dict[str, Any]:
    cached = await asyncio.to_thread(app.state.cache.get, f"order:{order_id}")
    if cached is not None:
        return json.loads(cached)
    return await _fetch_order(f"SELECT {_ORDER_COLUMNS} FROM orders WHERE order_id = $1", order_id)


@app.get("/orders/{order_id}/source")
async def get_order_source(order_id: int) -> dict[str, Any]:
    return await _fetch_order(f"SELECT {_ORDER_COLUMNS} FROM orders WHERE order_id = $1", order_id)


@app.post("/orders/{order_id}")
async def update_status(order_id: int, body: StatusUpdate) -> dict[str, Any]:
    # The write updates postgres but deliberately never invalidates order:<id>
    # in redis — that omission is the cache-invalidation bug the agent diagnoses.
    return await _fetch_order(
        f"UPDATE orders SET status = $1, updated_at = now() "
        f"WHERE order_id = $2 RETURNING {_ORDER_COLUMNS}",
        body.status,
        order_id,
    )
