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

from tolokaforge.docker.wheel_resolver import WheelArtifact


class FakeMutatingDBClient:
    """Recorder for ``mutate`` calls; answers ``get_state`` from a seeded store.

    Shared between the Tau and MCP diff-sync tests — both drive a diff path
    that only touches these two methods on the DB client.
    """

    def __init__(self, state: dict[str, list[dict]] | None = None) -> None:
        self._state = {t: [dict(r) for r in v] for t, v in (state or {}).items()}
        self.mutations: list[tuple[str, list[dict]]] = []

    async def get_state(self, trial_id: str) -> Any:
        return SimpleNamespace(data=self._state)

    async def mutate(self, trial_id: str, table_name: str, operations: list[dict]) -> Any:
        self.mutations.append((table_name, operations))
        return SimpleNamespace(success=True)


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
    with (
        patch(
            "tolokaforge.docker.wheel_resolver.resolve_wheel",
            return_value=artifact,
        ),
        patch(
            "tolokaforge.docker.builder.resolve_wheel",
            return_value=artifact,
        ),
        patch(
            "tolokaforge.docker.stacks.core.resolve_wheel",
            return_value=artifact,
        ),
        patch(
            "tolokaforge.docker.stacks.full.resolve_wheel",
            return_value=artifact,
        ),
    ):
        yield artifact
