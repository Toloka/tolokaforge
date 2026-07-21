"""Product-catalog API for the multi_service_helpdesk_workflow example.

A thin FastAPI service over the postgres `app-db` substrate. The agent drives
it over HTTP (service name `product-catalog:8000`) to learn whether a product
is temperature-sensitive and its hold limits. Connects as the read/write `app`
role via asyncpg, using the DSN in `APP_DB_DSN`.
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


app = FastAPI(title="Product Catalog API", lifespan=lifespan)


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


@app.get("/products")
async def list_products() -> list[dict[str, Any]]:
    return _rows(await app.state.pool.fetch("SELECT * FROM products ORDER BY sku"))


@app.get("/products/{sku}")
async def get_product(sku: str) -> dict[str, Any]:
    return await _fetch_one("SELECT * FROM products WHERE sku = $1", sku)
