"""The dispatch-adapter seam: Protocol, deprecated alias, group, build context, errors."""

from __future__ import annotations

import importlib.metadata

import pytest

from tolokaforge.core.dispatch.adapter import DispatchAdapter
from tolokaforge.core.dispatch.errors import (
    DispatchProvisionError,
    DispatchRefused,
    DispatchTransient,
)
from tolokaforge.core.plugin_registry import (
    DISPATCH_ADAPTERS_GROUP,
    DispatchAdapterBuildContext,
    UnknownImplementationError,
    available_dispatch_adapters,
    load_dispatch_adapter,
)
from tolokaforge.core.remote_runtime import RemoteRuntimeBackend
from tolokaforge.core.trial_executor import TrialOwningBackend

pytestmark = pytest.mark.canonical


def test_trial_owning_backend_is_the_dispatch_adapter() -> None:
    assert TrialOwningBackend is DispatchAdapter


def test_remote_backend_satisfies_dispatch_adapter() -> None:
    assert issubclass(RemoteRuntimeBackend, DispatchAdapter)


def test_adapter_protocol_requires_run_level_lifecycle() -> None:
    assert {"run_trial_node", "connect", "close"} <= set(DispatchAdapter.__protocol_attrs__)
    assert all(callable(getattr(RemoteRuntimeBackend, n)) for n in ("connect", "close"))


def test_object_without_lifecycle_is_not_an_adapter() -> None:
    class RunOnly:
        def run_trial_node(self, inputs: object) -> object:
            return inputs

    assert not isinstance(RunOnly(), DispatchAdapter)


def test_object_without_run_trial_node_is_not_an_adapter() -> None:
    assert not isinstance(object(), DispatchAdapter)


def test_group_is_declared_and_has_no_entries_yet() -> None:
    assert DISPATCH_ADAPTERS_GROUP == "tolokaforge.dispatch_adapters"
    assert not list(importlib.metadata.entry_points(group=DISPATCH_ADAPTERS_GROUP))
    assert available_dispatch_adapters() == []


def test_unknown_adapter_fails_loud() -> None:
    with pytest.raises(UnknownImplementationError):
        load_dispatch_adapter("nope")


def test_build_context_defaults_carry_only_dispatch_fields() -> None:
    ctx = DispatchAdapterBuildContext()
    assert ctx.connect_timeout_s == 30.0
    assert ctx.connect_retry_interval_s == 1.0
    assert dict(ctx.config) == {}
    assert not hasattr(ctx, "grpc_max_message_mb")
    with pytest.raises(AttributeError):
        ctx.connect_timeout_s = 1.0  # type: ignore[misc]


@pytest.mark.parametrize("error_cls", [DispatchRefused, DispatchTransient, DispatchProvisionError])
def test_neutral_errors_carry_classification(error_cls: type[RuntimeError]) -> None:
    err = error_cls("Internal", "preflight", "bad thing")  # type: ignore[call-arg]
    assert (err.error_type, err.stage, err.reason) == ("Internal", "preflight", "bad thing")  # type: ignore[attr-defined]
    assert str(err) == "[Internal/preflight] bad thing"
