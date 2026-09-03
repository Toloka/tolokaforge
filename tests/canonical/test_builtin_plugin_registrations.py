"""Built-in entry-point registrations for the four swappable seams.

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
from tolokaforge.core.conductor import (
    ConductorContext,
    InMemoryConductor,
    InProcessConductor,
)
from tolokaforge.core.grading.bundle_store import LocalDiskBundleStore, S3BundleStore
from tolokaforge.core.grading.grading_method import (
    CompositeGradingMethod,
    TestExecutionGradingMethod,
)
from tolokaforge.core.grading.kinds import CompositeGraderKind, TestExecutionGraderKind
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.plugin_registry import (
    BUNDLE_STORES_GROUP,
    GRADER_KINDS_GROUP,
    GRADING_METHODS_GROUP,
    RUNTIME_BACKENDS_GROUP,
    SERVICE_READINESS_PROBES_GROUP,
    TURN_POLICIES_GROUP,
    RuntimeBackendBuildContext,
    TrialGraderContext,
    TurnPolicyContext,
    available_bundle_stores,
    available_conductors,
    available_grader_kinds,
    available_grading_methods,
    available_readiness_probes,
    available_runtime_backends,
    available_trial_graders,
    available_turn_policies,
    load_bundle_store,
    load_conductor,
    load_grader_kind,
    load_grading_method,
    load_readiness_probe,
    load_runtime_backend,
    load_trial_grader,
    load_turn_policy,
)
from tolokaforge.core.runtime import InMemoryRuntimeBackend
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


@pytest.mark.parametrize(
    ("name", "expected_cls"),
    [
        ("composite", CompositeGradingMethod),
        ("test_execution", TestExecutionGradingMethod),
    ],
)
def test_grading_method_names_resolve_to_their_marker(name: str, expected_cls: type) -> None:
    marker = load_grading_method(name)
    assert marker is expected_cls
    assert name == marker.NAME


@pytest.mark.parametrize(
    ("name", "expected_cls"),
    [
        ("composite", CompositeGraderKind),
        ("test_execution", TestExecutionGraderKind),
    ],
)
def test_grader_kind_names_resolve_to_their_class(name: str, expected_cls: type) -> None:
    kind_cls = load_grader_kind(name)
    assert kind_cls is expected_cls
    assert name == kind_cls.NAME


@pytest.mark.parametrize(
    ("name", "expected_cls"),
    [
        ("local_disk", LocalDiskBundleStore),
        ("s3", S3BundleStore),
    ],
)
def test_bundle_store_name_resolves_to_its_class(name: str, expected_cls: type) -> None:
    assert load_bundle_store(name) is expected_cls


def test_available_listings_match_the_builtin_set() -> None:
    assert available_runtime_backends() == ["in_memory", "per_trial", "shared"]
    assert available_trial_graders() == ["grader_rpc", "judge_only", "queue", "runner_rpc"]
    assert available_conductors() == ["in_memory", "in_process"]
    assert available_readiness_probes() == ["grpc", "http", "tcp"]
    assert available_turn_policies() == ["agent_only", "conversational"]
    assert available_grading_methods() == ["composite", "test_execution"]
    assert available_grader_kinds() == ["composite", "test_execution"]
    assert available_bundle_stores() == ["local_disk", "s3"]


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


def test_raw_entry_point_probe_lists_grading_methods() -> None:
    names = sorted(ep.name for ep in importlib.metadata.entry_points(group=GRADING_METHODS_GROUP))
    assert names == ["composite", "test_execution"]


def test_raw_entry_point_probe_lists_grader_kinds() -> None:
    names = sorted(ep.name for ep in importlib.metadata.entry_points(group=GRADER_KINDS_GROUP))
    assert names == ["composite", "test_execution"]


def test_raw_entry_point_probe_lists_bundle_stores() -> None:
    names = sorted(ep.name for ep in importlib.metadata.entry_points(group=BUNDLE_STORES_GROUP))
    assert names == ["local_disk", "s3"]
