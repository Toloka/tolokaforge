"""Regenerate the `cache_poisoned.rdb` redis_dump seed for this example.

Boots a throwaway `redis:7-alpine` (RDB-only, matching the compose service),
SETs `order:4021` to the STALE cached value (`status: "processing"`), SAVEs, and
copies the resulting `dump.rdb` out to `cache_poisoned.rdb` next to this script.
The same technique as `tests/integration/reset_recipes/test_redis_dump_recipe.py`.
Stdlib + a running Docker daemon only.

After regenerating, re-stamp the seed digest so `project.yaml` matches:

    uv run tolokaforge assets stamp examples/native/multi_service_cache_debug
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

REDIS_IMAGE = "redis:7-alpine"
STALE_ORDER = {
    "order_id": 4021,
    "customer_id": "ACME",
    "product": "Widget crate",
    "status": "processing",
    "updated_at": "2026-07-10T09:00:00+00:00",
}
PING_MAX_ATTEMPTS = 30


def _run(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(args), capture_output=True, check=True)


def _wait_for_ping(container: str) -> None:
    for _ in range(PING_MAX_ATTEMPTS):
        result = subprocess.run(
            ["docker", "exec", container, "redis-cli", "PING"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and b"PONG" in result.stdout:
            return
        time.sleep(1)
    raise RuntimeError(f"redis container {container!r} did not answer PING")


def build_seed(out_path: Path) -> None:
    container = f"cache-poisoned-seed-{uuid.uuid4().hex[:8]}"
    _run(
        "docker",
        "run",
        "-d",
        "--name",
        container,
        REDIS_IMAGE,
        "redis-server",
        "--save",
        "",
        "--appendonly",
        "no",
    )
    try:
        _wait_for_ping(container)
        _run("docker", "exec", container, "redis-cli", "SET", "order:4021", json.dumps(STALE_ORDER))
        _run("docker", "exec", container, "redis-cli", "SAVE")
        _run("docker", "cp", f"{container}:/data/dump.rdb", str(out_path))
    finally:
        _run("docker", "rm", "-f", container)


if __name__ == "__main__":
    build_seed(Path(__file__).parent / "cache_poisoned.rdb")
