"""Cache-admin API for the multi_service_cache_debug example.

Read-only FastAPI inspector over the redis cache, using the bundled `redis_mini`
RESP client. Lets the agent see the raw cached value behind a key — the layer
where the stale `order:4021` value lives — without a redis client in the image.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis_mini import RedisMini


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.cache = RedisMini(os.environ["REDIS_URL"])
    yield


app = FastAPI(title="Cache Admin API", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    await asyncio.to_thread(app.state.cache.keys, "*")
    return {"status": "ok"}


@app.get("/keys")
async def list_keys() -> dict[str, list[str]]:
    return {"keys": await asyncio.to_thread(app.state.cache.keys, "*")}


@app.get("/cache/{key}")
async def get_cache(key: str) -> dict[str, str | None]:
    return {"key": key, "value": await asyncio.to_thread(app.state.cache.get, key)}
