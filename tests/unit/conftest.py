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


@pytest.fixture
def isolated_secret_manager():
    """A fresh default SecretManager and a cold redaction cache, restored after.

    ``register_runtime_secret`` replaces the process-wide singleton, and the
    log redactor caches its scrub set keyed by that manager's identity. Any
    test that registers a runtime credential has to hand both back, or it
    leaks a credential into every test that runs after it — and a second
    registration of the same name with a different value raises.
    """
    from tolokaforge.secrets import log_filter as log_filter_module
    from tolokaforge.secrets import manager as manager_module

    saved_manager = manager_module._default_manager
    saved_cached_manager = log_filter_module._cached_manager
    saved_cached_values = log_filter_module._cached_values
    manager_module._default_manager = None
    log_filter_module._cached_manager = None
    log_filter_module._cached_values = frozenset()
    yield
    manager_module._default_manager = saved_manager
    log_filter_module._cached_manager = saved_cached_manager
    log_filter_module._cached_values = saved_cached_values


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
    # ``tolokaforge.docker.stacks.core`` no longer resolves a host-side
    # wheel — the runner Dockerfile builds the runner-subset wheel in the
    # ``hatch build --target custom`` stage of the multi-stage image
    # (ADR-0025 § subset target; ADR-0027 § the shim's build path). The
    # remaining ``resolve_wheel`` consumers (``builder`` for the rag stack,
    # ``stacks.full`` for its own service) still need the mock so
    # ``core_stack()`` / ``full_stack()`` can compose without a real wheel
    # build on disk.
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
            "tolokaforge.docker.stacks.full.resolve_wheel",
            return_value=artifact,
        ),
    ):
        yield artifact
