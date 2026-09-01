"""Built-in entry-point registrations for the swappable seams.

Snapshots the registration table tolokaforge ships in its own ``pyproject.toml``:
the built-in names resolve through the fail-loud loader to factories that build
the right impl, the ``available_*`` listings match the ADR-locked set, and the
raw ``importlib.metadata`` probe from the acceptance criterion sees the
runtime-backend and readiness-probe names. Canonical tier because it reads
*installed* package metadata (``uv sync`` / the CI install step registers the
entry points).
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.actors.turn_policy import AgentOnlyTurnPolicy, ConversationalTurnPolicy
from tolokaforge.core.composition_runtime import (
    ComposeMaterialiser,
    SubstrateComposer,
)
from tolokaforge.core.conductor import (
    ConductorContext,
    InMemoryConductor,
    InProcessConductor,
)
from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer
from tolokaforge.core.docker_compose_materialiser import DockerComposeMaterialiser
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.plugin_registry import (
    COMPOSE_MATERIALISERS_GROUP,
    RUNTIME_BACKENDS_GROUP,
    SERVICE_LIFECYCLE_DISPATCHERS_GROUP,
    SERVICE_READINESS_PROBES_GROUP,
    SUBSTRATE_COMPOSERS_GROUP,
    TURN_POLICIES_GROUP,
    RuntimeBackendBuildContext,
    TrialGraderContext,
    TurnPolicyContext,
    UnknownImplementationError,
    available_compose_materialisers,
    available_conductors,
    available_readiness_probes,
    available_runtime_backends,
    available_service_lifecycle_dispatchers,
    available_substrate_composers,
    available_trial_graders,
    available_turn_policies,
    load_compose_materialiser,
    load_conductor,
    load_readiness_probe,
    load_runtime_backend,
    load_service_lifecycle_dispatcher,
    load_substrate_composer,
    load_trial_grader,
    load_turn_policy,
)
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.service_lifecycle_dispatchers import (
    EphemeralDispatcher,
    ResetDispatcher,
    SharedDispatcher,
)
from tolokaforge.core.service_readiness import (
    GrpcReadinessProbe,
    HttpReadinessProbe,
    TcpReadinessProbe,
)
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial_grader import RunnerRPCTrialGrader

pytestmark = pytest.mark.canonical


def _runtime_backend_context() -> RuntimeBackendBuildContext:
    return RuntimeBackendBuildContext(
        runner_address="runner:50051",
        env_manifest=None,
        run_id="run",
        seeds={},
        log_capture=None,
    )


def _conductor_context() -> ConductorContext:
    return ConductorContext(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=MagicMock(),
        logger=MagicMock(),
        verbose=False,
        strict=False,
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        trial_grader=MagicMock(),
        output_dir=Path("/tmp"),
        request_limiter=None,
    )


@pytest.mark.parametrize(
    ("name", "expected_cls"),
    [
        ("shared", SharedStackRuntimeBackend),
        ("per_trial", PerTrialRuntimeBackend),
        ("in_memory", InMemoryRuntimeBackend),
    ],
)
def test_runtime_backend_names_resolve_to_their_class(name: str, expected_cls: type) -> None:
    backend = load_runtime_backend(name)(_runtime_backend_context())
    assert isinstance(backend, expected_cls)


def test_trial_grader_name_resolves_to_its_class() -> None:
    import tolokaforge.core.shared_stack_runtime as ssr

    # The built-in ``runner_rpc`` factory constructs a ``GrpcRunnerClient``
    # from ``ctx.runner_address``; stub the client to avoid a real socket.
    class _StubClient:
        def __init__(self, runner_address: str) -> None:
            self.runner_address = runner_address

    original = ssr.GrpcRunnerClient
    ssr.GrpcRunnerClient = _StubClient  # type: ignore[misc,assignment]
    try:
        ctx = TrialGraderContext(runner_address="stub:0", logger=MagicMock())
        grader = load_trial_grader("runner_rpc")(ctx)
    finally:
        ssr.GrpcRunnerClient = original  # type: ignore[misc]
    assert isinstance(grader, RunnerRPCTrialGrader)


@pytest.mark.parametrize(
    ("name", "expected_cls"),
    [
        ("in_process", InProcessConductor),
        ("in_memory", InMemoryConductor),
    ],
)
def test_conductor_names_resolve_to_their_class(name: str, expected_cls: type) -> None:
    conductor = load_conductor(name)(_conductor_context())
    assert isinstance(conductor, expected_cls)


@pytest.mark.parametrize(
    ("kind", "expected_cls"),
    [
        ("grpc", GrpcReadinessProbe),
        ("http", HttpReadinessProbe),
        ("tcp", TcpReadinessProbe),
    ],
)
def test_readiness_probe_kinds_resolve_to_their_class(kind: str, expected_cls: type) -> None:
    probe = load_readiness_probe(kind)()
    assert isinstance(probe, expected_cls)


def test_turn_policy_names_resolve_to_their_class() -> None:
    context = TurnPolicyContext(user_simulator=MagicMock())
    conversational = load_turn_policy("conversational")(context)
    assert isinstance(conversational, ConversationalTurnPolicy)

    agent_only = load_turn_policy("agent_only")(TurnPolicyContext(user_simulator=None))
    assert isinstance(agent_only, AgentOnlyTurnPolicy)


def test_available_listings_match_the_builtin_set() -> None:
    assert available_runtime_backends() == ["in_memory", "per_trial", "shared"]
    assert available_trial_graders() == ["grader_rpc", "judge_only", "queue", "runner_rpc"]
    assert available_conductors() == ["in_memory", "in_process"]
    assert available_readiness_probes() == ["grpc", "http", "tcp"]
    assert available_turn_policies() == ["agent_only", "conversational"]
    assert available_compose_materialisers() == ["docker_compose"]
    assert available_service_lifecycle_dispatchers() == ["ephemeral", "reset", "shared"]
    assert available_substrate_composers() == ["default"]


def test_raw_entry_point_probe_lists_runtime_backends() -> None:
    names = sorted(ep.name for ep in importlib.metadata.entry_points(group=RUNTIME_BACKENDS_GROUP))
    assert names == ["in_memory", "per_trial", "shared"]


def test_raw_entry_point_probe_lists_readiness_probes() -> None:
    names = sorted(
        ep.name for ep in importlib.metadata.entry_points(group=SERVICE_READINESS_PROBES_GROUP)
    )
    assert names == ["grpc", "http", "tcp"]


def test_raw_entry_point_probe_lists_turn_policies() -> None:
    names = sorted(ep.name for ep in importlib.metadata.entry_points(group=TURN_POLICIES_GROUP))
    assert names == ["agent_only", "conversational"]


def test_raw_entry_point_probe_lists_compose_materialisers() -> None:
    names = sorted(
        ep.name for ep in importlib.metadata.entry_points(group=COMPOSE_MATERIALISERS_GROUP)
    )
    assert names == ["docker_compose"]


def test_raw_entry_point_probe_lists_service_lifecycle_dispatchers() -> None:
    names = sorted(
        ep.name for ep in importlib.metadata.entry_points(group=SERVICE_LIFECYCLE_DISPATCHERS_GROUP)
    )
    assert names == ["ephemeral", "reset", "shared"]


def test_raw_entry_point_probe_lists_substrate_composers() -> None:
    names = sorted(
        ep.name for ep in importlib.metadata.entry_points(group=SUBSTRATE_COMPOSERS_GROUP)
    )
    assert names == ["default"]


@pytest.mark.parametrize(
    ("name", "expected_cls"),
    [("docker_compose", DockerComposeMaterialiser)],
)
def test_compose_materialiser_names_resolve_to_their_class(name: str, expected_cls: type) -> None:
    cls = load_compose_materialiser(name)
    assert cls is expected_cls
    instance = cls()
    assert isinstance(instance, ComposeMaterialiser)


@pytest.mark.parametrize(
    ("name", "expected_cls"),
    [
        ("shared", SharedDispatcher),
        ("reset", ResetDispatcher),
        ("ephemeral", EphemeralDispatcher),
    ],
)
def test_service_lifecycle_dispatcher_names_resolve_to_their_class(
    name: str, expected_cls: type
) -> None:
    cls = load_service_lifecycle_dispatcher(name)
    assert cls is expected_cls
    instance = cls()
    # ``isolation`` is a ClassVar naming the label the dispatcher owns; the
    # ADR-0044 registry pairs the entry-point name with that label.
    assert instance.isolation == name
    assert callable(instance.cycle)


@pytest.mark.parametrize(
    ("name", "expected_cls"),
    [("default", DefaultSubstrateComposer)],
)
def test_substrate_composer_names_resolve_to_their_class(name: str, expected_cls: type) -> None:
    cls = load_substrate_composer(name)
    assert cls is expected_cls
    instance = cls()
    assert isinstance(instance, SubstrateComposer)


@pytest.mark.parametrize(
    "loader",
    [
        load_compose_materialiser,
        load_service_lifecycle_dispatcher,
        load_substrate_composer,
    ],
)
def test_composition_seam_unknown_name_raises(loader) -> None:
    with pytest.raises(UnknownImplementationError):
        loader("this_name_is_not_registered")
