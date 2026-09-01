"""Unit tests for :class:`DockerComposeMaterialiser`.

The docker-daemon-touching lifecycle (``.start()`` / ``.stop()``) is
covered by the integration suite; here every test injects a hand-rolled
:class:`_StubDockerCompose` via
:attr:`DockerComposeMaterialiser.docker_compose_factory` so the sequence
of on-disk transforms and driver-side calls is observable in-process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.compose_materialisation import (
    CONTAINER_SECRETS_ENV_VAR,
    DOCKER_SOCKET_PATH,
    NETPOLICY_EDGE_NETWORK,
    NETPOLICY_INTERNAL_NETWORK,
    TOLOKAFORGE_TRIAL_SLUG_ENV,
)
from tolokaforge.core.composition_runtime import (
    MaterialiseContext,
    MaterialiseLogCapture,
    WriteComposeEnv,
)
from tolokaforge.core.docker_compose_materialiser import (
    DockerComposeMaterialiser,
    _DockerComposeStackHandle,
)
from tolokaforge.core.run_display_events import _NULL_EVENTS
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import NetworkPolicy
from tolokaforge.runner.models import StackDecl, StackScope

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubComposeContainer:
    """Minimal ``ComposeContainer`` stand-in for ``.get_containers()``."""

    def __init__(self, *, ID: str, Name: str, Service: str) -> None:  # noqa: N803
        self.ID = ID
        self.Name = Name
        self.Service = Service


class _StubDockerCompose:
    """In-process stand-in for ``testcontainers.compose.DockerCompose``.

    Records the constructor kwargs and the ``start()`` / ``stop()`` /
    ``get_containers()`` invocation order so a test can assert the
    materialiser drives the compose stack in the expected sequence.
    ``start_should_raise`` triggers the failure path without a real
    docker daemon.
    """

    calls: list[tuple[str, tuple[Any, ...]]]

    def __init__(
        self,
        *,
        context: str,
        compose_file_name: str,
        pull: bool,
        build: bool,
        wait: bool,
        containers: list[_StubComposeContainer] | None = None,
        start_should_raise: BaseException | None = None,
    ) -> None:
        self.context = context
        self.compose_file_name = compose_file_name
        self.pull = pull
        self.build = build
        self.wait = wait
        self._containers = containers or []
        self._start_should_raise = start_should_raise
        self.calls = [
            (
                "__init__",
                (context, compose_file_name, pull, build, wait),
            )
        ]

    def start(self) -> None:
        self.calls.append(("start", ()))
        if self._start_should_raise is not None:
            raise self._start_should_raise

    def stop(self, down: bool = True) -> None:
        self.calls.append(("stop", (down,)))

    def get_containers(self) -> list[_StubComposeContainer]:
        self.calls.append(("get_containers", ()))
        return list(self._containers)


class _FactoryRecorder:
    """Records every ``docker_compose_factory`` invocation.

    ``last`` returns the most-recent stub — the one the materialise call
    under test drove. ``containers`` seeds the compose stack's snapshot
    for the log-router attach loop.
    """

    def __init__(
        self,
        *,
        containers: list[_StubComposeContainer] | None = None,
        start_should_raise: BaseException | None = None,
    ) -> None:
        self._containers = containers or []
        self._start_should_raise = start_should_raise
        self.instances: list[_StubDockerCompose] = []

    def __call__(self, **kwargs: Any) -> _StubDockerCompose:
        stub = _StubDockerCompose(
            containers=self._containers,
            start_should_raise=self._start_should_raise,
            **kwargs,
        )
        self.instances.append(stub)
        return stub

    @property
    def last(self) -> _StubDockerCompose:
        return self.instances[-1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


COMPOSE_SOURCE = (
    "services:\n"
    "  runner:\n"
    "    image: tolokaforge-runner:local\n"
    "    ports:\n"
    '      - "50051"\n'
    "  db-service:\n"
    "    image: tolokaforge-db-service:local\n"
    "    ports:\n"
    '      - "8000"\n'
)


def _write_compose(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    compose = tmp_path / "environment.compose.yaml"
    compose.write_text(COMPOSE_SOURCE)
    return compose


def _make_ctx(
    *,
    scope_key: str = "run-a",
    stack_id: str = "default",
    network_policy: NetworkPolicy = NetworkPolicy.NO_INTERNET,
    mount_docker_socket: bool = False,
    log_capture: MaterialiseLogCapture | None = None,
    write_compose_env: WriteComposeEnv | None = None,
    component_id_prefix: str = "engine",
) -> MaterialiseContext:
    return MaterialiseContext(
        scope_key=scope_key,
        stack_id=stack_id,
        network_policy=network_policy,
        limited_internet_allowlist=(),
        restricted_services=frozenset(),
        mount_docker_socket=mount_docker_socket,
        log_capture=log_capture,
        write_compose_env=write_compose_env,
        events=_NULL_EVENTS,
        component_id_prefix=component_id_prefix,
    )


def _make_decl(
    compose_file: Path,
    *,
    stack_id: str = "default",
    stack_scope: StackScope = "run",
    runner_service: str | None = "runner",
) -> StackDecl:
    return StackDecl(
        stack_id=stack_id,
        compose_file=compose_file,
        stack_scope=stack_scope,
        runner_service=runner_service,
    )


# ---------------------------------------------------------------------------
# materialise: on-disk transforms
# ---------------------------------------------------------------------------


class TestMaterialiseTransforms:
    def test_writes_network_policy_transformed_compose(self, tmp_path: Path) -> None:
        """``no_internet`` rewrites every non-restricted service onto the
        injected internal net and adds the edge net to the runner. The
        materialiser must write those transforms to the temp-dir compose
        file before ``.start()`` runs."""
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file)
        ctx = _make_ctx(network_policy=NetworkPolicy.NO_INTERNET)
        factory = _FactoryRecorder()
        materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)

        handle = materialiser.materialise(decl, ctx)

        assert isinstance(handle, _DockerComposeStackHandle)
        transformed = yaml.safe_load((Path(factory.last.context) / compose_file.name).read_text())
        assert NETPOLICY_INTERNAL_NETWORK in transformed["networks"]
        assert NETPOLICY_EDGE_NETWORK in transformed["networks"]
        assert NETPOLICY_EDGE_NETWORK in transformed["services"]["runner"]["networks"]
        assert NETPOLICY_EDGE_NETWORK not in transformed["services"]["db-service"]["networks"]

    def test_full_internet_leaves_compose_untransformed(self, tmp_path: Path) -> None:
        """``full_internet`` is identity — the network-policy step must
        not rewrite the file; the credential-injection step still runs."""
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file)
        ctx = _make_ctx(network_policy=NetworkPolicy.FULL_INTERNET)
        materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())

        handle = materialiser.materialise(decl, ctx)

        transformed = yaml.safe_load(
            (Path(handle.temp_dir) / compose_file.name).read_text()  # type: ignore[attr-defined]
        )
        assert "networks" not in transformed

    def test_injects_runner_credentials_when_runner_service_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Credential injection places the engine's container-secrets
        payload on the runner service iff ``decl.runner_service`` is set."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-runner-cred")
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file, runner_service="runner")
        ctx = _make_ctx()
        materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())

        handle = materialiser.materialise(decl, ctx)

        written = yaml.safe_load(
            (Path(handle.temp_dir) / compose_file.name).read_text()  # type: ignore[attr-defined]
        )
        env = written["services"]["runner"]["environment"]
        assert CONTAINER_SECRETS_ENV_VAR in env

    def test_skips_runner_credentials_when_runner_service_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A runner-less stack must not receive credentials — the
        materialiser bypasses ``inject_runner_credentials`` when
        ``decl.runner_service is None``."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-runner-cred")
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file, runner_service=None, stack_scope="task")
        ctx = _make_ctx()
        materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())

        handle = materialiser.materialise(decl, ctx)

        written = yaml.safe_load(
            (Path(handle.temp_dir) / compose_file.name).read_text()  # type: ignore[attr-defined]
        )
        for svc in written["services"].values():
            env = svc.get("environment") or {}
            if isinstance(env, dict):
                assert CONTAINER_SECRETS_ENV_VAR not in env

    def test_mounts_docker_socket_iff_context_requests(self, tmp_path: Path) -> None:
        """The docker-socket bind mount lands on the runner iff
        ``ctx.mount_docker_socket`` is True."""
        for want_mount in (True, False):
            compose_file = _write_compose(tmp_path / f"case_{want_mount}")
            decl = _make_decl(compose_file)
            ctx = _make_ctx(mount_docker_socket=want_mount)
            materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())

            handle = materialiser.materialise(decl, ctx)

            written = yaml.safe_load(
                (Path(handle.temp_dir) / compose_file.name).read_text()  # type: ignore[attr-defined]
            )
            volumes = written["services"]["runner"].get("volumes") or []
            socket_mounted = any(
                isinstance(v, str) and v.startswith(f"{DOCKER_SOCKET_PATH}:") for v in volumes
            )
            message = f"mount_docker_socket={want_mount} produced socket_mounted={socket_mounted}"
            assert socket_mounted is want_mount, message

    def test_writes_env_file_iff_write_compose_env_set(self, tmp_path: Path) -> None:
        """The per-trial ``.env`` is written iff the context carries a
        :class:`WriteComposeEnv` directive — run-scope stacks pass
        ``None`` and must produce no ``.env``."""
        compose_file = _write_compose(tmp_path / "with_env")
        decl = _make_decl(compose_file, stack_scope="trial")
        ctx = _make_ctx(
            write_compose_env=WriteComposeEnv(
                trial_id="task_a:0", stack_inputs={"DB_NAME": "example"}
            )
        )
        materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())

        handle = materialiser.materialise(decl, ctx)

        env_file = Path(handle.temp_dir) / ".env"  # type: ignore[attr-defined]
        assert env_file.exists()
        env_text = env_file.read_text()
        assert "DB_NAME=example" in env_text
        assert f"{TOLOKAFORGE_TRIAL_SLUG_ENV}=" in env_text

    def test_no_env_file_when_write_compose_env_none(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path / "no_env")
        decl = _make_decl(compose_file)
        ctx = _make_ctx(write_compose_env=None)
        materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())

        handle = materialiser.materialise(decl, ctx)

        env_file = Path(handle.temp_dir) / ".env"  # type: ignore[attr-defined]
        assert not env_file.exists()


# ---------------------------------------------------------------------------
# materialise: failure path
# ---------------------------------------------------------------------------


class TestMaterialiseFailure:
    def test_start_failure_cleans_up_and_raises_provision_error(self, tmp_path: Path) -> None:
        """A failure inside ``.start()`` cleans up the temp dir and
        raises :class:`ProvisionError` with ``stage="provision"``. The
        run id (``ctx.scope_key``) travels on the error."""
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file)
        ctx = _make_ctx(scope_key="run-boom")
        factory = _FactoryRecorder(start_should_raise=RuntimeError("docker up failed"))
        materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)

        with pytest.raises(ProvisionError) as exc_info:
            materialiser.materialise(decl, ctx)

        err = exc_info.value
        assert err.stage == "provision"
        assert err.trial_id == "run-boom"
        assert "docker compose up failed" in err.reason
        # Temp dir cleaned up.
        assert not Path(factory.last.context).exists()
        # Compose was ``stop``ped as part of cleanup.
        stop_calls = [c for c in factory.last.calls if c[0] == "stop"]
        assert len(stop_calls) == 1

    def test_start_failure_captures_logs_when_log_capture_set(self, tmp_path: Path) -> None:
        """``ctx.log_capture`` set routes a per-service log dump to the
        declared dest_dir before cleanup — the materialiser's fail-time
        capture surface."""
        compose_file = _write_compose(tmp_path / "case")
        capture_dir = tmp_path / "capture"
        capture_calls: list[tuple[Any, tuple[str, ...], Path, int]] = []

        def fake_capture(compose: Any, services: Any, dest: Path, tail: int) -> dict[str, int]:
            services_tuple = tuple(services)
            capture_calls.append((compose, services_tuple, dest, tail))
            (dest).mkdir(parents=True, exist_ok=True)
            (dest / "runner.log").write_bytes(b"log-line\n")
            return {"runner": 9}

        import tolokaforge.core.docker_compose_materialiser as mod

        original_capture = mod.capture_compose_service_logs
        mod.capture_compose_service_logs = fake_capture  # type: ignore[assignment]
        try:
            decl = _make_decl(compose_file)
            ctx = _make_ctx(log_capture=MaterialiseLogCapture(dest_dir=capture_dir, tail=200))
            factory = _FactoryRecorder(start_should_raise=RuntimeError("boom"))
            materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)

            with pytest.raises(ProvisionError):
                materialiser.materialise(decl, ctx)
        finally:
            mod.capture_compose_service_logs = original_capture  # type: ignore[assignment]

        assert len(capture_calls) == 1
        _, services, dest, tail = capture_calls[0]
        assert set(services) == {"runner", "db-service"}
        assert dest == capture_dir
        assert tail == 200

    def test_start_failure_without_log_capture_skips_capture(self, tmp_path: Path) -> None:
        """``ctx.log_capture is None`` disables capture entirely — the
        materialiser must not touch the filesystem beyond cleanup."""
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file)
        ctx = _make_ctx(log_capture=None)
        factory = _FactoryRecorder(start_should_raise=RuntimeError("boom"))
        materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)

        with pytest.raises(ProvisionError):
            materialiser.materialise(decl, ctx)


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


class TestTeardown:
    def test_teardown_stops_compose_and_removes_temp_dir(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file)
        ctx = _make_ctx()
        factory = _FactoryRecorder()
        materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)

        handle = materialiser.materialise(decl, ctx)
        temp_dir = handle.temp_dir  # type: ignore[attr-defined]
        assert Path(temp_dir).exists()

        materialiser.teardown(handle)

        assert not Path(temp_dir).exists()
        stop_calls = [c for c in factory.last.calls if c[0] == "stop"]
        assert len(stop_calls) == 1

    def test_teardown_is_idempotent(self, tmp_path: Path) -> None:
        """A second teardown call is a no-op: compose ``stop`` runs
        again (best-effort) and ``rmtree(ignore_errors=True)`` sees the
        already-missing directory. Nothing raises."""
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file)
        ctx = _make_ctx()
        factory = _FactoryRecorder()
        materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)

        handle = materialiser.materialise(decl, ctx)
        materialiser.teardown(handle)
        materialiser.teardown(handle)  # must not raise

        # ``shutdown_compose`` swallows exceptions; two stop calls acceptable.
        stop_calls = [c for c in factory.last.calls if c[0] == "stop"]
        assert len(stop_calls) == 2

    def test_teardown_of_foreign_handle_raises_type_error(self) -> None:
        """A handle from another materialiser family is refused with
        :class:`TypeError` — teardown must not silently succeed on a
        handle it did not produce."""

        class _ForeignHandle:
            stack_id = "x"
            stack_scope = "run"
            runner_service: str | None = None

        materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())
        with pytest.raises(TypeError, match="_DockerComposeStackHandle"):
            materialiser.teardown(_ForeignHandle())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_endpoint / get_containers / capture_logs
# ---------------------------------------------------------------------------


class TestSecondarySurface:
    def test_resolve_endpoint_returns_host_port_from_compose(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file)
        ctx = _make_ctx()
        materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())
        handle = materialiser.materialise(decl, ctx)

        # Patch resolve_host_port to a known value.
        import tolokaforge.core.docker_compose_materialiser as mod

        original = mod.resolve_host_port
        mod.resolve_host_port = lambda compose, svc, port: (  # type: ignore[assignment]
            "localhost",
            60051,
        )
        try:
            endpoint = materialiser.resolve_endpoint(handle, "runner", 50051)
        finally:
            mod.resolve_host_port = original  # type: ignore[assignment]
        assert endpoint == ("localhost", 60051)

    def test_resolve_endpoint_returns_none_when_unresolvable(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path)
        decl = _make_decl(compose_file)
        ctx = _make_ctx()
        materialiser = DockerComposeMaterialiser(docker_compose_factory=_FactoryRecorder())
        handle = materialiser.materialise(decl, ctx)

        import tolokaforge.core.docker_compose_materialiser as mod

        original = mod.resolve_host_port
        mod.resolve_host_port = lambda *a, **k: (None, None)  # type: ignore[assignment]
        try:
            assert materialiser.resolve_endpoint(handle, "absent", 1234) is None
        finally:
            mod.resolve_host_port = original  # type: ignore[assignment]
