"""Unit tests for :class:`PerTrialRuntimeBackend`.

Uses monkey-patched ``DockerCompose`` and ``GrpcRunnerClient`` so tests
run without a Docker daemon. Real-daemon coverage lives in
``tests/integration/docker/test_per_trial_runtime_backend_integration.py``,
gated by ``@pytest.mark.docker``.

Every code path this file exercises is Protocol-level: does the backend
call the right methods on the right stubs, in the right order, and
handle failures per the ADR-0010 contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_task_description
from tolokaforge.core import per_trial_runtime as per_trial_runtime_module
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend, _LocalEnvHandle
from tolokaforge.core.runtime import EnvHandle, ProvisionError, RuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec

pytestmark = pytest.mark.canonical


_FIXTURES = Path(__file__).parent / "fixtures" / "environment_manifest"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCompose:
    """Stand-in for ``testcontainers.compose.DockerCompose``.

    Records lifecycle calls; returns deterministic host + port for
    ``get_service_host_and_port`` so ``endpoints()`` can be asserted
    against known values. Configurable behaviour flags let tests
    exercise failure branches without spinning up real Docker.
    """

    def __init__(
        self,
        context: str,
        compose_file_name: str,
        pull: bool = False,
        build: bool = False,
        wait: bool = True,
    ) -> None:
        self.context = context
        self.compose_file_name = compose_file_name
        self.pull = pull
        self.build = build
        self.wait = wait
        self.started = False
        self.stopped = False
        # Test knobs — mutated by tests before .start() runs.
        self.start_raises: Exception | None = None
        self.exposed_services: dict[str, dict[int, int]] = {
            "default": {50051: 50100},
            "db": {5432: 55432},
        }

    def start(self) -> None:
        if self.start_raises is not None:
            raise self.start_raises
        self.started = True

    def stop(self, down: bool = True) -> None:
        self.stopped = True
        self.down_flag = down

    def get_service_host_and_port(
        self, service_name: str, port: int
    ) -> tuple[str | None, int | None]:
        service = self.exposed_services.get(service_name)
        if service is None:
            raise KeyError(service_name)
        host_port = service.get(port)
        if host_port is None:
            raise ValueError(f"{service_name}: port {port} not exposed")
        return ("127.0.0.1", host_port)

    def get_container(self, service_name: str) -> Any:
        # Present as "not declared" for services outside exposed_services.
        if service_name not in self.exposed_services:
            raise KeyError(service_name)
        return _FakeContainer(self.exposed_services[service_name])


class _FakeContainer:
    def __init__(self, ports: dict[int, int]) -> None:
        self.Publishers = [_FakePublisher(TargetPort=p) for p in ports]


class _FakePublisher:
    def __init__(self, TargetPort: int) -> None:  # noqa: N803 — mirrors Testcontainers API
        self.TargetPort = TargetPort


class _FakeRunnerClient:
    """Stand-in for ``GrpcRunnerClient`` with a recording call log."""

    def __init__(self, runner_address: str) -> None:
        self.runner_address = runner_address
        self.connected = False
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def health_check(self) -> bool:
        return self.connected

    def _record(self, name: str, /, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, (), kwargs))
        return {"success": True, "error": None}

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        return self._record(
            "register_trial",
            trial_id=trial_id,
            trial_spec_json=trial_spec_json,
            default_tool_timeout_s=default_tool_timeout_s,
        )

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float = 30.0,
        executor: str = "agent",
    ) -> Any:
        self.calls.append(
            (
                "execute_tool",
                (),
                {
                    "trial_id": trial_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "timeout_seconds": timeout_seconds,
                    "executor": executor,
                },
            )
        )
        return {"success": True}

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._record(
            "grade_trial",
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
        )

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._record(
            "get_state",
            trial_id=trial_id,
            include_unstable=include_unstable,
            tables=tables,
        )

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict[str, Any]:
        return self._record(
            "reset_trial",
            trial_id=trial_id,
            execute_init_actions=execute_init_actions,
        )

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        return self._record("cleanup_trial", trial_id=trial_id)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_backend(monkeypatch: pytest.MonkeyPatch) -> PerTrialRuntimeBackend:
    """PerTrialRuntimeBackend with DockerCompose + GrpcRunnerClient patched
    to record-only fakes. Every test in this file uses this fixture."""
    monkeypatch.setattr(per_trial_runtime_module, "DockerCompose", _FakeCompose)
    monkeypatch.setattr(per_trial_runtime_module, "GrpcRunnerClient", _FakeRunnerClient)
    return PerTrialRuntimeBackend()


def _make_trial_spec(
    trial_id: str = "task-1:0",
    compose_file: Path | None = None,
) -> TrialSpec:
    manifest = EnvironmentManifest(compose_file=compose_file) if compose_file is not None else None
    return TrialSpec(
        trial_id=trial_id,
        run_id="run_contract_test",
        task=make_task_description(
            task_id="task-1",
            name="probe",
            category="general",
            description="Local backend test",
            environment_manifest=manifest,
        ),
        agent_model_config=ModelConfig(name="claude-sonnet-4-6", provider="anthropic"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:5432",
            runner_url="http://placeholder:50051",
        ),
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_per_trial_runtime_backend_satisfies_runtime_backend(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        assert isinstance(patched_backend, RuntimeBackend)

    def test_all_provisioning_methods_are_present(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        for method in ("provision", "await_ready", "endpoints", "teardown"):
            assert callable(getattr(patched_backend, method))

    def test_all_per_trial_rpc_methods_are_present(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        for method in (
            "register_trial",
            "execute_tool",
            "grade_trial",
            "get_state",
            "reset_trial",
            "cleanup_trial",
        ):
            assert callable(getattr(patched_backend, method))


# ---------------------------------------------------------------------------
# Run-level lifecycle
# ---------------------------------------------------------------------------


class TestRunLevelLifecycle:
    def test_connect_is_a_no_op(self, patched_backend: PerTrialRuntimeBackend) -> None:
        # Must not raise; no side effects tests can check beyond that.
        patched_backend.connect(timeout=5.0, retry_interval=0.1)

    def test_health_check_returns_true_with_no_trials(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        assert patched_backend.health_check() is True

    def test_close_drops_all_cached_clients(self, patched_backend: PerTrialRuntimeBackend) -> None:
        handle = patched_backend.provision(
            _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        )
        assert handle.trial_id in patched_backend._clients
        patched_backend.close()
        assert patched_backend._clients == {}


# ---------------------------------------------------------------------------
# Per-trial provisioning
# ---------------------------------------------------------------------------


class TestProvision:
    def test_returns_handle_with_matching_trial_id(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(
            trial_id="task-1:0", compose_file=_FIXTURES / "safe_two_service.yaml"
        )
        handle = patched_backend.provision(spec)
        assert isinstance(handle, EnvHandle)
        assert handle.trial_id == "task-1:0"

    def test_populates_per_trial_client_cache(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        patched_backend.provision(spec)
        assert spec.trial_id in patched_backend._clients
        client = patched_backend._clients[spec.trial_id]
        assert isinstance(client, _FakeRunnerClient)
        # Connect is deferred to first RPC use — the client is cached but
        # not yet connected.
        assert client.connected is False
        assert spec.trial_id not in patched_backend._connected_trials

    def test_first_rpc_call_triggers_connect(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        patched_backend.provision(spec)
        patched_backend.register_trial(trial_id=spec.trial_id, trial_spec_json="{}")
        client = patched_backend._clients[spec.trial_id]
        assert isinstance(client, _FakeRunnerClient)
        assert client.connected is True
        assert spec.trial_id in patched_backend._connected_trials

    def test_repeated_rpc_calls_do_not_reconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First RPC connects; subsequent RPCs reuse the connected client
        without re-running the connect health-check loop."""

        class _CountingClient(_FakeRunnerClient):
            def __init__(self, runner_address: str) -> None:
                super().__init__(runner_address)
                self.connect_calls = 0

            def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
                self.connect_calls += 1
                super().connect(timeout=timeout, retry_interval=retry_interval)

        monkeypatch.setattr(per_trial_runtime_module, "DockerCompose", _FakeCompose)
        monkeypatch.setattr(per_trial_runtime_module, "GrpcRunnerClient", _CountingClient)
        backend = PerTrialRuntimeBackend()
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        backend.provision(spec)
        backend.register_trial(trial_id=spec.trial_id, trial_spec_json="{}")
        backend.execute_tool(trial_id=spec.trial_id, tool_name="x", arguments={})
        backend.get_state(trial_id=spec.trial_id)
        client = backend._clients[spec.trial_id]
        assert isinstance(client, _CountingClient)
        assert client.connect_calls == 1

    def test_starts_the_compose_stack(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = patched_backend.provision(spec)
        assert isinstance(handle, _LocalEnvHandle)
        assert handle.compose.started is True

    def test_creates_per_trial_temp_directory(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = patched_backend.provision(spec)
        assert isinstance(handle, _LocalEnvHandle)
        assert handle.temp_dir.exists()
        assert handle.temp_dir.is_dir()

    def test_missing_manifest_raises_provision_error(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=None)  # manifest = None
        with pytest.raises(ProvisionError) as exc:
            patched_backend.provision(spec)
        assert exc.value.stage == "provision"
        assert exc.value.trial_id == spec.trial_id

    def test_compose_start_failure_raises_provision_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FailingCompose(_FakeCompose):
            def start(self) -> None:
                raise RuntimeError("simulated compose up failure")

        monkeypatch.setattr(per_trial_runtime_module, "DockerCompose", _FailingCompose)
        monkeypatch.setattr(per_trial_runtime_module, "GrpcRunnerClient", _FakeRunnerClient)
        backend = PerTrialRuntimeBackend()
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        assert exc.value.stage == "provision"
        assert "compose up failed" in exc.value.reason
        # No orphan client cached after failure.
        assert spec.trial_id not in backend._clients

    def test_concurrent_trials_get_independent_temp_dirs(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        spec_a = _make_trial_spec(
            trial_id="task-1:0", compose_file=_FIXTURES / "safe_two_service.yaml"
        )
        spec_b = _make_trial_spec(
            trial_id="task-1:1", compose_file=_FIXTURES / "safe_two_service.yaml"
        )
        h_a = patched_backend.provision(spec_a)
        h_b = patched_backend.provision(spec_b)
        assert isinstance(h_a, _LocalEnvHandle)
        assert isinstance(h_b, _LocalEnvHandle)
        assert h_a.temp_dir != h_b.temp_dir


# ---------------------------------------------------------------------------
# await_ready — no-op contract
# ---------------------------------------------------------------------------


class TestAwaitReady:
    def test_no_op_after_provision(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = patched_backend.provision(spec)
        assert patched_backend.await_ready(handle) is None


# ---------------------------------------------------------------------------
# endpoints — convention-based resolution
# ---------------------------------------------------------------------------


class TestEndpoints:
    def test_returns_env_endpoints(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = patched_backend.provision(spec)
        endpoints = patched_backend.endpoints(handle)
        assert isinstance(endpoints, EnvEndpoints)
        assert endpoints.runner_url == "http://127.0.0.1:50100"
        assert endpoints.db_url == "http://127.0.0.1:55432"
        assert endpoints.rag_url is None

    def test_missing_db_service_raises_at_provision_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Endpoint resolution runs at provision time — a compose stack
        without a ``db`` service fails fast before a handle is returned.
        Prior design deferred the check to :meth:`endpoints` which then
        had to tear down mid-call; that surprising side effect is gone."""

        class _NoDbCompose(_FakeCompose):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.exposed_services = {"default": {50051: 50100}}

        monkeypatch.setattr(per_trial_runtime_module, "DockerCompose", _NoDbCompose)
        monkeypatch.setattr(per_trial_runtime_module, "GrpcRunnerClient", _FakeRunnerClient)
        backend = PerTrialRuntimeBackend()
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_one_service.yaml")
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        assert exc.value.stage == "provision"
        assert "compose service named 'db'" in exc.value.reason
        # No handle returned → no cache entry, no lingering client.
        assert spec.trial_id not in backend._clients

    def test_rag_service_resolves_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _WithRagCompose(_FakeCompose):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.exposed_services = {
                    "default": {50051: 50100},
                    "db": {5432: 55432},
                    "rag": {8080: 58080},
                }

        monkeypatch.setattr(per_trial_runtime_module, "DockerCompose", _WithRagCompose)
        monkeypatch.setattr(per_trial_runtime_module, "GrpcRunnerClient", _FakeRunnerClient)
        backend = PerTrialRuntimeBackend()
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        endpoints = backend.endpoints(handle)
        assert endpoints.rag_url == "http://127.0.0.1:58080"

    def test_endpoints_rejects_foreign_handle(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        class _NotAHandle:
            trial_id = "x"

        with pytest.raises(TypeError):
            patched_backend.endpoints(_NotAHandle())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# teardown — idempotent, releases resources
# ---------------------------------------------------------------------------


class TestTeardown:
    def test_stops_compose_and_removes_temp_dir(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = patched_backend.provision(spec)
        assert isinstance(handle, _LocalEnvHandle)
        temp_dir = handle.temp_dir
        assert temp_dir.exists()
        patched_backend.teardown(handle)
        assert handle.compose.stopped is True
        assert not temp_dir.exists()

    def test_closes_the_cached_runner_client_when_connected(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        """Only clients that were actually connected via first RPC use
        get closed on teardown — a client that was never connected has
        nothing to close on the gRPC side."""
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = patched_backend.provision(spec)
        # Trigger first-use connect via an RPC call.
        patched_backend.register_trial(trial_id=spec.trial_id, trial_spec_json="{}")
        client = patched_backend._clients[spec.trial_id]
        patched_backend.teardown(handle)
        assert isinstance(client, _FakeRunnerClient)
        assert client.closed is True
        assert spec.trial_id not in patched_backend._clients
        assert spec.trial_id not in patched_backend._connected_trials

    def test_never_connected_client_is_not_closed_on_teardown(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        """A trial that never called an RPC has a cached-but-unconnected
        client. teardown removes it from the cache but does not call
        close() on it (nothing to close)."""
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = patched_backend.provision(spec)
        client = patched_backend._clients[spec.trial_id]
        patched_backend.teardown(handle)
        assert isinstance(client, _FakeRunnerClient)
        assert client.closed is False
        assert spec.trial_id not in patched_backend._clients

    def test_teardown_is_idempotent(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = patched_backend.provision(spec)
        patched_backend.teardown(handle)
        patched_backend.teardown(handle)  # must not raise

    def test_teardown_ignores_foreign_handle(self, patched_backend: PerTrialRuntimeBackend) -> None:
        class _NotAHandle:
            trial_id = "x"

        # Silent no-op — contract says teardown is idempotent.
        patched_backend.teardown(_NotAHandle())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Per-trial RPC delegation (ADR-0013)
# ---------------------------------------------------------------------------


class TestPerTrialRpcDelegation:
    def test_register_trial_delegates_to_cached_client(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        patched_backend.provision(spec)
        result = patched_backend.register_trial(trial_id=spec.trial_id, trial_spec_json="{}")
        assert result == {"success": True, "error": None}
        client = patched_backend._clients[spec.trial_id]
        assert isinstance(client, _FakeRunnerClient)
        assert client.calls[-1][0] == "register_trial"

    def test_execute_tool_delegates(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        patched_backend.provision(spec)
        patched_backend.execute_tool(trial_id=spec.trial_id, tool_name="echo", arguments={"x": 1})
        client = patched_backend._clients[spec.trial_id]
        assert isinstance(client, _FakeRunnerClient)
        assert client.calls[-1][0] == "execute_tool"
        assert client.calls[-1][2]["arguments"] == {"x": 1}

    def test_grade_trial_delegates(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        patched_backend.provision(spec)
        patched_backend.grade_trial(trial_id=spec.trial_id)
        client = patched_backend._clients[spec.trial_id]
        assert isinstance(client, _FakeRunnerClient)
        assert client.calls[-1][0] == "grade_trial"

    def test_get_state_delegates(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        patched_backend.provision(spec)
        patched_backend.get_state(trial_id=spec.trial_id)
        client = patched_backend._clients[spec.trial_id]
        assert isinstance(client, _FakeRunnerClient)
        assert client.calls[-1][0] == "get_state"

    def test_reset_trial_delegates(self, patched_backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        patched_backend.provision(spec)
        patched_backend.reset_trial(trial_id=spec.trial_id, execute_init_actions=True)
        client = patched_backend._clients[spec.trial_id]
        assert isinstance(client, _FakeRunnerClient)
        assert client.calls[-1][0] == "reset_trial"
        assert client.calls[-1][2]["execute_init_actions"] is True

    def test_cleanup_trial_delegates_when_client_present(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        patched_backend.provision(spec)
        result = patched_backend.cleanup_trial(spec.trial_id)
        assert result == {"success": True, "error": None}

    def test_cleanup_trial_is_idempotent_when_no_client(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        # No provision → no client. Contract says cleanup_trial is idempotent.
        result = patched_backend.cleanup_trial("never-provisioned:0")
        assert result == {"success": True, "error": None}

    def test_rpc_before_provision_raises_clear_error(
        self, patched_backend: PerTrialRuntimeBackend
    ) -> None:
        with pytest.raises(RuntimeError, match="provision"):
            patched_backend.register_trial(trial_id="never-provisioned:0", trial_spec_json="{}")
