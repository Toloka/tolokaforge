"""Unit tests for the built-in :class:`ServiceLifecycleDispatcher` triple.

Every test injects a hand-rolled :class:`_StubDockerCompose` +
:class:`_DockerComposeStackHandle` so the sequence of docker-side calls
is observable in-process without a docker daemon. The ``reset``
dispatcher's :func:`recipe_dispatch` seam is exercised through a fake
:class:`~tolokaforge.runtime.reset_recipes.RecipeDispatcher` swapped into
:data:`RECIPE_REGISTRY`; the ``ephemeral`` dispatcher's subprocess seam
is exercised through the constructor-injected
:attr:`EphemeralDispatcher.subprocess_runner`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.docker_compose_materialiser import _DockerComposeStackHandle
from tolokaforge.core.models.task_config import SeedRef
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.service_lifecycle_dispatchers import (
    DISPATCHER_REGISTRY,
    EphemeralDispatcher,
    ResetDispatcher,
    SharedDispatcher,
    dispatch,
)
from tolokaforge.runner.models import ResetSpec, ServiceIsolation, ServiceSpec
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _SpyDockerCompose:
    """Records every attribute access and method call.

    The ``shared`` dispatcher must not touch the compose object at all;
    the spy asserts that by tracking ``getattr`` + call history. The
    ``ephemeral`` dispatcher must read ``docker_compose_command`` and
    ``context`` before running its subprocess — the spy records those
    accesses so the ordering can be asserted precisely.
    """

    def __init__(
        self,
        *,
        docker_compose_command: list[str],
        context: str,
    ) -> None:
        self.access_log: list[str] = []
        self._docker_compose_command = docker_compose_command
        self._context = context

    @property
    def docker_compose_command(self) -> list[str]:
        self.access_log.append("docker_compose_command")
        return list(self._docker_compose_command)

    @property
    def context(self) -> str:
        self.access_log.append("context")
        return self._context


class _StrictSpyDockerCompose:
    """Compose stub that refuses every access.

    Used by the ``shared`` dispatcher test — any attribute the dispatcher
    reads raises, so a silent regression that touches the compose is
    caught even without an explicit access assertion.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"SharedDispatcher.cycle must not read compose attribute {name!r}")


class _FakeSubprocessRunner:
    """Records every ``run`` invocation for later assertion.

    The default behaviour is a successful zero-exit; a per-invocation
    ``exc`` can be seeded to raise :class:`subprocess.CalledProcessError`
    so the failure path is exercised too.
    """

    def __init__(self, *, raise_on_verb: str | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self._raise_on_verb = raise_on_verb

    def __call__(
        self,
        cmd: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((tuple(cmd), dict(kwargs)))
        if self._raise_on_verb is not None and self._raise_on_verb in cmd:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr=b"boom",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")


class _FakeRecipeDispatcher:
    """One-shot :class:`RecipeDispatcher` that records its call args.

    Seeded into :data:`RECIPE_REGISTRY` via a fixture so
    :class:`ResetDispatcher.cycle` exercises the real
    :func:`tolokaforge.runtime.reset_recipes.dispatch` code path — the
    only fake is at the leaf. Mutate :attr:`exc` after construction to
    seed the failure path for the next call.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[SeedRef, str, Any]] = []
        self.exc: BaseException | None = None

    def apply(
        self,
        seed: SeedRef,
        service_name: str,
        compose: Any,
    ) -> None:
        self.calls.append((seed, service_name, compose))
        if self.exc is not None:
            raise self.exc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_handle(
    *,
    stack_id: str = "default",
    compose: Any,
    runner_service: str | None = "runner",
) -> _DockerComposeStackHandle:
    """Build a :class:`_DockerComposeStackHandle` around a stub compose.

    Fields that this stage's dispatchers do not read are left at inert
    defaults — dispatchers must fail loud (via the strict spy or an
    attribute lookup) if they ever start reading them.
    """
    return _DockerComposeStackHandle(
        stack_id=stack_id,
        stack_scope="trial",
        runner_service=runner_service,
        compose=compose,
        temp_dir=Path("/tmp/does-not-exist"),
        log_routers=(),
        service_names=("runner",),
    )


def _make_seed(*, path: str = "seed.sql", kind: str = "sql_dump") -> SeedRef:
    return SeedRef.model_validate({"path": path, "kind": kind, "digest": "sha256:" + "0" * 64})


def _reset_spec(seed_name: str = "baseline") -> ServiceSpec:
    return ServiceSpec(isolation="reset", reset=ResetSpec(seed=seed_name))


def _shared_spec() -> ServiceSpec:
    return ServiceSpec(isolation="shared")


def _ephemeral_spec() -> ServiceSpec:
    return ServiceSpec(isolation="ephemeral")


@pytest.fixture
def swap_sql_dump_dispatcher() -> Iterator[_FakeRecipeDispatcher]:
    """Swap the ``sql_dump`` recipe dispatcher for a recording fake.

    Restores the real dispatcher on teardown so subsequent tests see the
    production registry unchanged.
    """
    fake = _FakeRecipeDispatcher()
    original = RECIPE_REGISTRY["sql_dump"]
    RECIPE_REGISTRY["sql_dump"] = fake  # type: ignore[assignment]
    try:
        yield fake
    finally:
        RECIPE_REGISTRY["sql_dump"] = original


# ---------------------------------------------------------------------------
# DISPATCHER_REGISTRY shape
# ---------------------------------------------------------------------------


class TestRegistryShape:
    def test_registry_contains_exactly_three_dispatchers(self) -> None:
        """The built-in registry closes over the :data:`ServiceIsolation`
        Literal — no extras, no gaps. A new label MUST land alongside a
        matching dispatcher registration in the same commit."""
        assert set(DISPATCHER_REGISTRY) == {"shared", "reset", "ephemeral"}

    def test_registry_dispatchers_are_the_expected_types(self) -> None:
        """Each key resolves to the shipped implementation for that label —
        catches a swap or accidental re-assignment at import time."""
        assert isinstance(DISPATCHER_REGISTRY["shared"], SharedDispatcher)
        assert isinstance(DISPATCHER_REGISTRY["reset"], ResetDispatcher)
        assert isinstance(DISPATCHER_REGISTRY["ephemeral"], EphemeralDispatcher)


# ---------------------------------------------------------------------------
# SharedDispatcher
# ---------------------------------------------------------------------------


class TestSharedDispatcher:
    def test_cycle_never_touches_the_compose_object(self) -> None:
        """A ``shared`` service persists across trials by construction —
        the dispatcher must not read any compose attribute or call any
        compose method. The strict spy raises on every access."""
        spy = _StrictSpyDockerCompose()
        handle = _make_handle(compose=spy)
        SharedDispatcher().cycle(
            "db-service",
            _shared_spec(),
            handle,
            materialiser=None,  # type: ignore[arg-type]
            seeds={},
        )

    def test_cycle_returns_none(self) -> None:
        """The Protocol declares :meth:`cycle` returns ``None`` — the
        no-op path must not leak any sentinel."""
        handle = _make_handle(compose=_StrictSpyDockerCompose())
        result = SharedDispatcher().cycle(
            "db-service",
            _shared_spec(),
            handle,
            materialiser=None,  # type: ignore[arg-type]
            seeds={},
        )
        assert result is None


# ---------------------------------------------------------------------------
# ResetDispatcher
# ---------------------------------------------------------------------------


class TestResetDispatcher:
    def test_cycle_dispatches_to_recipe_registry_in_order(
        self,
        swap_sql_dump_dispatcher: _FakeRecipeDispatcher,
    ) -> None:
        """The dispatcher resolves ``seed.kind`` through the real
        :func:`recipe_dispatch` and hands the recipe
        ``(seed, service_name, compose)`` in that order."""
        seed = _make_seed(kind="sql_dump")
        handle = _make_handle(compose=_StrictSpyDockerCompose())
        ResetDispatcher().cycle(
            "db-service",
            _reset_spec(seed_name="baseline"),
            handle,
            materialiser=None,  # type: ignore[arg-type]
            seeds={"baseline": seed},
        )
        assert swap_sql_dump_dispatcher.calls == [
            (seed, "db-service", handle.compose),
        ]

    def test_cycle_refuses_when_seed_map_lacks_referenced_seed(self) -> None:
        """A ``reset`` service pointing at a seed that is not in the
        run's seed map fails loud with the canonical refusal ``reason``
        text (the dispatcher owns this phrasing; the composer supplies
        the outer ``stage`` / ``trial_id``)."""
        handle = _make_handle(compose=_StrictSpyDockerCompose())
        with pytest.raises(ProvisionError) as excinfo:
            ResetDispatcher().cycle(
                "db-service",
                _reset_spec(seed_name="missing"),
                handle,
                materialiser=None,  # type: ignore[arg-type]
                seeds={},
            )
        assert excinfo.value.stage == "cycle"
        assert excinfo.value.reason == (
            "service 'db-service' names seed 'missing' but "
            "the backend has no such seed in its registry "
            "(available: [])."
        )

    def test_cycle_wraps_recipe_runtime_error_as_provision_error(
        self,
        swap_sql_dump_dispatcher: _FakeRecipeDispatcher,
    ) -> None:
        """A :class:`RuntimeError` from the recipe dispatch surfaces as
        :class:`ProvisionError` with ``stage="cycle"`` — the reason
        carries the canonical reset-recipe failure phrasing."""
        seed = _make_seed(kind="sql_dump")
        swap_sql_dump_dispatcher.exc = RuntimeError("dump failed")
        handle = _make_handle(compose=_StrictSpyDockerCompose())
        with pytest.raises(ProvisionError) as excinfo:
            ResetDispatcher().cycle(
                "db-service",
                _reset_spec(seed_name="baseline"),
                handle,
                materialiser=None,  # type: ignore[arg-type]
                seeds={"baseline": seed},
            )
        assert excinfo.value.stage == "cycle"
        assert excinfo.value.reason == (
            "reset recipe for service 'db-service' "
            "(seed 'baseline', kind 'sql_dump') failed: dump failed"
        )


# ---------------------------------------------------------------------------
# EphemeralDispatcher
# ---------------------------------------------------------------------------


class TestEphemeralDispatcher:
    def test_cycle_invokes_docker_rm_then_up(self) -> None:
        """The dispatcher drives ``docker compose rm -f -v <svc>``
        followed by ``docker compose up -d --wait <svc>`` — both with
        the compose's own command prefix and ``cwd`` — in that order."""
        compose = _SpyDockerCompose(
            docker_compose_command=["docker", "compose", "-f", "environment.compose.yaml"],
            context="/tmp/tolokaforge-run-a-XXX",
        )
        handle = _make_handle(compose=compose)
        runner = _FakeSubprocessRunner()
        dispatcher = EphemeralDispatcher(subprocess_runner=runner)

        dispatcher.cycle(
            "worker",
            _ephemeral_spec(),
            handle,
            materialiser=None,  # type: ignore[arg-type]
            seeds={},
        )

        rm_cmd, rm_kwargs = runner.calls[0]
        up_cmd, up_kwargs = runner.calls[1]
        assert rm_cmd == (
            "docker",
            "compose",
            "-f",
            "environment.compose.yaml",
            "rm",
            "-f",
            "-v",
            "worker",
        )
        assert up_cmd == (
            "docker",
            "compose",
            "-f",
            "environment.compose.yaml",
            "up",
            "-d",
            "--wait",
            "worker",
        )
        assert rm_kwargs == {
            "cwd": "/tmp/tolokaforge-run-a-XXX",
            "check": True,
            "capture_output": True,
        }
        assert up_kwargs == rm_kwargs
        assert len(runner.calls) == 2

    def test_cycle_wraps_subprocess_failure_as_provision_error(self) -> None:
        """A non-zero exit on either verb surfaces as
        :class:`ProvisionError` with ``stage="cycle"``; the reason names
        the failing verb and the service."""
        compose = _SpyDockerCompose(
            docker_compose_command=["docker", "compose", "-f", "env.yaml"],
            context="/tmp/x",
        )
        handle = _make_handle(compose=compose, stack_id="scratch")
        runner = _FakeSubprocessRunner(raise_on_verb="rm")
        dispatcher = EphemeralDispatcher(subprocess_runner=runner)

        with pytest.raises(ProvisionError) as excinfo:
            dispatcher.cycle(
                "worker",
                _ephemeral_spec(),
                handle,
                materialiser=None,  # type: ignore[arg-type]
                seeds={},
            )

        assert excinfo.value.stage == "cycle"
        assert "docker compose rm failed" in excinfo.value.reason
        assert "'worker'" in excinfo.value.reason
        assert "'scratch'" in excinfo.value.reason


# ---------------------------------------------------------------------------
# Module-level dispatch helper
# ---------------------------------------------------------------------------


class TestDispatchHelper:
    def test_dispatch_routes_to_registered_dispatcher(self) -> None:
        """The module-level :func:`dispatch` looks up
        :data:`DISPATCHER_REGISTRY` by ``service_spec.isolation`` — the
        ``shared`` label lands on :class:`SharedDispatcher`, which is a
        no-op even against the strict spy."""
        handle = _make_handle(compose=_StrictSpyDockerCompose())
        dispatch(
            "db-service",
            _shared_spec(),
            handle,
            materialiser=None,  # type: ignore[arg-type]
            seeds={},
        )

    def test_dispatch_refuses_unregistered_label(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A hole in the registry surfaces as :class:`ProvisionError`
        with ``stage="cycle"`` — the closed :data:`ServiceIsolation`
        vocab keeps this off the hot path, so the refusal only fires
        when a caller supplies a registry missing a shipped label. The
        test simulates that by deleting ``shared`` from the module
        registry for the call and letting :func:`monkeypatch.delitem`
        restore it on teardown."""
        monkeypatch.delitem(DISPATCHER_REGISTRY, "shared")
        handle = _make_handle(compose=_StrictSpyDockerCompose())
        with pytest.raises(ProvisionError) as excinfo:
            dispatch(
                "db-service",
                _shared_spec(),
                handle,
                materialiser=None,  # type: ignore[arg-type]
                seeds={},
            )
        assert excinfo.value.stage == "cycle"
        assert excinfo.value.reason == ("no dispatcher registered for isolation label 'shared'")

    @pytest.mark.parametrize("label", ["shared", "reset", "ephemeral"])
    def test_isolation_class_attributes_match_registry_keys(self, label: ServiceIsolation) -> None:
        """Each dispatcher advertises its own label via the
        :attr:`isolation` class attribute; the registry keys are
        derived from those advertisements — a mismatch would slip a
        dispatcher under the wrong label at look-up time."""
        assert DISPATCHER_REGISTRY[label].isolation == label
