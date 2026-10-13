"""Locks ``RemoteConfig`` parsing and the ``runtime: remote`` node requirement."""

from __future__ import annotations

import warnings

import pytest
from pydantic import ValidationError

from tolokaforge.core.models.run_config import OrchestratorConfig, RemoteConfig

pytestmark = pytest.mark.unit


def _orchestrator(**fields: object) -> OrchestratorConfig:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return OrchestratorConfig(**fields)


def test_remote_config_rejects_a_mistyped_key() -> None:
    with pytest.raises(ValidationError, match="lease_timeout"):
        RemoteConfig(nodes=["a:1"], lease_timeout=10)  # type: ignore[call-arg]


def test_remote_config_defaults_lease_timeout() -> None:
    assert RemoteConfig(nodes=["a:1"]).lease_timeout_s == 300


def test_remote_runtime_without_remote_block_fails_loud() -> None:
    with pytest.raises(ValidationError, match="orchestrator.remote.nodes"):
        _orchestrator(runtime="remote")


def test_remote_runtime_with_empty_nodes_fails_loud() -> None:
    with pytest.raises(ValidationError, match="at least one runner node"):
        _orchestrator(runtime="remote", remote={"nodes": []})


def test_remote_runtime_with_nodes_parses() -> None:
    cfg = _orchestrator(runtime="remote", remote={"nodes": ["a:1", "b:2"]})

    assert cfg.remote is not None
    assert cfg.remote.nodes == ["a:1", "b:2"]


def test_other_runtimes_do_not_require_nodes() -> None:
    assert _orchestrator(runtime="shared").remote is None


def test_remote_config_rejects_duplicate_nodes() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        RemoteConfig(nodes=["a:1", "b:2", "a:1"])


@pytest.mark.parametrize("node", ["no-port", "host:", ":50051", "host:port"])
def test_remote_config_rejects_malformed_nodes(node: str) -> None:
    with pytest.raises(ValidationError, match="host:port"):
        RemoteConfig(nodes=[node])
