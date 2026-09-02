"""Canonical tests for :class:`PerTrialRuntimeBackend`.

``PerTrialRuntimeBackend`` is a thin preset over
:class:`SharedStackRuntimeBackend` in per-trial mode, so every method
delegates to a composer stitched onto the internal backend. These tests
inject a :class:`DefaultSubstrateComposer` with a fake
``docker_compose_factory`` and a fake ``runner_client_factory`` so the
whole provisioning + RPC delegation chain runs without a Docker daemon
or a real gRPC server. Real-daemon coverage lives in
``tests/integration/docker/test_per_trial_runtime_backend_integration.py``,
gated by ``@pytest.mark.docker``.

Assertions on the per-trial substrate read from the composer-produced
:class:`ComposedEnvHandle` and the underlying
:class:`_DockerComposeStackHandle` — the composer owns compose lifecycle
and per-trial temp dirs.

Materialisation transforms run for real, so every ``provision()`` here
writes a compose file carrying the credential payload; the
package-level ``_pin_fake_secrets`` pins the manager supplying it.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.canonical._factories import make_task_description
from tolokaforge.core.composition_runtime import ComposedEnvHandle
from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer
from tolokaforge.core.docker_compose_materialiser import (
    DockerComposeMaterialiser,
    _DockerComposeStackHandle,
)
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.project_loader import _synthesise_composition_plan
from tolokaforge.core.runtime import EnvHandle, ProvisionError, RuntimeBackend
from tolokaforge.core.service_readiness import (
    InMemoryServiceReadinessProbe,
    ResolvedEndpoint,
    ServiceReadinessProbe,
)
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import ReadinessSpec, ResetSpec, ServiceSpec
from tolokaforge.secrets import CONTAINER_SECRETS_ENV_VAR

pytestmark = pytest.mark.canonical


_FIXTURES = Path(__file__).parent / "fixtures" / "environment_manifest"


def _ready_loader(kind: str) -> Callable[[], ServiceReadinessProbe]:
    """Readiness-probe loader seam yielding an always-ready in-memory probe, so
    provision's host-side readiness gate passes without a live listener."""
    del kind
    return lambda: InMemoryServiceReadinessProbe(ok=True)


class _RecordingLoader:
    """Readiness-probe loader seam that records the ``kind`` requested per
    service and hands back an always-ready probe whose own call log captures
    the endpoint it was probed against."""

    def __init__(self) -> None:
        self.kinds: list[str] = []
        self.probes: list[InMemoryServiceReadinessProbe] = []

    def __call__(self, kind: str) -> Callable[[], ServiceReadinessProbe]:
        self.kinds.append(kind)
        probe = InMemoryServiceReadinessProbe(ok=True)
        self.probes.append(probe)
        return lambda: probe


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
            "db-service": {8000: 58000},
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

    def get_containers(self) -> list[Any]:
        # Empty by default — tests that need router construction populate
        # a purpose-built fake with declared containers.
        return []


class _FakeContainer:
    def __init__(self, ports: dict[int, int]) -> None:
        self.Publishers = [
            _FakePublisher(TargetPort=cp, PublishedPort=hp) for cp, hp in ports.items()
        ]


class _FakePublisher:
    def __init__(self, TargetPort: int, PublishedPort: int) -> None:  # noqa: N803
        self.TargetPort = TargetPort
        self.PublishedPort = PublishedPort


class _FakeRunnerClient:
    """Stand-in for ``GrpcRunnerClient`` with a recording call log."""

    def __init__(self, runner_address: str, events: Any = None) -> None:
        del events
        self.runner_address = runner_address
        self.connected = False
        self.closed = False
        self.connect_calls = 0
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        del timeout, retry_interval
        self.connect_calls += 1
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
        executor: str = "agent",
        *,
        call_id: str,
    ) -> Any:
        self.calls.append(
            (
                "execute_tool",
                (),
                {
                    "trial_id": trial_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "executor": executor,
                    "call_id": call_id,
                },
            )
        )
        return {"success": True}

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._record(
            "grade_trial",
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
            termination_reason=termination_reason,
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


def _make_composer(
    *,
    readiness_probe_loader: Callable[[str], Callable[[], ServiceReadinessProbe]] = _ready_loader,
    compose_factory: Callable[..., _FakeCompose] = _FakeCompose,
    runner_client_factory: Callable[[str, Any], _FakeRunnerClient] = _FakeRunnerClient,
) -> DefaultSubstrateComposer:
    """Build a composer wired to the test fakes.

    The materialiser writes real files, applies real transforms, and
    hands them to ``compose_factory`` — the same seam production uses
    but pointed at ``_FakeCompose`` instead of ``DockerCompose``. The
    runner client factory returns ``_FakeRunnerClient`` so RPCs record
    into a call log.
    """
    return DefaultSubstrateComposer(
        materialiser=DockerComposeMaterialiser(docker_compose_factory=compose_factory),
        runner_client_factory=runner_client_factory,
        readiness_probe_loader=readiness_probe_loader,
    )


@pytest.fixture
def backend() -> PerTrialRuntimeBackend:
    """PerTrialRuntimeBackend with a composer wired to fake compose +
    fake runner client. Every test in this file uses this fixture."""
    return PerTrialRuntimeBackend(composer=_make_composer())


def _make_trial_spec(
    trial_id: str = "task-1:0",
    compose_file: Path | None = None,
    services: dict[str, ServiceSpec] | None = None,
    manifest: EnvironmentManifest | None = None,
) -> TrialSpec:
    if manifest is not None:
        pass
    elif compose_file is None:
        manifest = None
    elif services is None:
        manifest = EnvironmentManifest(compose_file=compose_file)
    else:
        manifest = EnvironmentManifest(compose_file=compose_file, services=services)
    if manifest is not None:
        _synthesise_composition_plan(manifest, {})
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


def _stack_handle(env_handle: EnvHandle) -> _DockerComposeStackHandle:
    """Return the first (and only) per-trial stack handle from a composer-produced env handle."""
    assert isinstance(env_handle, ComposedEnvHandle)
    stack_handle = env_handle.trial_stack_handles[0]
    assert isinstance(stack_handle, _DockerComposeStackHandle)
    return stack_handle


def _fake_runner_client(env_handle: EnvHandle) -> _FakeRunnerClient:
    assert isinstance(env_handle, ComposedEnvHandle)
    assert isinstance(env_handle.trial_runner_client, _FakeRunnerClient)
    return env_handle.trial_runner_client


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_per_trial_runtime_backend_satisfies_runtime_backend(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        assert isinstance(backend, RuntimeBackend)

    def test_all_provisioning_methods_are_present(self, backend: PerTrialRuntimeBackend) -> None:
        for method in ("provision", "await_ready", "endpoints", "teardown"):
            assert callable(getattr(backend, method))

    def test_all_per_trial_rpc_methods_are_present(self, backend: PerTrialRuntimeBackend) -> None:
        for method in (
            "register_trial",
            "execute_tool",
            "grade_trial",
            "get_state",
            "reset_trial",
            "cleanup_trial",
        ):
            assert callable(getattr(backend, method))


# ---------------------------------------------------------------------------
# Run-level lifecycle
# ---------------------------------------------------------------------------


class TestRunLevelLifecycle:
    def test_connect_is_a_no_op(self, backend: PerTrialRuntimeBackend) -> None:
        # Must not raise; no side effects tests can check beyond that.
        backend.connect(timeout=5.0, retry_interval=0.1)

    def test_health_check_returns_true_with_no_trials(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        assert backend.health_check() is True

    def test_close_drops_all_provisioned_trials(self, backend: PerTrialRuntimeBackend) -> None:
        handle = backend.provision(
            _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        )
        assert handle.trial_id in backend._delegate._env_handles
        backend.close()
        assert backend._delegate._env_handles == {}


# ---------------------------------------------------------------------------
# Per-trial provisioning
# ---------------------------------------------------------------------------


class TestProvision:
    def test_returns_handle_with_matching_trial_id(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(
            trial_id="task-1:0", compose_file=_FIXTURES / "safe_two_service.yaml"
        )
        handle = backend.provision(spec)
        assert isinstance(handle, EnvHandle)
        assert handle.trial_id == "task-1:0"

    def test_populates_per_trial_client_before_connect(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        client = _fake_runner_client(handle)
        # Connect is deferred to first RPC use — the client is built but
        # not yet connected.
        assert client.connected is False
        assert spec.trial_id not in backend._delegate._connected_trials

    def test_first_rpc_call_triggers_connect(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        backend.register_trial(trial_id=spec.trial_id, trial_spec_json="{}")
        client = _fake_runner_client(handle)
        assert client.connected is True
        assert spec.trial_id in backend._delegate._connected_trials

    def test_repeated_rpc_calls_do_not_reconnect(self) -> None:
        """First RPC connects; subsequent RPCs reuse the connected client
        without re-running the connect health-check loop."""
        backend = PerTrialRuntimeBackend(composer=_make_composer())
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        backend.register_trial(trial_id=spec.trial_id, trial_spec_json="{}")
        backend.execute_tool(trial_id=spec.trial_id, tool_name="x", arguments={}, call_id="c0")
        backend.get_state(trial_id=spec.trial_id)
        client = _fake_runner_client(handle)
        assert client.connect_calls == 1

    def test_starts_the_compose_stack(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        stack = _stack_handle(handle)
        assert stack.compose.started is True

    def test_creates_per_trial_temp_directory(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        stack = _stack_handle(handle)
        assert stack.temp_dir.exists()
        assert stack.temp_dir.is_dir()

    def test_the_materialised_compose_file_gives_only_the_runner_the_payload(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        """Asserted on the file ``docker compose`` reads, not on a spy: the
        runner service of the materialised stack carries the credential entry
        and no sibling service does."""
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        stack = _stack_handle(handle)

        services = yaml.safe_load((stack.temp_dir / "safe_two_service.yaml").read_text())[
            "services"
        ]
        try:
            assert CONTAINER_SECRETS_ENV_VAR in services["default"]["environment"]
            assert CONTAINER_SECRETS_ENV_VAR not in yaml.safe_dump(services["db"])
        finally:
            shutil.rmtree(stack.temp_dir, ignore_errors=True)

    def test_missing_manifest_raises_provision_error(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=None)  # manifest = None
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        assert exc.value.stage == "provision"
        assert exc.value.trial_id == spec.trial_id

    def test_readiness_gate_failure_raises_provision_error_with_diagnostic(
        self,
    ) -> None:
        """A not-ready gated endpoint fails provisioning at the readiness gate
        with ``stage='provision'`` and a populated :class:`DiagnosticPayload`
        naming the probed service, kind, resolved endpoint, and probe outcome."""

        def _failing_loader(kind: str) -> Callable[[], ServiceReadinessProbe]:
            del kind
            return lambda: InMemoryServiceReadinessProbe(ok=False, fail_detail="channel not ready")

        backend = PerTrialRuntimeBackend(
            composer=_make_composer(readiness_probe_loader=_failing_loader)
        )
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        err = exc.value
        assert err.stage == "provision"
        assert err.trial_id == spec.trial_id
        assert "channel not ready" in err.reason
        # The composer-side readiness gate raises without a docker-inspection
        # diagnostic payload — the failing-probe details in the reason cover
        # the same signal.
        assert err.diagnostic is None
        # No orphan handle is cached: the gate runs before the client is built.
        assert spec.trial_id not in backend._delegate._env_handles

    def test_runner_is_probed_with_grpc_at_resolved_host_port(self) -> None:
        """The runner substrate is always probed with the ``grpc`` kind at its
        resolved host endpoint, regardless of any per-service readiness spec."""
        loader = _RecordingLoader()
        backend = PerTrialRuntimeBackend(composer=_make_composer(readiness_probe_loader=loader))
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        backend.provision(spec)
        assert loader.kinds == ["grpc"]
        assert loader.probes[0].call_log.calls[0].endpoint == ResolvedEndpoint(
            host="127.0.0.1", port=50100
        )

    def test_declared_readiness_service_probed_by_its_kind(self) -> None:
        """A service that declares a ``readiness`` spec is probed by that spec's
        kind at its first published host port, alongside the runner's grpc probe."""

        class _WithDbCompose(_FakeCompose):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.exposed_services = {"default": {50051: 50100}, "db": {5432: 55432}}

        loader = _RecordingLoader()
        backend = PerTrialRuntimeBackend(
            composer=_make_composer(
                readiness_probe_loader=loader,
                compose_factory=_WithDbCompose,
            )
        )
        manifest = EnvironmentManifest(
            compose_file=_FIXTURES / "safe_two_service.yaml",
            services={
                "db": ServiceSpec(isolation="ephemeral", readiness=ReadinessSpec(kind="tcp"))
            },
        )
        backend.provision(_make_trial_spec(manifest=manifest))
        assert loader.kinds == ["grpc", "tcp"]
        assert loader.probes[1].call_log.calls[0].endpoint == ResolvedEndpoint(
            host="127.0.0.1", port=55432
        )

    def test_declared_readiness_service_without_published_port_fails(self) -> None:
        """A declared-readiness service that exposes no resolvable published
        port cannot have its contract honoured — provisioning fails fast rather
        than silently skipping the probe."""
        backend = PerTrialRuntimeBackend(composer=_make_composer())
        # ``_FakeCompose`` exposes "default" + "db-service" but not "db"; the manifest
        # declares readiness on "db", which resolves to no host port.
        manifest = EnvironmentManifest(
            compose_file=_FIXTURES / "safe_two_service.yaml",
            services={
                "db": ServiceSpec(isolation="ephemeral", readiness=ReadinessSpec(kind="tcp"))
            },
        )
        with pytest.raises(ProvisionError) as exc:
            backend.provision(_make_trial_spec(manifest=manifest))
        assert exc.value.stage == "provision"
        assert "no resolvable published port" in exc.value.reason
        assert exc.value.diagnostic is None

    def test_compose_start_failure_raises_provision_error(self) -> None:
        class _FailingCompose(_FakeCompose):
            def start(self) -> None:
                raise RuntimeError("simulated compose up failure")

        backend = PerTrialRuntimeBackend(composer=_make_composer(compose_factory=_FailingCompose))
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        assert exc.value.stage == "provision"
        assert "compose up failed" in exc.value.reason
        # No orphan handle cached after failure.
        assert spec.trial_id not in backend._delegate._env_handles

    def test_concurrent_trials_get_independent_temp_dirs(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        spec_a = _make_trial_spec(
            trial_id="task-1:0", compose_file=_FIXTURES / "safe_two_service.yaml"
        )
        spec_b = _make_trial_spec(
            trial_id="task-1:1", compose_file=_FIXTURES / "safe_two_service.yaml"
        )
        h_a = backend.provision(spec_a)
        h_b = backend.provision(spec_b)
        assert _stack_handle(h_a).temp_dir != _stack_handle(h_b).temp_dir


# ---------------------------------------------------------------------------
# Per-trial compose ``.env`` — stack_inputs materialisation
# ---------------------------------------------------------------------------


class TestComposeEnvFile:
    """The manifest's ``stack_inputs`` becomes a per-trial ``.env`` next to the
    copied compose file so ``docker compose up`` interpolates ``${var}`` slots.
    The engine appends its own reserved block last (currently just
    ``TOLOKAFORGE_TRIAL_SLUG``); any task-authored ``.env`` in the source
    context is preserved above it. Keys under the reserved prefix in
    ``stack_inputs`` are rejected before provision starts so the error does
    not read as ``docker compose up failed``.
    """

    def test_env_file_written_with_reserved_block_last(
        self, backend: PerTrialRuntimeBackend, tmp_path: Path
    ) -> None:
        # Copy the fixture into a temp source dir and add a task-authored
        # .env: the copy step must preserve it, and the reserved block must
        # win by appearing last.
        src = tmp_path / "src"
        src.mkdir()
        compose = src / "compose.yaml"
        compose.write_text((_FIXTURES / "safe_two_service.yaml").read_text())
        (src / ".env").write_text("TASK_AUTHORED=preserved\n")
        manifest = EnvironmentManifest(
            compose_file=compose,
            stack_inputs={"IMAGE_TAG": "v1.2.3", "PORT": "5432"},
        )
        spec = _make_trial_spec(trial_id="task-1:0", manifest=manifest)
        handle = backend.provision(spec)
        stack = _stack_handle(handle)
        env_content = (stack.temp_dir / ".env").read_text()
        assert "TASK_AUTHORED=preserved" in env_content
        assert "IMAGE_TAG=v1.2.3" in env_content
        assert "PORT=5432" in env_content
        assert "TOLOKAFORGE_TRIAL_SLUG=task-1_0" in env_content
        # Reserved block last: no task/manifest line may appear below it.
        lines = [line for line in env_content.splitlines() if line]
        assert lines[-1] == "TOLOKAFORGE_TRIAL_SLUG=task-1_0"

    def test_empty_stack_inputs_still_writes_reserved_block(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        stack = _stack_handle(handle)
        env_content = (stack.temp_dir / ".env").read_text()
        assert "TOLOKAFORGE_TRIAL_SLUG=task-1_0" in env_content

    def test_reserved_prefix_key_raises_before_compose_up(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        """A ``stack_inputs`` key under the reserved prefix must fail the
        provision *before* the compose lifecycle's ``try`` block, so the
        reason names the reserved-prefix rule and the offending key —
        not ``docker compose up failed``."""
        manifest = EnvironmentManifest(
            compose_file=_FIXTURES / "safe_two_service.yaml",
            stack_inputs={"TOLOKAFORGE_TRIAL_SLUG": "clobber"},
        )
        spec = _make_trial_spec(trial_id="task-1:0", manifest=manifest)
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        err = exc.value
        assert err.stage == "provision"
        assert err.trial_id == spec.trial_id
        assert "TOLOKAFORGE_TRIAL_SLUG" in err.reason
        assert "reserved" in err.reason
        assert "compose up failed" not in err.reason

    def test_terminal_bench_provider_env_reaches_the_env_file(
        self,
        tmp_path: Path,
    ) -> None:
        """The terminal-bench adapter's ``agent_provider_env`` end-to-end.

        A harness CLI reads its credentials from the container's environment,
        which compose interpolates from this ``.env`` at up-time. Driven through
        the real adapter rather than a hand-built manifest so the whole chain is
        covered — adapter param, ``StackPatch.inputs``, ``project_loader.resolve``,
        ``EnvironmentManifest.stack_inputs``, ``write_compose_env_file``.
        """
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        class _TbenchCompose(_FakeCompose):
            """The synthesised compose names its runner service ``runner``."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.exposed_services["runner"] = {50051: 50100}

        backend = PerTrialRuntimeBackend(composer=_make_composer(compose_factory=_TbenchCompose))

        tasks_dir = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(tasks_dir),
                "staging_root": str(tmp_path / "staging"),
                "agent_harness": "claude-code",
                "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
                "agent_provider_env": {
                    "ANTHROPIC_API_KEY": "sk-canon",
                    "ANTHROPIC_BASE_URL": "https://proxy.example",
                },
            }
        )
        spec = _make_trial_spec(
            trial_id="echo-hello:0",
            manifest=adapter.to_task_description("echo-hello").environment_manifest,
        )
        handle = backend.provision(spec)
        stack = _stack_handle(handle)
        lines = [line for line in (stack.temp_dir / ".env").read_text().splitlines() if line]
        assert "TBENCH_PROVIDER_ANTHROPIC_API_KEY=sk-canon" in lines
        assert "TBENCH_PROVIDER_ANTHROPIC_BASE_URL=https://proxy.example" in lines
        assert lines[-1] == "TOLOKAFORGE_TRIAL_SLUG=echo-hello_0"


# ---------------------------------------------------------------------------
# Reset-recipe failure attribution — the reset seam owns ``stage="reset_recipe"``
# ---------------------------------------------------------------------------


class TestResetRecipeFailureAttribution:
    """A reset-recipe failure is distinct from a compose-up failure. The
    compose stack came up fine, so ``provision`` must attribute the failure
    to ``stage="reset_recipe"`` (never ``"provision"``) and never leave a
    handle cached."""

    def test_missing_seed_raises_reset_recipe_stage(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(
            compose_file=_FIXTURES / "safe_two_service.yaml",
            services={"db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline"))},
        )
        # backend.seeds is empty — the named seed is absent from the registry.
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        assert exc.value.stage == "reset_recipe"
        assert "baseline" in exc.value.reason
        assert "compose up failed" not in exc.value.reason
        assert spec.trial_id not in backend._delegate._env_handles

    def test_reset_service_without_seed_pointer_raises_reset_recipe_stage(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        # The ServiceSpec invariant forbids isolation='reset' with reset=None,
        # so reaching the backend's defensive guard means clearing the pointer
        # after construction (schema validation would reject it upstream).
        spec = _make_trial_spec(
            compose_file=_FIXTURES / "safe_two_service.yaml",
            services={"db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline"))},
        )
        assert spec.task.environment_manifest is not None
        spec.task.environment_manifest.services["db"].reset = None
        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        assert exc.value.stage == "reset_recipe"
        assert spec.trial_id not in backend._delegate._env_handles


# ---------------------------------------------------------------------------
# await_ready — no-op contract
# ---------------------------------------------------------------------------


class TestAwaitReady:
    def test_no_op_after_provision(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        assert backend.await_ready(handle) is None


# ---------------------------------------------------------------------------
# endpoints — convention-based resolution
# ---------------------------------------------------------------------------


class TestEndpoints:
    def test_returns_env_endpoints(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        endpoints = backend.endpoints(handle)
        assert isinstance(endpoints, EnvEndpoints)
        assert endpoints.runner_url == "http://127.0.0.1:50100"
        assert endpoints.db_url == "http://127.0.0.1:58000"
        assert endpoints.rag_url is None

    def test_missing_db_service_yields_db_url_none(self) -> None:
        """``db_url`` is best-effort — a task compose file that omits
        ``db-service:8000`` yields ``EnvEndpoints(db_url=None, ...)``
        and provisioning proceeds. The runner-side ``DBServiceClient``
        binds to ``DB_SERVICE_URL`` from its container env, so a missing
        ``db_url`` is not a provisioning failure."""

        class _NoDbCompose(_FakeCompose):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.exposed_services = {"default": {50051: 50100}}

        backend = PerTrialRuntimeBackend(composer=_make_composer(compose_factory=_NoDbCompose))
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_one_service.yaml")
        handle = backend.provision(spec)
        endpoints = backend.endpoints(handle)
        assert endpoints.db_url is None
        assert endpoints.runner_url == "http://127.0.0.1:50100"
        # Handle was cached — no lingering-failure state.
        assert spec.trial_id in backend._delegate._env_handles

    def test_runner_port_and_db_service_overrides_flow_to_endpoints(self) -> None:
        """``stack`` endpoint overrides on the manifest reach resolution:
        a non-default ``runner_port`` is the port resolved for the runner
        service, and a non-default ``db_service`` / ``db_port`` name the
        service+port resolved for ``db_url`` (not the ``db-service:8000``
        convention)."""

        class _OverrideCompose(_FakeCompose):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.exposed_services = {
                    "default": {9000: 50100},
                    "db": {5433: 58001},
                }

        backend = PerTrialRuntimeBackend(composer=_make_composer(compose_factory=_OverrideCompose))
        manifest = EnvironmentManifest(
            compose_file=_FIXTURES / "safe_two_service.yaml",
            runner_port=9000,
            db_service="db",
            db_port=5433,
        )
        spec = _make_trial_spec(manifest=manifest)
        handle = backend.provision(spec)
        endpoints = backend.endpoints(handle)
        assert endpoints.runner_url == "http://127.0.0.1:50100"
        assert endpoints.db_url == "http://127.0.0.1:58001"

    def test_endpoints_rejects_foreign_handle(self, backend: PerTrialRuntimeBackend) -> None:
        class _NotAHandle:
            trial_id = "x"

        with pytest.raises(TypeError):
            backend.endpoints(_NotAHandle())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# teardown — idempotent, releases resources
# ---------------------------------------------------------------------------


class TestTeardown:
    def test_stops_compose_and_removes_temp_dir(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        stack = _stack_handle(handle)
        temp_dir = stack.temp_dir
        assert temp_dir.exists()
        backend.teardown(handle)
        assert stack.compose.stopped is True
        assert not temp_dir.exists()

    def test_closes_the_trial_runner_client_on_teardown(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        """The composer's teardown_trial closes the trial-owned runner
        client whether or not it was ever connected — a client with an
        idempotent close() has nothing to lose."""
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        client = _fake_runner_client(handle)
        backend.teardown(handle)
        assert client.closed is True
        assert spec.trial_id not in backend._delegate._env_handles
        assert spec.trial_id not in backend._delegate._connected_trials

    def test_teardown_is_idempotent(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        backend.teardown(handle)
        backend.teardown(handle)  # must not raise

    def test_teardown_rejects_foreign_handle(self, backend: PerTrialRuntimeBackend) -> None:
        class _NotAHandle:
            trial_id = "x"

        # Composer path requires a ComposedEnvHandle; a foreign shape is
        # explicit-rejected so a caller mis-routing handles surfaces.
        with pytest.raises(TypeError):
            backend.teardown(_NotAHandle())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Per-trial RPC delegation (ADR-0013)
# ---------------------------------------------------------------------------


class TestPerTrialRpcDelegation:
    def test_register_trial_delegates_to_trial_client(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        result = backend.register_trial(trial_id=spec.trial_id, trial_spec_json="{}")
        assert result == {"success": True, "error": None}
        client = _fake_runner_client(handle)
        assert client.calls[-1][0] == "register_trial"

    def test_execute_tool_delegates(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        backend.execute_tool(
            trial_id=spec.trial_id, tool_name="echo", arguments={"x": 1}, call_id="toolu_A"
        )
        client = _fake_runner_client(handle)
        assert client.calls[-1][0] == "execute_tool"
        assert client.calls[-1][2]["arguments"] == {"x": 1}
        assert client.calls[-1][2]["call_id"] == "toolu_A"

    def test_grade_trial_delegates(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        backend.grade_trial(trial_id=spec.trial_id, termination_reason="agent_done")
        client = _fake_runner_client(handle)
        assert client.calls[-1][0] == "grade_trial"
        assert client.calls[-1][2]["termination_reason"] == "agent_done"

    def test_get_state_delegates(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        backend.get_state(trial_id=spec.trial_id)
        client = _fake_runner_client(handle)
        assert client.calls[-1][0] == "get_state"

    def test_reset_trial_delegates(self, backend: PerTrialRuntimeBackend) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        handle = backend.provision(spec)
        backend.reset_trial(trial_id=spec.trial_id, execute_init_actions=True)
        client = _fake_runner_client(handle)
        assert client.calls[-1][0] == "reset_trial"
        assert client.calls[-1][2]["execute_init_actions"] is True

    def test_cleanup_trial_delegates_when_handle_present(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
        backend.provision(spec)
        result = backend.cleanup_trial(spec.trial_id)
        assert result == {"success": True, "error": None}

    def test_cleanup_trial_is_idempotent_when_no_handle(
        self, backend: PerTrialRuntimeBackend
    ) -> None:
        # No provision → no env handle. Cleanup must report success rather
        # than raising — the orchestrator's retry path calls this before
        # provision has run for a given trial.
        result = backend.cleanup_trial("never-provisioned:0")
        assert result == {"success": True, "error": None}

    def test_rpc_before_provision_raises_clear_error(self, backend: PerTrialRuntimeBackend) -> None:
        with pytest.raises(KeyError):
            backend.register_trial(trial_id="never-provisioned:0", trial_spec_json="{}")
