"""Orders service for the multi_service_endpoint_add example.

FastAPI over asyncpg exposing three read endpoints over the orders + customers
substrate. It has no ``GET /orders/{order_id}/summary`` — adding that endpoint
(order joined with its customer) is the agent's task; ``tests/test_summary.py``
fails until it exists.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException

_ORDER_COLUMNS = "order_id, customer_id, product, status, amount::float8 AS amount"
_CUSTOMER_COLUMNS = "customer_id, name, email, tier"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await asyncpg.create_pool(os.environ["APP_DB_DSN"])
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Orders API", lifespan=lifespan)


def _serialize(record: asyncpg.Record) -> dict[str, Any]:
    return dict(record)


async def _fetch_one(query: str, *args: Any) -> dict[str, Any]:
    record = await app.state.pool.fetchrow(query, *args)
    if record is None:
        raise HTTPException(status_code=404, detail="not found")
    return _serialize(record)


@app.get("/health")
async def health() -> dict[str, str]:
    await app.state.pool.fetchval("SELECT 1")
    return {"status": "ok"}


@app.get("/orders")
async def list_orders() -> list[dict[str, Any]]:
    rows = await app.state.pool.fetch(f"SELECT {_ORDER_COLUMNS} FROM orders ORDER BY order_id")
    return [_serialize(r) for r in rows]


@app.get("/orders/{order_id}")
async def get_order(order_id: int) -> dict[str, Any]:
    return await _fetch_one(f"SELECT {_ORDER_COLUMNS} FROM orders WHERE order_id = $1", order_id)


@app.get("/customers/{customer_id}")
async def get_customer(customer_id: int) -> dict[str, Any]:
    return await _fetch_one(
        f"SELECT {_CUSTOMER_COLUMNS} FROM customers WHERE customer_id = $1", customer_id
    )
