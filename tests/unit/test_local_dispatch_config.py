"""Unit tests for the ``local`` dispatch adapter's config validation."""

from __future__ import annotations

import pytest

from tolokaforge.core.dispatch.local import LocalDispatchAdapter, _LocalConfig, _parse_config
from tolokaforge.core.plugin_registry import DispatchAdapterBuildContext, load_dispatch_adapter

pytestmark = pytest.mark.unit


def test_defaults_to_thread_mode_one_worker() -> None:
    config = _LocalConfig()
    assert (config.mode, config.workers) == ("thread", 1)


def test_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="thread"):
        _parse_config({"mode": "cluster"})


def test_rejects_a_non_positive_worker_count() -> None:
    with pytest.raises(ValueError, match="workers"):
        _parse_config({"mode": "thread", "workers": 0})


def test_rejects_an_unknown_config_key() -> None:
    with pytest.raises(ValueError, match="local"):
        _parse_config({"mode": "thread", "bogus": 1})


def test_factory_builds_a_thread_adapter() -> None:
    adapter = load_dispatch_adapter("local")(
        DispatchAdapterBuildContext(config={"mode": "thread", "workers": 3})
    )
    assert isinstance(adapter, LocalDispatchAdapter)
    assert (adapter.mode, adapter.workers) == ("thread", 3)
    assert adapter.registry is None and adapter.grpc_adapter is None


def test_factory_builds_a_subprocess_adapter_threading_the_budget_and_message_bound() -> None:
    adapter = load_dispatch_adapter("local")(
        DispatchAdapterBuildContext(
            connect_timeout_s=9.0,
            connect_retry_interval_s=0.25,
            config={"mode": "subprocess", "workers": 2, "grpc_max_message_mb": 11},
        )
    )
    assert isinstance(adapter, LocalDispatchAdapter)
    assert (adapter.mode, adapter.workers) == ("subprocess", 2)
    assert adapter.registry is not None and adapter.grpc_adapter is not None
    assert adapter.grpc_adapter.grpc_max_message_mb == 11
    assert adapter.grpc_adapter.connect_timeout_s == 9.0
    assert adapter.grpc_adapter.retry_interval_s == 0.25
