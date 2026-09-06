"""Locks ``RuntimeConnectConfig`` schema — Pydantic bounds + defaults.

The block is a small operator surface for the runner health-check budget
during connect. Defaults preserve the pre-block behaviour (30 s wall,
1 s retry interval); ``extra="forbid"`` catches typos; both fields require
strictly positive floats — a zero or negative value would either loop
forever (retry_interval = 0) or refuse before the first attempt (timeout
= 0).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.models.run_config import OrchestratorConfig, RuntimeConnectConfig

pytestmark = pytest.mark.unit


def test_defaults_preserve_pre_block_behaviour() -> None:
    cfg = RuntimeConnectConfig()
    assert cfg.timeout_s == 30.0
    assert cfg.retry_interval_s == 1.0


def test_orchestrator_config_default_includes_runtime_connect_defaults() -> None:
    cfg = OrchestratorConfig()
    assert cfg.runtime_connect.timeout_s == 30.0
    assert cfg.runtime_connect.retry_interval_s == 1.0


def test_extra_key_refuses_typo() -> None:
    with pytest.raises(ValidationError) as exc_info:
        RuntimeConnectConfig(timeout_seconds=45.0)  # typo of timeout_s
    assert "timeout_seconds" in str(exc_info.value)


@pytest.mark.parametrize("field", ["timeout_s", "retry_interval_s"])
@pytest.mark.parametrize("value", [0.0, -1.0, -0.001])
def test_non_positive_value_refuses(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        RuntimeConnectConfig(**{field: value})


def test_operator_can_raise_timeout_and_shorten_retry_interval() -> None:
    cfg = RuntimeConnectConfig(timeout_s=90.0, retry_interval_s=0.5)
    assert cfg.timeout_s == 90.0
    assert cfg.retry_interval_s == 0.5
