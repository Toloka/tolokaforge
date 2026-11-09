"""Shared fixtures for unit tests.

The wheel resolver mock is autouse=True so that any test calling
``core_stack()``, ``get_image_definition("runner")``, or ``build_image("runner")``
gets a fake wheel artifact instead of triggering a real wheel build.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests.utils.secret_state import cold_secret_manager
from tolokaforge.core.hash import table_digest
from tolokaforge.docker.wheel_resolver import WheelArtifact


class FakeMutatingDBClient:
    """Records ``mutate`` calls and applies them to a seeded store, as the db-service does.

    Shared between the Tau and MCP diff-sync tests. Applying the ops matters to the
    MCP sync: it compares the store's digests after mutating, and a store that ignored
    them would always look unlike the holder's and draw a whole-table replace.
    """

    def __init__(self, state: dict[str, list[dict]] | None = None) -> None:
        self._state = {t: [dict(r) for r in v] for t, v in (state or {}).items()}
        self.mutations: list[tuple[str, list[dict]]] = []

    async def get_state(
        self,
        trial_id: str,
        tables: list[str] | None = None,
        timeout: float | None = None,
        *,
        hashes: bool = True,
    ) -> Any:
        if tables:
            return SimpleNamespace(data={t: self._state[t] for t in tables if t in self._state})
        return SimpleNamespace(data=self._state)

    async def get_table_digests(
        self, trial_id: str, tables: list[str] | None = None, timeout: float | None = None
    ) -> dict[str, str]:
        return {
            name: table_digest(rows)
            for name, rows in self._state.items()
            if not tables or name in tables
        }

    async def mutate(
        self,
        trial_id: str,
        table_name: str,
        operations: list[dict],
        timeout: float | None = None,
        *,
        hashes: bool = True,
    ) -> Any:
        self.mutations.append((table_name, operations))
        for op in operations:
            self._state[table_name] = self._applied(self._state.get(table_name, []), op)
        return SimpleNamespace(success=True)

    @staticmethod
    def _applied(rows: list[dict], op: dict) -> list[dict]:
        if op["op"] == "replace":
            return [dict(r) for r in op["records"]]
        if op["op"] == "insert":
            return [*rows, dict(op["record"])]
        if op["op"] == "delete":
            return [r for r in rows if any(r.get(k) != v for k, v in op["filter"].items())]
        assert op["op"] == "upsert", op
        key = op.get("key", "id")
        fields = [key] if isinstance(key, str) else key
        for row in rows:
            if all(row.get(f) == op["record"][f] for f in fields):
                row.update(op["record"])
                return rows
        return [*rows, dict(op["record"])]


@pytest.fixture
def isolated_secret_manager():
    """A fresh default SecretManager and a cold redaction cache, restored after.

    ``register_runtime_secret`` replaces the process-wide singleton, and the
    log redactor caches its scrub set keyed by that manager's identity. Any
    test that registers a runtime credential has to hand both back, or it
    leaks a credential into every test that runs after it — and a second
    registration of the same name with a different value raises.
    """
    with cold_secret_manager():
        yield


@pytest.fixture(autouse=True)
def _no_ambient_typesense_stack(monkeypatch: pytest.MonkeyPatch):
    """Strip a developer machine's TYPESENSE_HOST / TYPESENSE_PORT from every test.

    The runner resolves its TypeSense address from these variables with
    precedence over the task's own connection details, so an exported host on
    the machine running the suite would flip precedence under every test that
    does not set them itself. Tests that want a stack address set the variables
    in their own body, which always runs after fixture setup.
    """
    monkeypatch.delenv("TYPESENSE_HOST", raising=False)
    monkeypatch.delenv("TYPESENSE_PORT", raising=False)


@pytest.fixture(autouse=True)
def _mock_wheel_resolver(tmp_path: Path):
    """Auto-mock resolve_wheel for every unit test.

    Creates a tiny fake ``.whl`` in a temp directory and patches
    ``resolve_wheel`` at every import site that consumes it.
    """
    whl = tmp_path / "tolokaforge-0.2.0-py3-none-any.whl"
    whl.write_bytes(b"PK\x03\x04fake-unit-test-wheel")
    artifact = WheelArtifact(
        path=whl,
        version="0.2.0",
        content_hash="unit-test-hash",
        provider_name="unit-test-mock",
    )
    # Neither stack resolves a host-side wheel directly: the runner Dockerfile
    # builds the runner-subset wheel in its ``hatch build --target custom``
    # stage (ADR-0025 § subset target; ADR-0027 § the shim's build path), and
    # ``full_stack`` resolves the rag-service build-context spec lazily through
    # ``builder.get_image_definition`` on the build path. The sole remaining
    # consumer is ``builder._rag_definition``, so mocking ``builder.resolve_wheel``
    # lets ``core_stack()`` / ``full_stack()`` compose without a real wheel on disk.
    with (
        patch(
            "tolokaforge.docker.wheel_resolver.resolve_wheel",
            return_value=artifact,
        ),
        patch(
            "tolokaforge.docker.builder.resolve_wheel",
            return_value=artifact,
        ),
    ):
        yield artifact
