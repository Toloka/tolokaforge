"""Pin the ``RuntimeBackend`` Protocol contract — runtime check + parity.

Two implementations are checked: :class:`SharedStackRuntimeBackend` (real gRPC client
wrapper, never ``connect()``-ed in this test) and
:class:`InMemoryRuntimeBackend` (no-network test fixture). Lifecycle
methods and the retry-cleanup method are exercised on both; ``connect()``
itself is *not* invoked on ``SharedStackRuntimeBackend`` because that would require
a real runner gRPC server.

The lower half of the file pins the ADR-0010 provisioning surface:
``provision`` / ``await_ready`` / ``endpoints`` / ``teardown``,
``EnvHandle``, ``ProvisionError``. Those provisions materialise a compose file
for real, credential injection included, so the package-level
``_pin_fake_secrets`` pins the manager whose payload reaches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import make_env_endpoints, make_task_description
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.runtime import (
    EnvHandle,
    InMemoryRuntimeBackend,
    ProvisionError,
    RuntimeBackend,
    RuntimeBackendCallLog,
)
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import TaskDescription

_FIXTURES = Path(__file__).parent / "fixtures" / "environment_manifest"

pytestmark = pytest.mark.canonical


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; every implementation
    satisfies it via ``isinstance`` (not just by structural type-hint
    compatibility).
    """

    def test_shared_stack_runtime_backend_passes_isinstance(self) -> None:
        assert isinstance(
            SharedStackRuntimeBackend(runner_address="sentinel:50051"), RuntimeBackend
        )

    def test_in_memory_runtime_backend_passes_isinstance(self) -> None:
        assert isinstance(InMemoryRuntimeBackend(), RuntimeBackend)

    def test_per_trial_runtime_backend_passes_isinstance(self) -> None:
        from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend

        assert isinstance(PerTrialRuntimeBackend(), RuntimeBackend)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotARuntime:
            pass

        assert not isinstance(_NotARuntime(), RuntimeBackend)


class TestLifecycleMethodParity:
    """Every Protocol method accepts the same arguments on every
    implementation. ``connect()`` itself is exercised only on the
    in-memory impl because the Docker variant requires a real gRPC
    server; instead this class asserts the *method signature* exists
    on both via ``getattr``.
    """

    @pytest.fixture
    def implementations(self) -> list[RuntimeBackend]:
        # ``SharedStackRuntimeBackend`` is constructed but never ``connect()``-ed —
        # construction is cheap and side-effect-free until ``connect()``.
        return [
            InMemoryRuntimeBackend(),
            SharedStackRuntimeBackend(runner_address="sentinel:50051"),
        ]

    def test_connect_signature_is_present(self, implementations: list[RuntimeBackend]) -> None:
        for impl in implementations:
            assert callable(getattr(impl, "connect", None))

    def test_close_signature_is_present(self, implementations: list[RuntimeBackend]) -> None:
        for impl in implementations:
            assert callable(getattr(impl, "close", None))

    def test_health_check_signature_is_present(self, implementations: list[RuntimeBackend]) -> None:
        for impl in implementations:
            assert callable(getattr(impl, "health_check", None))

    def test_cleanup_trial_signature_is_present(
        self, implementations: list[RuntimeBackend]
    ) -> None:
        for impl in implementations:
            assert callable(getattr(impl, "cleanup_trial", None))

    @pytest.mark.parametrize(
        "method_name",
        [
            "register_trial",
            "execute_tool",
            "grade_trial",
            "get_state",
            "reset_trial",
        ],
    )
    def test_per_trial_rpc_methods_are_present(
        self, implementations: list[RuntimeBackend], method_name: str
    ) -> None:
        """ADR-0013 — the five per-trial RPC methods live on the Protocol,
        not on a per-trial adapter wrapper. Guards against a future edit
        dropping one from a backend and re-introducing the adapter shape."""
        for impl in implementations:
            assert callable(
                getattr(impl, method_name, None)
            ), f"{type(impl).__name__} is missing RuntimeBackend.{method_name}"


class TestInMemoryBackendSemantics:
    """The in-memory backend records calls on its
    :class:`RuntimeBackendCallLog` for tests to assert against, without
    touching the network.
    """

    def test_connect_records_arguments(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.connect(timeout=15.0, retry_interval=0.5)
        assert backend.call_log.connect_calls == [
            {"timeout": 15.0, "retry_interval": 0.5},
        ]

    def test_connect_defaults_are_recorded(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.connect()
        assert backend.call_log.connect_calls == [
            {"timeout": 30.0, "retry_interval": 1.0},
        ]

    def test_close_is_counted(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.close()
        backend.close()
        assert backend.call_log.close_calls == 2

    def test_health_check_returns_true_and_is_counted(self) -> None:
        backend = InMemoryRuntimeBackend()
        assert backend.health_check() is True
        assert backend.health_check() is True
        assert backend.call_log.health_check_calls == 2

    def test_cleanup_trial_returns_success_shape(self) -> None:
        backend = InMemoryRuntimeBackend()
        result = backend.cleanup_trial("airline_001:0")
        assert result == {"success": True, "error": None}

    def test_cleanup_trial_records_trial_ids_in_order(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.cleanup_trial("a:0")
        backend.cleanup_trial("b:0")
        backend.cleanup_trial("a:0")
        assert backend.call_log.cleanup_trial_calls == ["a:0", "b:0", "a:0"]

    def test_cleanup_trial_is_idempotent_on_unknown_trial(self) -> None:
        """Mirrors ``SharedStackRuntimeBackend``: cleaning a trial that was never
        registered must succeed so retry paths don't crash."""
        backend = InMemoryRuntimeBackend()
        result = backend.cleanup_trial("never_registered:0")
        assert result["success"] is True
        assert result["error"] is None

    @pytest.mark.parametrize(
        "method_call",
        [
            lambda b: b.register_trial("t:0", "{}"),
            lambda b: b.execute_tool("t:0", "noop", {}, call_id="c0"),
            lambda b: b.grade_trial("t:0"),
            lambda b: b.get_state("t:0"),
            lambda b: b.reset_trial("t:0"),
        ],
        ids=["register_trial", "execute_tool", "grade_trial", "get_state", "reset_trial"],
    )
    def test_per_trial_rpc_methods_raise_not_implemented(self, method_call) -> None:
        """The in-memory backend has no runner service to talk to; its
        RPC-method impls raise :class:`NotImplementedError` with a pointer
        at the right alternative."""
        backend = InMemoryRuntimeBackend()
        with pytest.raises(NotImplementedError, match="SharedStackRuntimeBackend or mock"):
            method_call(backend)

    def test_fresh_backend_has_empty_call_log(self) -> None:
        backend = InMemoryRuntimeBackend()
        assert backend.call_log == RuntimeBackendCallLog()


# ===========================================================================
# ADR-0010 provisioning contract
# ===========================================================================
#
# The tests below pin the provisioning surface added by ADR-0010:
# provision / await_ready / endpoints / teardown, plus EnvHandle shape and
# ProvisionError semantics. Per-backend enforcement of manifest safety
# declarations lives in that backend's own test module — this file tests
# the Protocol contract only.


def _make_task_description(manifest: EnvironmentManifest | None = None) -> TaskDescription:
    return make_task_description(
        task_id="task-1",
        name="probe",
        category="general",
        description="Contract-test task",
        system_prompt="You are a helpful assistant.",
        environment_manifest=manifest,
    )


def _make_trial_spec(
    trial_id: str = "task-1:0",
    manifest: EnvironmentManifest | None = None,
) -> TrialSpec:
    return TrialSpec(
        trial_id=trial_id,
        run_id="run_contract_test",
        task=_make_task_description(manifest),
        agent_model_config=ModelConfig(name="claude-sonnet-4-6", provider="anthropic"),
        env_endpoints=make_env_endpoints(),
    )


def _make_two_service_manifest() -> EnvironmentManifest:
    return EnvironmentManifest(compose_file=_FIXTURES / "safe_two_service.yaml")


def _make_one_service_manifest() -> EnvironmentManifest:
    return EnvironmentManifest(compose_file=_FIXTURES / "safe_one_service.yaml")


class TestProvisioningProtocolConformance:
    def test_shared_stack_runtime_backend_has_provisioning_methods(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="sentinel:50051")
        for method in ("provision", "await_ready", "endpoints", "teardown", "capture_service_logs"):
            assert callable(getattr(backend, method))

    def test_in_memory_backend_has_provisioning_methods(self) -> None:
        backend = InMemoryRuntimeBackend()
        for method in ("provision", "await_ready", "endpoints", "teardown", "capture_service_logs"):
            assert callable(getattr(backend, method))

    def test_per_trial_backend_has_capture_service_logs(self) -> None:
        from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend

        assert callable(PerTrialRuntimeBackend().capture_service_logs)


class TestInfrastructureSnapshotProtocolConformance:
    """Every backend implements :meth:`RuntimeBackend.get_infrastructure_snapshot`
    (the ``RunDisplayEvents`` seam's runtime hook). The Protocol widened in #416 —
    the orchestrator calls this on the hot path per trial, so a missing
    implementation on any backend is a run-corrupting hole.
    """

    def test_get_infrastructure_snapshot_is_present_on_all_backends(self) -> None:
        from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend

        implementations = [
            InMemoryRuntimeBackend(),
            SharedStackRuntimeBackend(runner_address="sentinel:50051"),
            PerTrialRuntimeBackend(),
        ]
        for impl in implementations:
            assert callable(
                getattr(impl, "get_infrastructure_snapshot", None)
            ), f"{type(impl).__name__} is missing RuntimeBackend.get_infrastructure_snapshot"

    def test_in_memory_returns_synthetic_single_container(self) -> None:
        """The in-memory backend has no real substrate; it returns a
        deterministic single-container snapshot keyed by trial id so
        display tests can assert against a known shape without docker.
        """
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec(trial_id="task-1:0"))

        snapshot = backend.get_infrastructure_snapshot(handle)

        assert snapshot == [
            {
                "name": "in-memory-runner-task-1:0",
                "service": "runner",
                "state": "running",
                "health": "healthy",
                "ports": {50051: 50051},
            }
        ]

    def test_shared_stack_built_in_mode_returns_empty(self) -> None:
        """In built-in-stack mode the shared-stack backend materialises no
        compose project of its own — the services widget already covers
        the built-in ``EngineStack``, so the infra snapshot is empty."""
        backend = SharedStackRuntimeBackend(runner_address="sentinel:50051")
        handle = backend.provision(_make_trial_spec())

        assert backend.get_infrastructure_snapshot(handle) == []


class TestEnvHandleShape:
    def test_handle_exposes_trial_id(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        assert handle.trial_id == "task-1:0"

    def test_handle_is_protocol_typed(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec())
        assert isinstance(handle, EnvHandle)


class TestProvisionError:
    def test_carries_trial_id_stage_reason(self) -> None:
        err = ProvisionError(trial_id="task-1:0", stage="provision", reason="port bind failed")
        assert err.trial_id == "task-1:0"
        assert err.stage == "provision"
        assert err.reason == "port bind failed"

    @pytest.mark.parametrize("stage", ["provision", "await_ready"])
    def test_stage_accepts_documented_literals(self, stage: str) -> None:
        err = ProvisionError(trial_id="t:0", stage=stage, reason="x")  # type: ignore[arg-type]
        assert err.stage == stage

    def test_error_is_raisable_and_catchable(self) -> None:
        with pytest.raises(ProvisionError) as exc:
            raise ProvisionError(trial_id="t:0", stage="provision", reason="x")
        assert exc.value.trial_id == "t:0"


class TestProvisionLifecycle:
    def test_returns_handle_with_matching_trial_id(self) -> None:
        backend = InMemoryRuntimeBackend()
        spec = _make_trial_spec(trial_id="task-1:0", manifest=_make_two_service_manifest())
        handle = backend.provision(spec)
        assert handle.trial_id == "task-1:0"

    def test_no_manifest_returns_handle(self) -> None:
        backend = InMemoryRuntimeBackend()
        spec = _make_trial_spec(manifest=None)
        handle = backend.provision(spec)
        assert handle.trial_id == spec.trial_id

    def test_partial_startup_raises_provision_error(self) -> None:
        backend = InMemoryRuntimeBackend(fail_provision_after_service="db")
        spec = _make_trial_spec(trial_id="task-1:0", manifest=_make_two_service_manifest())
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        assert exc.value.stage == "provision"
        assert exc.value.trial_id == "task-1:0"
        # Best-effort teardown ran before the raise.
        assert "task-1:0" in backend.call_log.torn_down_trials

    def test_partial_startup_only_fires_when_service_present(self) -> None:
        backend = InMemoryRuntimeBackend(fail_provision_after_service="db")
        handle = backend.provision(_make_trial_spec(manifest=_make_one_service_manifest()))
        assert handle.trial_id == "task-1:0"


class TestAwaitReadyLifecycle:
    def test_returns_when_all_probes_pass(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec())
        assert backend.await_ready(handle) is None

    def test_timeout_raises_provision_error(self) -> None:
        backend = InMemoryRuntimeBackend(await_ready_times_out=True)
        handle = backend.provision(_make_trial_spec(manifest=_make_two_service_manifest()))
        with pytest.raises(ProvisionError) as exc:
            backend.await_ready(handle)
        assert exc.value.stage == "await_ready"

    def test_handle_valid_after_await_ready_failure(self) -> None:
        backend = InMemoryRuntimeBackend(await_ready_times_out=True)
        handle = backend.provision(_make_trial_spec(manifest=_make_two_service_manifest()))
        with pytest.raises(ProvisionError):
            backend.await_ready(handle)
        backend.teardown(handle)  # must not raise
        assert handle.trial_id in backend.call_log.torn_down_trials


class TestEndpointsResolution:
    def test_returns_env_endpoints(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec())
        endpoints = backend.endpoints(handle)
        assert isinstance(endpoints, EnvEndpoints)
        assert endpoints.runner_url

    def test_does_not_block(self) -> None:
        backend = InMemoryRuntimeBackend(await_ready_times_out=True)
        handle = backend.provision(_make_trial_spec())
        _ = backend.endpoints(handle)  # must not raise

    def test_per_trial_endpoints_differ(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle_a = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        handle_b = backend.provision(_make_trial_spec(trial_id="task-1:1"))
        ep_a = backend.endpoints(handle_a)
        ep_b = backend.endpoints(handle_b)
        assert ep_a.runner_url != ep_b.runner_url


class TestTeardownLifecycle:
    def test_teardown_records_trial(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        backend.teardown(handle)
        assert "task-1:0" in backend.call_log.torn_down_trials

    def test_teardown_is_idempotent(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec())
        backend.teardown(handle)
        backend.teardown(handle)  # no exception


class TestSharedStackRuntimeBackendCompat:
    def test_provision_returns_handle_with_trial_id(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="localhost:50051")
        spec = _make_trial_spec(trial_id="task-1:0")
        handle = backend.provision(spec)
        assert handle.trial_id == "task-1:0"
        assert isinstance(handle, EnvHandle)

    def test_endpoints_returns_shared_run_wide_urls(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="localhost:50051")
        handle_a = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        handle_b = backend.provision(_make_trial_spec(trial_id="task-1:1"))
        # Shared-stack semantics: both handles resolve to the same endpoints.
        assert backend.endpoints(handle_a) == backend.endpoints(handle_b)

    def test_teardown_is_no_op(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="localhost:50051")
        handle = backend.provision(_make_trial_spec())
        backend.teardown(handle)  # no exception
        backend.teardown(handle)  # idempotent

    def test_await_ready_is_no_op(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="localhost:50051")
        handle = backend.provision(_make_trial_spec())
        assert backend.await_ready(handle) is None


class TestInMemoryProvisioningCallLog:
    def test_records_provision_and_teardown(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        backend.teardown(handle)
        assert backend.call_log.provisioned_trials == ["task-1:0"]
        assert backend.call_log.torn_down_trials == ["task-1:0"]

    def test_call_log_preserves_ordering(self) -> None:
        backend = InMemoryRuntimeBackend()
        h_a = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        h_b = backend.provision(_make_trial_spec(trial_id="task-1:1"))
        backend.teardown(h_a)
        backend.teardown(h_b)
        assert backend.call_log.provisioned_trials == ["task-1:0", "task-1:1"]
        assert backend.call_log.torn_down_trials == ["task-1:0", "task-1:1"]

    def test_records_await_ready_and_endpoints_calls(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        backend.await_ready(handle)
        backend.endpoints(handle)
        assert backend.call_log.await_ready_calls == ["task-1:0"]
        assert backend.call_log.endpoints_calls == ["task-1:0"]

    def test_configurable_provision_failure(self) -> None:
        backend = InMemoryRuntimeBackend(fail_provision_after_service="db")
        with pytest.raises(ProvisionError):
            backend.provision(_make_trial_spec(manifest=_make_two_service_manifest()))


class TestCaptureServiceLogsContract:
    """``capture_service_logs`` is a Protocol method on every backend. The
    in-memory fixture records the ``(trial_id, capture_worthy)`` pair for tests
    to assert against; the shared-stack backend is a documented no-op. Real
    per-service ``.log`` capture on the per-trial backend is locked by the
    Docker integration test."""

    def test_in_memory_records_call_and_returns_empty(self) -> None:
        backend = InMemoryRuntimeBackend()
        handle = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        assert backend.capture_service_logs(handle, capture_worthy=True) == {}
        assert backend.capture_service_logs(handle, capture_worthy=False) == {}
        assert backend.call_log.capture_service_logs_calls == [
            ("task-1:0", True),
            ("task-1:0", False),
        ]

    def test_shared_stack_is_no_op(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="localhost:50051")
        handle = backend.provision(_make_trial_spec(trial_id="task-1:0"))
        assert backend.capture_service_logs(handle, capture_worthy=True) == {}
        assert backend.capture_service_logs(handle, capture_worthy=False) == {}
