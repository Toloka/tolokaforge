"""Locks ``Orchestrator._prewarm_runner_host_endpoint`` and
``_resolve_runtime_connect_budget``.

The prewarm runs after ``service_stack.start_all(wait=True)`` and before
``runtime_backend.connect()`` in the built-in engine path. Compose's per-
container HEALTHCHECK reports "gRPC bound its port INSIDE the container"; the
published host port has propagation lag under Docker Desktop, so the client-
side connect below sometimes races the port publish and times out. This host-
side gRPC probe closes the gap; a refusal here raises actionably with the
operator knob named, rather than surfacing later as a confusing
``Runner service not healthy after 30.1s`` from the client.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    RuntimeConnectConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.docker.health import HealthProbeError, ProbeResult, ProbeType

pytestmark = pytest.mark.unit


def _orchestrator(tmp_path: Path, runtime_connect: RuntimeConnectConfig) -> Orchestrator:
    return Orchestrator(
        RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(
                workers=1,
                repeats=1,
                auto_start_services=False,
                runtime_connect=runtime_connect,
            ),
            evaluation=EvaluationConfig(
                output_dir=str(tmp_path / "results"), projects=[str(tmp_path)]
            ),
        ),
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda _ctx: InMemoryConductor(),
        ),
    )


def test_resolve_runtime_connect_budget_uses_yaml_when_env_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOLOKAFORGE_RUNNER_CONNECT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("TOLOKAFORGE_RUNNER_CONNECT_RETRY_INTERVAL_S", raising=False)
    orch = _orchestrator(tmp_path, RuntimeConnectConfig(timeout_s=90.0, retry_interval_s=0.5))
    assert orch._resolve_runtime_connect_budget() == (90.0, 0.5)


def test_resolve_runtime_connect_budget_env_wins_over_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOLOKAFORGE_RUNNER_CONNECT_TIMEOUT_S", "120")
    monkeypatch.setenv("TOLOKAFORGE_RUNNER_CONNECT_RETRY_INTERVAL_S", "2")
    orch = _orchestrator(tmp_path, RuntimeConnectConfig(timeout_s=30.0, retry_interval_s=1.0))
    assert orch._resolve_runtime_connect_budget() == (120.0, 2.0)


def test_prewarm_probe_uses_resolved_budget_and_returns_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOLOKAFORGE_RUNNER_CONNECT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("TOLOKAFORGE_RUNNER_CONNECT_RETRY_INTERVAL_S", raising=False)
    orch = _orchestrator(tmp_path, RuntimeConnectConfig(timeout_s=45.0, retry_interval_s=0.25))
    probe = MagicMock()
    probe.wait.return_value = ProbeResult(healthy=True, message="ok")
    with patch("tolokaforge.core.orchestrator.HealthProbe.grpc", return_value=probe) as grpc_ctor:
        orch._prewarm_runner_host_endpoint("localhost:54321")
    grpc_ctor.assert_called_once_with(host="localhost", port=54321, timeout_s=45.0, interval_s=0.25)
    probe.wait.assert_called_once_with()


def test_prewarm_probe_failure_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOLOKAFORGE_RUNNER_CONNECT_TIMEOUT_S", raising=False)
    orch = _orchestrator(tmp_path, RuntimeConnectConfig(timeout_s=30.0, retry_interval_s=1.0))
    probe = MagicMock()
    probe.wait.side_effect = HealthProbeError(
        ProbeType.GRPC, "localhost:54321", "connection refused after 30 attempts"
    )
    with patch("tolokaforge.core.orchestrator.HealthProbe.grpc", return_value=probe):
        with pytest.raises(RuntimeError) as exc_info:
            orch._prewarm_runner_host_endpoint("localhost:54321")
    msg = str(exc_info.value)
    assert "localhost:54321" in msg
    assert "30.0" in msg
    assert "orchestrator.runtime_connect.timeout_s" in msg
    assert "TOLOKAFORGE_RUNNER_CONNECT_TIMEOUT_S" in msg
