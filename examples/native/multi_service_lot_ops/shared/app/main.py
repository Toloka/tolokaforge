"""Manufacturing lot-operations API for the multi_service_lot_ops example.

A thin FastAPI service over the postgres `app-db` substrate. The agent drives
it over HTTP (service name `app-service:8000`); grading reads the same postgres
directly through a read-only role. Connects as the read/write `app` role via
asyncpg, using the DSN in `APP_DB_DSN`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewCorrectiveAction(_Body):
    reason_code: str
    note: str
    status: str = "open"


class CorrectiveActionPatch(_Body):
    note: str | None = None
    status: str | None = None


class NewAllocation(_Body):
    lot_id: int
    quantity: int


class NewLot(_Body):
    lot_id: int
    lot_code: str
    product: str
    status: str
    quantity: int
    created_at: date


class LotPatch(_Body):
    status: str | None = None
    quantity: int | None = None


class NewOrder(_Body):
    order_id: int
    order_code: str
    lot_id: int
    status: str
    quantity: int
    due_date: date


class OrderPatch(_Body):
    status: str | None = None
    quantity: int | None = None
    due_date: date | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await asyncpg.create_pool(os.environ["APP_DB_DSN"])
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Lot Operations API", lifespan=lifespan)


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in records]


async def _fetch_one(query: str, *args: Any) -> dict[str, Any]:
    record = await app.state.pool.fetchrow(query, *args)
    if record is None:
        raise HTTPException(status_code=404, detail="not found")
    return dict(record)


def _build_update(table: str, id_col: str, fields: dict[str, Any]) -> tuple[str, list[Any]]:
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    assignments = ", ".join(f"{col} = ${i}" for i, col in enumerate(fields, start=1))
    id_placeholder = f"${len(fields) + 1}"
    query = f"UPDATE {table} SET {assignments} WHERE {id_col} = {id_placeholder} RETURNING *"
    return query, list(fields.values())


@app.get("/health")
async def health() -> dict[str, str]:
    await app.state.pool.fetchval("SELECT 1")
    return {"status": "ok"}


@app.get("/lots")
async def list_lots() -> list[dict[str, Any]]:
    return _rows(await app.state.pool.fetch("SELECT * FROM lots ORDER BY lot_id"))


@app.get("/lots/{lot_id}")
async def get_lot(lot_id: int) -> dict[str, Any]:
    return await _fetch_one("SELECT * FROM lots WHERE lot_id = $1", lot_id)


@app.get("/lots/{lot_id}/corrective-actions")
async def lot_corrective_actions(lot_id: int) -> list[dict[str, Any]]:
    return _rows(
        await app.state.pool.fetch(
            "SELECT * FROM corrective_actions WHERE lot_id = $1 ORDER BY ca_id", lot_id
        )
    )


@app.get("/orders")
async def list_orders() -> list[dict[str, Any]]:
    return _rows(await app.state.pool.fetch("SELECT * FROM production_orders ORDER BY order_id"))


@app.get("/orders/{order_id}")
async def get_order(order_id: int) -> dict[str, Any]:
    return await _fetch_one("SELECT * FROM production_orders WHERE order_id = $1", order_id)


@app.get("/orders/{order_id}/allocations")
async def order_allocations(order_id: int) -> list[dict[str, Any]]:
    return _rows(
        await app.state.pool.fetch(
            "SELECT * FROM allocations WHERE order_id = $1 ORDER BY alloc_id", order_id
        )
    )


@app.get("/corrective-actions")
async def list_corrective_actions() -> list[dict[str, Any]]:
    return _rows(await app.state.pool.fetch("SELECT * FROM corrective_actions ORDER BY ca_id"))


@app.get("/corrective-actions/{ca_id}")
async def get_corrective_action(ca_id: int) -> dict[str, Any]:
    return await _fetch_one("SELECT * FROM corrective_actions WHERE ca_id = $1", ca_id)


@app.get("/reason-codes")
async def list_reason_codes() -> list[dict[str, Any]]:
    return _rows(await app.state.pool.fetch("SELECT * FROM reason_codes ORDER BY code"))


@app.post("/lots/{lot_id}/corrective-actions", status_code=201)
async def open_corrective_action(lot_id: int, body: NewCorrectiveAction) -> dict[str, Any]:
    await _fetch_one("SELECT lot_id FROM lots WHERE lot_id = $1", lot_id)
    return await _fetch_one(
        "INSERT INTO corrective_actions (lot_id, reason_code, note, status) "
        "VALUES ($1, $2, $3, $4) RETURNING *",
        lot_id,
        body.reason_code,
        body.note,
        body.status,
    )


@app.patch("/corrective-actions/{ca_id}")
async def patch_corrective_action(ca_id: int, body: CorrectiveActionPatch) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    query, values = _build_update("corrective_actions", "ca_id", fields)
    return await _fetch_one(query, *values, ca_id)


@app.post("/corrective-actions/{ca_id}/close")
async def close_corrective_action(ca_id: int) -> dict[str, Any]:
    return await _fetch_one(
        "UPDATE corrective_actions SET status = 'closed' WHERE ca_id = $1 RETURNING *", ca_id
    )


@app.post("/orders/{order_id}/allocations", status_code=201)
async def add_allocation(order_id: int, body: NewAllocation) -> dict[str, Any]:
    await _fetch_one("SELECT order_id FROM production_orders WHERE order_id = $1", order_id)
    return await _fetch_one(
        "INSERT INTO allocations (order_id, lot_id, quantity) VALUES ($1, $2, $3) RETURNING *",
        order_id,
        body.lot_id,
        body.quantity,
    )


@app.delete("/orders/{order_id}/allocations/{alloc_id}")
async def delete_allocation(order_id: int, alloc_id: int) -> dict[str, str]:
    deleted = await app.state.pool.fetchval(
        "DELETE FROM allocations WHERE alloc_id = $1 AND order_id = $2 RETURNING alloc_id",
        alloc_id,
        order_id,
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted"}


@app.post("/orders/{order_id}/close")
async def close_order(order_id: int) -> dict[str, Any]:
    return await _fetch_one(
        "UPDATE production_orders SET status = 'closed' WHERE order_id = $1 RETURNING *", order_id
    )


@app.patch("/lots/{lot_id}")
async def patch_lot(lot_id: int, body: LotPatch) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    query, values = _build_update("lots", "lot_id", fields)
    return await _fetch_one(query, *values, lot_id)


@app.post("/lots", status_code=201)
async def create_lot(body: NewLot) -> dict[str, Any]:
    return await _fetch_one(
        "INSERT INTO lots (lot_id, lot_code, product, status, quantity, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
        body.lot_id,
        body.lot_code,
        body.product,
        body.status,
        body.quantity,
        body.created_at,
    )


@app.post("/orders", status_code=201)
async def create_order(body: NewOrder) -> dict[str, Any]:
    return await _fetch_one(
        "INSERT INTO production_orders (order_id, order_code, lot_id, status, quantity, due_date) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
        body.order_id,
        body.order_code,
        body.lot_id,
        body.status,
        body.quantity,
        body.due_date,
    )


@app.patch("/orders/{order_id}")
async def patch_order(order_id: int, body: OrderPatch) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    query, values = _build_update("production_orders", "order_id", fields)
    return await _fetch_one(query, *values, order_id)
