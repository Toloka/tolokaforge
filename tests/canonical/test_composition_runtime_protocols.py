"""Pin the composition-plan adapter Protocol contract (ADR-0044).

The three adapter families (:class:`ComposeMaterialiser`,
:class:`ServiceLifecycleDispatcher`, :class:`SubstrateComposer`) are the
seam #1381/#1382/#1383 build against — a silent widening of any Protocol
method set would rewrite the contract without anyone noticing. Snapshot
the method names + companion dataclass shape here so every future edit
either updates this file or fails loud.
"""

from __future__ import annotations

import dataclasses
import typing

import pytest

from tolokaforge.core.composition_runtime import (
    ComposedEnvHandle,
    ComposeMaterialiser,
    CompositionPlan,
    EnvHandle,
    MaterialiseContext,
    MaterialiseLogCapture,
    RunCtx,
    RunSubstrate,
    ServiceLifecycleDispatcher,
    StackDecl,
    StackHandle,
    SubstrateComposer,
    WriteComposeEnv,
)

pytestmark = pytest.mark.canonical


def _public_methods(cls: type) -> frozenset[str]:
    """Non-underscore method names on ``cls`` (``dir`` filters out
    class-level attribute annotations without defaults, so this is a
    method-only view). Widening the Protocol with a new method surfaces
    here."""

    return frozenset(name for name in dir(cls) if not name.startswith("_"))


def _annotated_class_attrs(cls: type) -> frozenset[str]:
    """Non-underscore class-level type-annotated attributes on ``cls``.
    Widening the Protocol with a new class attribute (e.g. a marker like
    ``name`` on the materialiser) surfaces here."""

    return frozenset(name for name in typing.get_type_hints(cls) if not name.startswith("_"))


# ---------------------------------------------------------------------------
# @runtime_checkable — every Protocol must accept isinstance()
# ---------------------------------------------------------------------------


class TestProtocolsAreRuntimeCheckable:
    """``@runtime_checkable`` is the entry point for the structural
    conformance check :class:`ComposedEnvHandle` relies on and future
    tests will lean on to swap in fake materialisers."""

    @pytest.mark.parametrize(
        "protocol",
        [StackHandle, ComposeMaterialiser, ServiceLifecycleDispatcher, SubstrateComposer],
    )
    def test_protocol_is_runtime_checkable(self, protocol: type) -> None:
        is_runtime_checkable = getattr(protocol, "_is_runtime_protocol", False)
        assert is_runtime_checkable, f"{protocol.__name__} must be @runtime_checkable"


# ---------------------------------------------------------------------------
# Method-name snapshots — lock the Protocol surfaces
# ---------------------------------------------------------------------------


class TestComposeMaterialiserSurface:
    """Snapshot the :class:`ComposeMaterialiser` method set + class
    attributes. Widening this Protocol without updating the snapshot is
    the failure a stage 4 edit could silently ship — the plan calls this
    out explicitly."""

    def test_method_names_are_exactly(self) -> None:
        assert _public_methods(ComposeMaterialiser) == frozenset(
            {
                "materialise",
                "resolve_endpoint",
                "get_containers",
                "capture_logs",
                "teardown",
            }
        )

    def test_class_attributes_are_exactly(self) -> None:
        assert _annotated_class_attrs(ComposeMaterialiser) == frozenset({"name"})


class TestServiceLifecycleDispatcherSurface:
    def test_method_names_are_exactly(self) -> None:
        assert _public_methods(ServiceLifecycleDispatcher) == frozenset({"cycle"})

    def test_class_attributes_are_exactly(self) -> None:
        assert _annotated_class_attrs(ServiceLifecycleDispatcher) == frozenset({"isolation"})


class TestSubstrateComposerSurface:
    def test_method_names_are_exactly(self) -> None:
        assert _public_methods(SubstrateComposer) == frozenset(
            {
                "materialise_run",
                "provision_trial",
                "cycle_between_trials",
                "teardown_trial",
                "teardown_run",
                "runner_client_for",
                "endpoints_for",
            }
        )

    def test_no_class_attributes(self) -> None:
        assert _annotated_class_attrs(SubstrateComposer) == frozenset()


class TestStackHandleSurface:
    def test_no_methods(self) -> None:
        assert _public_methods(StackHandle) == frozenset()

    def test_class_attributes_are_exactly(self) -> None:
        assert _annotated_class_attrs(StackHandle) == frozenset(
            {"stack_id", "stack_scope", "runner_service"}
        )


# ---------------------------------------------------------------------------
# Companion dataclass shape — frozen + expected fields
# ---------------------------------------------------------------------------


class TestCompanionDataclassesAreFrozen:
    """Frozen for the immutable ones so the composer can share instances
    across trials without a copy; the mutable :class:`RunSubstrate` is
    the exception because task-scope handles accumulate on it."""

    @pytest.mark.parametrize(
        "cls",
        [
            MaterialiseLogCapture,
            WriteComposeEnv,
            MaterialiseContext,
            RunCtx,
            ComposedEnvHandle,
        ],
    )
    def test_class_is_frozen_dataclass(self, cls: type) -> None:
        assert dataclasses.is_dataclass(cls), f"{cls.__name__} is not a dataclass"
        is_frozen = cls.__dataclass_params__.frozen
        assert is_frozen is True, f"{cls.__name__} must be a frozen dataclass"

    def test_run_substrate_is_mutable_dataclass(self) -> None:
        """Task-scope stacks materialise lazily — the composer records
        each new handle onto :attr:`RunSubstrate.task_stack_handles`.
        The dataclass must therefore be mutable."""

        assert dataclasses.is_dataclass(RunSubstrate)
        assert RunSubstrate.__dataclass_params__.frozen is False


class TestCompanionDataclassFields:
    def test_materialise_log_capture_fields(self) -> None:
        assert [f.name for f in dataclasses.fields(MaterialiseLogCapture)] == ["dest_dir", "tail"]

    def test_write_compose_env_fields(self) -> None:
        assert [f.name for f in dataclasses.fields(WriteComposeEnv)] == [
            "trial_id",
            "stack_inputs",
        ]

    def test_materialise_context_fields(self) -> None:
        assert [f.name for f in dataclasses.fields(MaterialiseContext)] == [
            "scope_key",
            "stack_id",
            "network_policy",
            "limited_internet_allowlist",
            "restricted_services",
            "mount_docker_socket",
            "log_capture",
            "write_compose_env",
            "events",
            "component_id_prefix",
            "bridged_services",
            "stripped_container_secrets",
        ]

    def test_run_ctx_fields(self) -> None:
        assert [f.name for f in dataclasses.fields(RunCtx)] == [
            "run_id",
            "manifest",
            "mount_docker_socket",
            "log_capture",
            "events",
            "seeds",
        ]

    def test_run_substrate_fields(self) -> None:
        """The three trailing fields — :attr:`mount_docker_socket`,
        :attr:`log_capture`, :attr:`events` — carry run-wide policy
        that :meth:`SubstrateComposer.provision_trial` reads when it
        materialises task-scope and trial-scope stacks. Threading them
        via the substrate keeps :class:`SubstrateComposer` from
        re-reading :class:`RunCtx` on every per-trial call."""

        assert [f.name for f in dataclasses.fields(RunSubstrate)] == [
            "run_id",
            "run_stack_handles",
            "task_stack_handles",
            "runner_client",
            "endpoints",
            "seeds",
            "mount_docker_socket",
            "log_capture",
            "events",
        ]

    def test_composed_env_handle_fields(self) -> None:
        assert [f.name for f in dataclasses.fields(ComposedEnvHandle)] == [
            "trial_id",
            "trial_stack_handles",
            "trial_endpoints",
            "trial_runner_client",
        ]


# ---------------------------------------------------------------------------
# EnvHandle structural conformance
# ---------------------------------------------------------------------------


class TestComposedEnvHandleSatisfiesEnvHandle:
    """The backend consumes :class:`ComposedEnvHandle` where
    :class:`~tolokaforge.core.runtime.EnvHandle` is expected. The
    ``@runtime_checkable`` isinstance check is the mechanism — this test
    locks the mechanism at the seam."""

    def test_instance_satisfies_env_handle(self) -> None:
        handle = ComposedEnvHandle(
            trial_id="t1",
            trial_stack_handles=(),
            trial_endpoints=None,
            trial_runner_client=None,
        )
        assert isinstance(handle, EnvHandle)


# ---------------------------------------------------------------------------
# CompositionPlan alias — resolved list of StackDecl
# ---------------------------------------------------------------------------


class TestCompositionPlanAlias:
    def test_alias_is_list_of_stack_decl(self) -> None:
        assert typing.get_origin(CompositionPlan) is list
        assert typing.get_args(CompositionPlan) == (StackDecl,)
