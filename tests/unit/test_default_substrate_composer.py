"""Unit tests for :class:`DefaultSubstrateComposer`.

Every test wires a fake :class:`ComposeMaterialiser` and a fake dispatcher
registry through the composer's public seams (constructor fields), so the
sequence of materialise / dispatch / teardown calls is observable in
process with no docker daemon. The ``_validate_plan`` INV-12 check and
the module-level helpers ``_run_scope_log_capture`` /
``_trial_scope_log_capture`` are exercised as their own bracket.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.composition_runtime import (
    ComposedEnvHandle,
    MaterialiseContext,
    RunCtx,
    RunSubstrate,
    StackHandle,
)
from tolokaforge.core.default_substrate_composer import (
    DefaultSubstrateComposer,
    _run_scope_log_capture,
    _trial_scope_log_capture,
    _validate_plan,
)
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.models.task_config import SeedRef
from tolokaforge.core.run_display_events import _NULL_EVENTS, ContainerSnapshot
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.service_readiness import (
    InMemoryServiceReadinessProbe,
    ServiceReadinessProbe,
)
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import (
    ResetSpec,
    ServiceIsolation,
    ServiceSpec,
    StackDecl,
    StackScope,
    TaskDescription,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture compose files (safe under the manifest safety validators)
# ---------------------------------------------------------------------------


_RUNNER_COMPOSE = (
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


def _write_compose(tmp_path: Path, name: str = "environment.compose.yaml") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    compose = tmp_path / name
    compose.write_text(_RUNNER_COMPOSE)
    return compose


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeHandle:
    """Non-docker :class:`StackHandle` a fake materialiser produces."""

    stack_id: str
    stack_scope: StackScope
    runner_service: str | None


@dataclass
class _FakeMaterialiser:
    """Records every call; hands back deterministic endpoints.

    Set :attr:`raise_on_next` to seed a failure on the next
    :meth:`materialise` invocation. :attr:`endpoint_map` overrides
    :meth:`resolve_endpoint` per ``(service, port)`` pair.
    :attr:`materialise_contexts` snapshots the full
    :class:`MaterialiseContext` per ``materialise`` call so tests can
    assert on threaded policy values (``mount_docker_socket``,
    ``log_capture``, ``events``).
    """

    name: str = "fake"
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    materialise_contexts: list[MaterialiseContext] = field(default_factory=list)
    torn_down: list[str] = field(default_factory=list)
    raise_on_next: BaseException | None = None
    endpoint_map: dict[tuple[str, int], tuple[str, int] | None] = field(default_factory=dict)

    def materialise(self, decl: StackDecl, ctx: MaterialiseContext) -> StackHandle:
        self.calls.append(("materialise", (decl.stack_id, ctx.scope_key, ctx.component_id_prefix)))
        self.materialise_contexts.append(ctx)
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        return _FakeHandle(
            stack_id=decl.stack_id,
            stack_scope=decl.stack_scope,
            runner_service=decl.runner_service,
        )

    def resolve_endpoint(
        self, handle: StackHandle, service: str, container_port: int
    ) -> tuple[str, int] | None:
        self.calls.append(("resolve_endpoint", (handle.stack_id, service, container_port)))
        if (service, container_port) in self.endpoint_map:
            return self.endpoint_map[(service, container_port)]
        return ("127.0.0.1", 60000 + container_port)

    def get_containers(self, handle: StackHandle) -> list[ContainerSnapshot]:
        del handle
        return []

    def capture_logs(
        self,
        handle: StackHandle,
        services: tuple[str, ...],
        dest_dir: Path,
        tail: int,
    ) -> dict[str, int]:
        del handle, services, dest_dir, tail
        return {}

    def teardown(self, handle: StackHandle) -> None:
        self.calls.append(("teardown", (handle.stack_id,)))
        self.torn_down.append(handle.stack_id)


@dataclass
class _RecordingDispatcher:
    """Fake dispatcher that records every :meth:`cycle` invocation."""

    isolation: ServiceIsolation
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def cycle(
        self,
        service_name: str,
        service_spec: ServiceSpec,
        stack_handle: StackHandle,
        materialiser: Any,
        *,
        seeds: Mapping[str, SeedRef],
    ) -> None:
        del materialiser, seeds
        self.calls.append((service_name, stack_handle.stack_id, service_spec.isolation))


class _FakeRunnerClient:
    """Records ``close()`` so teardown-order assertions can look at it."""

    def __init__(self, runner_address: str, events: Any = None) -> None:
        self.runner_address = runner_address
        self.events = events
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        del timeout, retry_interval

    def health_check(self) -> bool:
        return True


def _fake_client_factory(runner_address: str, events: Any) -> _FakeRunnerClient:
    return _FakeRunnerClient(runner_address=runner_address, events=events)


def _always_ready_loader(kind: str) -> Callable[[], ServiceReadinessProbe]:
    del kind
    return lambda: InMemoryServiceReadinessProbe(ok=True)


# ---------------------------------------------------------------------------
# Helpers to build plans + specs
# ---------------------------------------------------------------------------


def _decl(
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


def _manifest(
    compose_file: Path, services: dict[str, ServiceSpec] | None = None
) -> EnvironmentManifest:
    kwargs: dict[str, Any] = {"compose_file": compose_file, "runner_service": "runner"}
    if services is not None:
        kwargs["services"] = services
    return EnvironmentManifest(**kwargs)


def _run_ctx(
    manifest: EnvironmentManifest,
    *,
    run_id: str = "run-a",
    seeds: dict[str, SeedRef] | None = None,
) -> RunCtx:
    return RunCtx(
        run_id=run_id,
        manifest=manifest,
        mount_docker_socket=False,
        log_capture=None,
        events=_NULL_EVENTS,
        seeds=seeds or {},
    )


def _trial_spec(
    manifest: EnvironmentManifest | None,
    *,
    task_id: str = "task-1",
    trial_id: str = "task-1:0",
) -> TrialSpec:
    return TrialSpec(
        trial_id=trial_id,
        run_id="run-a",
        task=TaskDescription(
            task_id=task_id,
            name=task_id,
            category="test",
            description="unit-test stub",
            adapter_type="native",
            system_prompt="",
            environment_manifest=manifest,
        ),
        agent_model_config=ModelConfig(provider="anthropic", name="stub"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:5432",
            runner_url="http://placeholder:50051",
        ),
    )


def _empty_run_sub(run_id: str = "run-a") -> RunSubstrate:
    return RunSubstrate(
        run_id=run_id,
        run_stack_handles=(),
        task_stack_handles={},
        runner_client=None,
        endpoints=None,
        seeds={},
        mount_docker_socket=False,
        log_capture=None,
        events=_NULL_EVENTS,
    )


# ---------------------------------------------------------------------------
# _validate_plan — INV-12
# ---------------------------------------------------------------------------


class TestValidatePlan:
    def test_accepts_single_stack_with_one_runner(self, tmp_path: Path) -> None:
        """A minimal single-stack plan with a runner is what every existing
        Case-B run resolves to — INV-12 must accept it unchanged."""
        compose = _write_compose(tmp_path)
        _validate_plan([_decl(compose)])

    def test_refuses_two_stacks_with_runner_service(self, tmp_path: Path) -> None:
        """More than one stack setting ``runner_service`` is refused —
        the refusal names the offending stacks so the operator can find
        them without re-reading the plan."""
        compose_a = _write_compose(tmp_path / "a")
        compose_b = _write_compose(tmp_path / "b")
        plan = [
            _decl(compose_a, stack_id="stack_a"),
            _decl(compose_b, stack_id="stack_b"),
        ]
        with pytest.raises(ProvisionError) as excinfo:
            _validate_plan(plan)
        assert excinfo.value.stage == "materialise_run"
        assert "INV-12" in excinfo.value.reason
        assert "'stack_a'" in excinfo.value.reason
        assert "'stack_b'" in excinfo.value.reason

    def test_refuses_plan_with_no_runner_and_no_trial_scope(self, tmp_path: Path) -> None:
        """A run with no runner service and no trial-scope stack has no
        runner at all — INV-12 catches the shape at plan-resolve time."""
        compose = _write_compose(tmp_path)
        plan = [_decl(compose, stack_scope="task", runner_service=None)]
        with pytest.raises(ProvisionError) as excinfo:
            _validate_plan(plan)
        assert excinfo.value.stage == "materialise_run"
        assert "INV-12" in excinfo.value.reason
        assert "no runner" in excinfo.value.reason

    def test_accepts_zero_runners_when_trial_scope_stack_present(self, tmp_path: Path) -> None:
        """A trial-scope stack owns the runner in the TRIAL_SCOPED_ONLY
        shape — INV-12 permits zero runners in that case."""
        compose = _write_compose(tmp_path)
        plan = [_decl(compose, stack_scope="trial", runner_service="runner")]
        _validate_plan(plan)  # single-runner case
        plan_no_runner_but_trial = [
            _decl(compose, stack_scope="trial", runner_service="runner"),
        ]
        _validate_plan(plan_no_runner_but_trial)


# ---------------------------------------------------------------------------
# materialise_run — SINGLE_RUN plan (Flow A)
# ---------------------------------------------------------------------------


class TestMaterialiseRunSingleRunPlan:
    def test_materialises_once_and_wires_runner_client(self, tmp_path: Path) -> None:
        """One run-scope decl → one ``materialise`` call, the returned
        substrate carries the run-scope handle, the runner client is
        constructed via the injected factory, and endpoints resolve from
        the fake materialiser."""
        compose = _write_compose(tmp_path)
        manifest = _manifest(compose)
        plan = [_decl(compose)]
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )

        substrate = composer.materialise_run(plan, _run_ctx(manifest))

        materialise_calls = [c for c in materialiser.calls if c[0] == "materialise"]
        assert len(materialise_calls) == 1
        _, (stack_id, scope_key, prefix) = materialise_calls[0]
        assert stack_id == "default"
        assert scope_key == "run-a"
        assert prefix == "engine"
        assert len(substrate.run_stack_handles) == 1
        assert isinstance(substrate.runner_client, _FakeRunnerClient)
        assert substrate.endpoints is not None
        assert substrate.endpoints.runner_url == "http://127.0.0.1:110051"
        assert substrate.seeds == {}

    def test_seeds_are_threaded_from_run_ctx_onto_substrate(self, tmp_path: Path) -> None:
        """The critic-approved plumbing: ``RunCtx.seeds`` reaches
        :attr:`RunSubstrate.seeds` unchanged so downstream
        ``provision_trial`` / ``cycle_between_trials`` calls resolve
        ``reset``-labelled services without callers re-threading them."""
        compose = _write_compose(tmp_path)
        manifest = _manifest(compose)
        seed = SeedRef.model_validate(
            {"path": "seed.sql", "kind": "sql_dump", "digest": "sha256:" + "0" * 64}
        )
        seeds: dict[str, SeedRef] = {"baseline": seed}
        composer = DefaultSubstrateComposer(
            materialiser=_FakeMaterialiser(),
            runner_client_factory=_fake_client_factory,
        )

        substrate = composer.materialise_run(
            [_decl(compose)],
            _run_ctx(manifest, seeds=seeds),
        )

        assert substrate.seeds == seeds

    def test_materialise_failure_tears_down_already_materialised_stacks(
        self, tmp_path: Path
    ) -> None:
        """A raise inside the sole ``materialise`` call must not leak
        partially-materialised state — the composer catches, runs
        teardown best-effort, and re-raises."""
        compose = _write_compose(tmp_path)
        manifest = _manifest(compose)
        materialiser = _FakeMaterialiser(raise_on_next=RuntimeError("boom"))
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
        )

        with pytest.raises(RuntimeError, match="boom"):
            composer.materialise_run([_decl(compose)], _run_ctx(manifest))

        # No teardown call because the sole materialise raised before returning
        # a handle — the composer's rollback iterates handles already produced.
        assert not any(c[0] == "teardown" for c in materialiser.calls)

    def test_refuses_when_runner_service_port_unresolvable(self, tmp_path: Path) -> None:
        """The materialiser returning ``None`` for the runner's declared
        port surfaces as :class:`ProvisionError` naming the service +
        port — matches today's shared-stack error text."""
        compose = _write_compose(tmp_path)
        manifest = _manifest(compose)
        materialiser = _FakeMaterialiser(
            endpoint_map={("runner", 50051): None},
        )
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
        )

        with pytest.raises(ProvisionError) as excinfo:
            composer.materialise_run([_decl(compose)], _run_ctx(manifest))
        assert excinfo.value.stage == "materialise_run"
        assert "'runner'" in excinfo.value.reason
        assert "50051" in excinfo.value.reason
        # Rollback fired: the materialised handle was torn down.
        assert materialiser.torn_down == ["default"]


# ---------------------------------------------------------------------------
# provision_trial — TRIAL_SCOPED_ONLY plan (Flow B)
# ---------------------------------------------------------------------------


class TestProvisionTrialTrialScopedPlan:
    def test_materialises_trial_scope_stack_and_constructs_runner_client(
        self, tmp_path: Path
    ) -> None:
        """A trial-scope decl brings the substrate up per-trial: the fake
        materialiser sees one ``materialise`` call scoped to
        ``spec.trial_id`` with the ``trial/<id>`` prefix, and the trial
        runner client + endpoints are wired on the handle."""
        compose = _write_compose(tmp_path)
        manifest = _manifest(compose)
        plan = [_decl(compose, stack_scope="trial", runner_service="runner")]
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )
        spec = _trial_spec(manifest)

        env_handle = composer.provision_trial(plan, spec, _empty_run_sub())

        m_calls = [c for c in materialiser.calls if c[0] == "materialise"]
        assert len(m_calls) == 1
        _, (stack_id, scope_key, prefix) = m_calls[0]
        assert stack_id == "default"
        assert scope_key == spec.trial_id
        assert prefix == f"trial/{spec.trial_id}"
        assert len(env_handle.trial_stack_handles) == 1
        assert env_handle.trial_endpoints is not None
        assert env_handle.trial_runner_client is not None

    def test_reset_recipes_dispatch_on_newly_materialised_stack(self, tmp_path: Path) -> None:
        """Every ``reset``-labelled service on a newly-materialised trial
        stack is cycled through the injected reset dispatcher; a
        ``shared`` service is not cycled at reset time (the reset seam
        only fires for ``reset`` labels)."""
        compose = _write_compose(tmp_path)
        seed = SeedRef.model_validate(
            {"path": "seed.sql", "kind": "sql_dump", "digest": "sha256:" + "0" * 64}
        )
        services = {
            "db-service": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
            "runner": ServiceSpec(isolation="shared"),
        }
        manifest = _manifest(compose, services=services)
        plan = [_decl(compose, stack_scope="trial")]
        materialiser = _FakeMaterialiser()
        reset_dispatcher = _RecordingDispatcher(isolation="reset")
        registry: dict[ServiceIsolation, Any] = {
            "shared": _RecordingDispatcher(isolation="shared"),
            "reset": reset_dispatcher,
            "ephemeral": _RecordingDispatcher(isolation="ephemeral"),
        }
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            dispatcher_registry=registry,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )
        run_sub = _empty_run_sub()
        run_sub.seeds = {"baseline": seed}  # type: ignore[assignment]
        spec = _trial_spec(manifest)

        composer.provision_trial(plan, spec, run_sub)

        assert reset_dispatcher.calls == [("db-service", "default", "reset")]

    def test_refuses_on_missing_seed_for_reset_service(self, tmp_path: Path) -> None:
        """A ``reset`` service naming a seed absent from
        :attr:`RunSubstrate.seeds` fails loud with the canonical
        reset-recipe refusal message."""
        compose = _write_compose(tmp_path)
        services = {
            "db-service": ServiceSpec(isolation="reset", reset=ResetSpec(seed="absent")),
        }
        manifest = _manifest(compose, services=services)
        plan = [_decl(compose, stack_scope="trial")]
        composer = DefaultSubstrateComposer(
            materialiser=_FakeMaterialiser(),
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )
        spec = _trial_spec(manifest)

        with pytest.raises(ProvisionError) as excinfo:
            composer.provision_trial(plan, spec, _empty_run_sub())
        assert excinfo.value.stage == "reset_recipe"
        assert excinfo.value.reason == (
            "service 'db-service' names seed 'absent' but "
            "the backend has no such seed in its registry "
            "(available: [])."
        )

    def test_refuses_reserved_prefix_in_stack_inputs(self, tmp_path: Path) -> None:
        """A ``TOLOKAFORGE_``-prefixed key in ``stack_inputs`` collides
        with the engine-authored compose variables — refused before any
        materialise call, matching today's per-trial backend check."""
        compose = _write_compose(tmp_path)
        manifest = EnvironmentManifest(
            compose_file=compose,
            runner_service="runner",
            stack_inputs={"TOLOKAFORGE_TRIAL_SLUG": "explicit"},
        )
        plan = [_decl(compose, stack_scope="trial")]
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
        )
        spec = _trial_spec(manifest)

        with pytest.raises(ProvisionError) as excinfo:
            composer.provision_trial(plan, spec, _empty_run_sub())
        assert excinfo.value.stage == "provision"
        assert "TOLOKAFORGE_" in excinfo.value.reason
        # No materialise call reached — the check runs before any
        # substrate work.
        assert not any(c[0] == "materialise" for c in materialiser.calls)

    def test_run_substrate_log_capture_and_events_thread_to_materialise_context(
        self, tmp_path: Path
    ) -> None:
        """A non-null :attr:`RunSubstrate.log_capture` +
        :attr:`RunSubstrate.events` govern the
        :class:`MaterialiseContext` the composer hands the materialiser
        for both task-scope and trial-scope stacks — the run-wide
        policy is what materialisation runs under, not a per-scope
        default."""

        class _RecordingEvents:
            """Non-null :class:`RunDisplayEvents` sink; identity-checked."""

        events_sink = _RecordingEvents()
        run_log_capture = LogCaptureConfig(output_root=tmp_path / "out", tail=125, on_success=False)
        task_compose = _write_compose(tmp_path / "task", name="task.compose.yaml")
        trial_compose = _write_compose(tmp_path / "trial", name="trial.compose.yaml")
        manifest = _manifest(trial_compose)
        plan = [
            _decl(
                task_compose,
                stack_id="task-stack",
                stack_scope="task",
                runner_service=None,
            ),
            _decl(
                trial_compose,
                stack_id="trial-stack",
                stack_scope="trial",
                runner_service="runner",
            ),
        ]
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )
        spec = _trial_spec(manifest)
        run_sub = RunSubstrate(
            run_id="run-a",
            run_stack_handles=(),
            task_stack_handles={},
            runner_client=None,
            endpoints=None,
            seeds={},
            mount_docker_socket=False,
            log_capture=run_log_capture,
            events=events_sink,  # type: ignore[arg-type]
        )

        composer.provision_trial(plan, spec, run_sub)

        contexts_by_stack = {ctx.stack_id: ctx for ctx in materialiser.materialise_contexts}
        assert set(contexts_by_stack) == {"task-stack", "trial-stack"}
        for stack_id in ("task-stack", "trial-stack"):
            ctx = contexts_by_stack[stack_id]
            assert ctx.events is events_sink, (
                f"stack {stack_id!r} MaterialiseContext.events was not "
                "threaded from RunSubstrate.events"
            )
            assert ctx.log_capture is not None, (
                f"stack {stack_id!r} MaterialiseContext.log_capture is None; "
                "RunSubstrate.log_capture was not threaded"
            )
            assert ctx.log_capture.tail == run_log_capture.tail
            # trial_services_dir writes under <output_root>/trials/<task>/<idx>/services/.
            assert ctx.log_capture.dest_dir == (
                run_log_capture.output_root / "trials" / spec.task.task_id / "0" / "services"
            )

    def test_run_substrate_mount_docker_socket_writes_socket_mount_to_trial_compose(
        self, tmp_path: Path
    ) -> None:
        """A ``RunSubstrate.mount_docker_socket=True`` reaches the
        docker-compose materialiser via
        :attr:`MaterialiseContext.mount_docker_socket`, which then adds
        the host docker-socket bind-mount to the runner service on the
        trial-scope stack's compose file. Parity-shape test — uses the
        real :class:`DockerComposeMaterialiser` with a stub
        ``docker_compose_factory``."""
        from tests.canonical._docker_compose_stubs import InertDockerCompose
        from tolokaforge.core.docker_compose_materialiser import (
            DockerComposeMaterialiser,
            _DockerComposeStackHandle,
        )

        compose_file = tmp_path / "trial.compose.yaml"
        compose_file.write_text(_RUNNER_COMPOSE)
        stubs: list[InertDockerCompose] = []

        def factory(**kwargs: Any) -> InertDockerCompose:
            stub = InertDockerCompose(**kwargs)
            stubs.append(stub)
            return stub

        materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
            readiness_probe_loader=_always_ready_loader,
        )
        manifest = _manifest(compose_file)
        plan = [
            _decl(
                compose_file,
                stack_id="trial-stack",
                stack_scope="trial",
                runner_service="runner",
            )
        ]
        spec = _trial_spec(manifest)
        run_sub = RunSubstrate(
            run_id="run-a",
            run_stack_handles=(),
            task_stack_handles={},
            runner_client=None,
            endpoints=None,
            seeds={},
            mount_docker_socket=True,
            log_capture=None,
            events=_NULL_EVENTS,
        )

        env_handle = composer.provision_trial(plan, spec, run_sub)
        try:
            trial_handle = env_handle.trial_stack_handles[0]
            assert isinstance(trial_handle, _DockerComposeStackHandle)
            written = (trial_handle.temp_dir / compose_file.name).read_text()
            assert "/var/run/docker.sock:/var/run/docker.sock" in written
        finally:
            composer.teardown_trial(env_handle)


# ---------------------------------------------------------------------------
# Reserved-prefix refusal — uniform across materialise_run + provision_trial
# ---------------------------------------------------------------------------


class TestReservedPrefixRefusalUniformity:
    """Both composer entry points fail-loud on the reserved
    ``TOLOKAFORGE_`` ``stack_inputs`` prefix with identical reason text.

    :meth:`DefaultSubstrateComposer.materialise_run` fires the refusal
    with ``stage="materialise_run"`` and ``trial_id=ctx.run_id``, before
    ``_validate_plan`` and any materialiser call.
    :meth:`DefaultSubstrateComposer.provision_trial` fires the refusal
    with ``stage="provision"`` and ``trial_id=spec.trial_id``. The
    reason text (past the id) is byte-identical.
    """

    def test_materialise_run_refuses_reserved_prefix(self, tmp_path: Path) -> None:
        """A run-scope manifest whose ``stack_inputs`` uses the reserved
        ``TOLOKAFORGE_`` prefix fails before any materialise call — the
        run id (not a trial id) surfaces on the refusal, and
        ``stage`` is ``"materialise_run"``."""
        compose = _write_compose(tmp_path)
        manifest = EnvironmentManifest(
            compose_file=compose,
            runner_service="runner",
            stack_inputs={"TOLOKAFORGE_FOO": "x"},
        )
        plan = [_decl(compose)]
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
        )
        ctx = _run_ctx(manifest, run_id="run-b")

        with pytest.raises(ProvisionError) as excinfo:
            composer.materialise_run(plan, ctx)
        assert excinfo.value.stage == "materialise_run"
        assert excinfo.value.trial_id == "run-b"
        assert excinfo.value.reason == (
            "stack_inputs key 'TOLOKAFORGE_FOO' uses the reserved "
            "TOLOKAFORGE_ prefix (engine-authored compose variables); "
            "rename or remove it from the manifest"
        )
        # No materialise reached — refusal precedes every substrate call.
        assert not any(c[0] == "materialise" for c in materialiser.calls)

    def test_provision_trial_refusal_still_fires_and_uses_stage_provision(
        self, tmp_path: Path
    ) -> None:
        """Regression guard on the default ``stage`` kwarg of
        :func:`_refuse_reserved_prefix` — ``provision_trial`` must
        continue to surface ``stage="provision"`` with the trial id and
        the same reason text ``materialise_run`` produces (modulo id)."""
        compose = _write_compose(tmp_path)
        manifest = EnvironmentManifest(
            compose_file=compose,
            runner_service="runner",
            stack_inputs={"TOLOKAFORGE_FOO": "x"},
        )
        plan = [_decl(compose, stack_scope="trial")]
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
        )
        spec = _trial_spec(manifest, trial_id="task-1:0")

        with pytest.raises(ProvisionError) as excinfo:
            composer.provision_trial(plan, spec, _empty_run_sub())
        assert excinfo.value.stage == "provision"
        assert excinfo.value.trial_id == "task-1:0"
        assert excinfo.value.reason == (
            "stack_inputs key 'TOLOKAFORGE_FOO' uses the reserved "
            "TOLOKAFORGE_ prefix (engine-authored compose variables); "
            "rename or remove it from the manifest"
        )
        assert not any(c[0] == "materialise" for c in materialiser.calls)


# ---------------------------------------------------------------------------
# cycle_between_trials
# ---------------------------------------------------------------------------


class TestCycleBetweenTrials:
    def test_all_shared_services_produce_one_noop_cycle_per_service(self, tmp_path: Path) -> None:
        """A SINGLE_RUN plan with every service labelled ``shared`` still
        drives ``dispatcher.cycle`` once per service, per run-scope stack
        — the dispatcher-side no-op is what makes the cycle observably
        do nothing."""
        compose = _write_compose(tmp_path)
        services = {
            "runner": ServiceSpec(isolation="shared"),
            "db-service": ServiceSpec(isolation="shared"),
        }
        manifest = _manifest(compose, services=services)
        shared_dispatcher = _RecordingDispatcher(isolation="shared")
        registry: dict[ServiceIsolation, Any] = {
            "shared": shared_dispatcher,
            "reset": _RecordingDispatcher(isolation="reset"),
            "ephemeral": _RecordingDispatcher(isolation="ephemeral"),
        }
        composer = DefaultSubstrateComposer(
            materialiser=_FakeMaterialiser(),
            dispatcher_registry=registry,
            runner_client_factory=_fake_client_factory,
        )
        spec = _trial_spec(manifest)
        run_sub = _empty_run_sub()
        run_sub.run_stack_handles = (
            _FakeHandle(stack_id="default", stack_scope="run", runner_service="runner"),
        )

        composer.cycle_between_trials(run_sub, spec)

        assert shared_dispatcher.calls == [
            ("runner", "default", "shared"),
            ("db-service", "default", "shared"),
        ]

    def test_refuses_when_registry_missing_referenced_label(self, tmp_path: Path) -> None:
        """A registry with a hole (missing ``reset``) plus a ``reset``
        service refuses cycle with a message that names both the label
        and the stack."""
        compose = _write_compose(tmp_path)
        seed = SeedRef.model_validate(
            {"path": "seed.sql", "kind": "sql_dump", "digest": "sha256:" + "0" * 64}
        )
        services = {
            "db-service": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
        }
        manifest = _manifest(compose, services=services)
        # Registry with the ``reset`` slot removed.
        registry: dict[ServiceIsolation, Any] = {
            "shared": _RecordingDispatcher(isolation="shared"),
            "ephemeral": _RecordingDispatcher(isolation="ephemeral"),
        }
        composer = DefaultSubstrateComposer(
            materialiser=_FakeMaterialiser(),
            dispatcher_registry=registry,
            runner_client_factory=_fake_client_factory,
        )
        spec = _trial_spec(manifest)
        run_sub = _empty_run_sub()
        run_sub.run_stack_handles = (
            _FakeHandle(stack_id="engine", stack_scope="run", runner_service="runner"),
        )
        run_sub.seeds = {"baseline": seed}  # type: ignore[assignment]

        with pytest.raises(ProvisionError) as excinfo:
            composer.cycle_between_trials(run_sub, spec)
        assert excinfo.value.stage == "cycle"
        assert "'reset'" in excinfo.value.reason
        assert "'engine'" in excinfo.value.reason


# ---------------------------------------------------------------------------
# teardown_trial / teardown_run — idempotency
# ---------------------------------------------------------------------------


class TestTeardownIdempotency:
    def test_teardown_trial_calls_teardown_on_every_trial_handle(self, tmp_path: Path) -> None:
        """Every handle on :attr:`ComposedEnvHandle.trial_stack_handles`
        gets a teardown; the trial runner client is closed if set."""
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
        )
        runner_client = _FakeRunnerClient(runner_address="127.0.0.1:60051", events=None)
        env_handle = ComposedEnvHandle(
            trial_id="task-1:0",
            trial_stack_handles=(
                _FakeHandle(stack_id="a", stack_scope="trial", runner_service="runner"),
            ),
            trial_endpoints=None,
            trial_runner_client=runner_client,
        )

        composer.teardown_trial(env_handle)
        # Second call — idempotency contract.
        composer.teardown_trial(env_handle)

        teardowns = [c for c in materialiser.calls if c[0] == "teardown"]
        # Two calls per stack (idempotency) — materialiser handles the no-op.
        assert len(teardowns) == 2
        assert runner_client.closed

    def test_teardown_swallows_materialiser_exceptions(self, tmp_path: Path) -> None:
        """A materialiser that raises in ``teardown`` must not abort the
        sibling teardowns — the composer catches per-handle."""

        class _RaisingMaterialiser(_FakeMaterialiser):
            def teardown(self, handle: StackHandle) -> None:
                self.calls.append(("teardown", (handle.stack_id,)))
                raise RuntimeError("teardown boom")

        materialiser = _RaisingMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
        )
        env_handle = ComposedEnvHandle(
            trial_id="task-1:0",
            trial_stack_handles=(
                _FakeHandle(stack_id="a", stack_scope="trial", runner_service=None),
                _FakeHandle(stack_id="b", stack_scope="trial", runner_service=None),
            ),
            trial_endpoints=None,
            trial_runner_client=None,
        )

        # Must not raise.
        composer.teardown_trial(env_handle)

        # Both handles' teardowns fired despite the first raising.
        teardowns = [c for c in materialiser.calls if c[0] == "teardown"]
        assert [call[1][0] for call in teardowns] == ["a", "b"]

    def test_teardown_run_walks_task_then_run_handles(self, tmp_path: Path) -> None:
        """The composer walks task-scope handles first, then run-scope
        handles, and closes the run runner client if set."""
        materialiser = _FakeMaterialiser()
        composer = DefaultSubstrateComposer(
            materialiser=materialiser,
            runner_client_factory=_fake_client_factory,
        )
        runner_client = _FakeRunnerClient(runner_address="127.0.0.1:60051", events=None)
        run_sub = RunSubstrate(
            run_id="run-a",
            run_stack_handles=(
                _FakeHandle(stack_id="engine", stack_scope="run", runner_service="runner"),
            ),
            task_stack_handles={
                ("task-a", "worker"): _FakeHandle(
                    stack_id="worker", stack_scope="task", runner_service=None
                ),
            },
            runner_client=runner_client,
            endpoints=None,
            seeds={},
            mount_docker_socket=False,
            log_capture=None,
            events=_NULL_EVENTS,
        )

        composer.teardown_run(run_sub)

        teardowns = [c for c in materialiser.calls if c[0] == "teardown"]
        assert [call[1][0] for call in teardowns] == ["worker", "engine"]
        assert runner_client.closed


# ---------------------------------------------------------------------------
# runner_client_for / endpoints_for
# ---------------------------------------------------------------------------


class TestRunnerClientAndEndpointsResolution:
    def test_trial_runner_client_wins_over_run_client(self, tmp_path: Path) -> None:
        """When both are set, the trial-scope client + endpoints take
        precedence — the run-scope pair is the run-wide fallback."""
        composer = DefaultSubstrateComposer(
            materialiser=_FakeMaterialiser(),
            runner_client_factory=_fake_client_factory,
        )
        run_client = _FakeRunnerClient(runner_address="127.0.0.1:1", events=None)
        trial_client = _FakeRunnerClient(runner_address="127.0.0.1:2", events=None)
        run_sub = RunSubstrate(
            run_id="r",
            run_stack_handles=(),
            task_stack_handles={},
            runner_client=run_client,
            endpoints=EnvEndpoints(runner_url="http://run:1"),
            seeds={},
            mount_docker_socket=False,
            log_capture=None,
            events=_NULL_EVENTS,
        )
        env_handle = ComposedEnvHandle(
            trial_id="t",
            trial_stack_handles=(),
            trial_endpoints=EnvEndpoints(runner_url="http://trial:2"),
            trial_runner_client=trial_client,
        )
        assert composer.runner_client_for(run_sub, env_handle) is trial_client
        assert composer.endpoints_for(run_sub, env_handle).runner_url == "http://trial:2"

    def test_run_runner_client_used_when_trial_client_is_none(self, tmp_path: Path) -> None:
        composer = DefaultSubstrateComposer(
            materialiser=_FakeMaterialiser(),
            runner_client_factory=_fake_client_factory,
        )
        run_client = _FakeRunnerClient(runner_address="127.0.0.1:1", events=None)
        run_sub = RunSubstrate(
            run_id="r",
            run_stack_handles=(),
            task_stack_handles={},
            runner_client=run_client,
            endpoints=EnvEndpoints(runner_url="http://run:1"),
            seeds={},
            mount_docker_socket=False,
            log_capture=None,
            events=_NULL_EVENTS,
        )
        env_handle = ComposedEnvHandle(
            trial_id="t",
            trial_stack_handles=(),
            trial_endpoints=None,
            trial_runner_client=None,
        )
        assert composer.runner_client_for(run_sub, env_handle) is run_client
        assert composer.endpoints_for(run_sub, env_handle).runner_url == "http://run:1"

    def test_raises_runtime_error_when_neither_scope_owns_the_runner(self, tmp_path: Path) -> None:
        """A plan that declares no runner (neither on a run-scope nor a
        trial-scope stack) leaves both fields ``None`` — resolving a
        runner is a caller misuse and raises loudly."""
        composer = DefaultSubstrateComposer(
            materialiser=_FakeMaterialiser(),
            runner_client_factory=_fake_client_factory,
        )
        run_sub = _empty_run_sub()
        env_handle = ComposedEnvHandle(
            trial_id="t",
            trial_stack_handles=(),
            trial_endpoints=None,
            trial_runner_client=None,
        )
        with pytest.raises(RuntimeError, match="no runner_service"):
            composer.runner_client_for(run_sub, env_handle)
        with pytest.raises(RuntimeError, match="no runner_service"):
            composer.endpoints_for(run_sub, env_handle)


# ---------------------------------------------------------------------------
# Log-capture helper adapters
# ---------------------------------------------------------------------------


class TestLogCaptureAdapters:
    def test_run_scope_capture_returns_none_when_disabled(self, tmp_path: Path) -> None:
        assert _run_scope_log_capture(None) is None

    def test_run_scope_capture_writes_under_output_root_services(self, tmp_path: Path) -> None:
        capture = LogCaptureConfig(output_root=tmp_path, tail=250, on_success=False)
        result = _run_scope_log_capture(capture)
        assert result is not None
        assert result.dest_dir == tmp_path / "services"
        assert result.tail == 250

    def test_trial_scope_capture_writes_under_trials_task_index(self, tmp_path: Path) -> None:
        capture = LogCaptureConfig(output_root=tmp_path, tail=100, on_success=False)
        result = _trial_scope_log_capture(capture, "task-1:0")
        assert result is not None
        assert result.dest_dir == tmp_path / "trials" / "task-1" / "0" / "services"
        assert result.tail == 100

    def test_trial_scope_capture_returns_none_when_disabled(self, tmp_path: Path) -> None:
        assert _trial_scope_log_capture(None, "task-1:0") is None
