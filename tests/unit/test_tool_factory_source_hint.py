"""Runner-side raise for a source-less non-builtin tool carries an actionable hint.

Locks the four content properties on ``ToolConfigurationError.message`` at
``tool_factory.py``: the tool name, the ``evaluation.harness_adapter`` config
key, the alternate ``tools.<actor>.mcp_server`` fix, and the enumerated
registered adapter names. Also locks the broad-catch fallback so a broken
``available_adapters()`` cannot mask the primary raise.
"""

from __future__ import annotations

import pytest

from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import ToolConfigurationError, ToolFactory

pytestmark = pytest.mark.unit


def _schema(name: str) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=f"stub tool {name}",
        parameters={"type": "object", "properties": {}},
        category="compute",
        timeout_s=30.0,
        source=None,
    )


def test_source_less_non_builtin_raises_actionable_hint() -> None:
    factory = ToolFactory(db_client=None, trial_id="t-repro")

    with pytest.raises(ToolConfigurationError) as exc_info:
        factory._create_wrapper(_schema("foreign_tool_x"))

    message = exc_info.value.message
    assert "foreign_tool_x" in message
    assert "evaluation.harness_adapter" in message
    assert "tools." in message
    assert "mcp_server" in message
    # The registered-adapter enumeration is always present; on a bare install
    # only "native" is guaranteed, but the token must appear.
    assert "native" in message


def test_builtin_tool_never_raises_source_hint() -> None:
    factory = ToolFactory(db_client=None, trial_id="t-repro")

    wrapper = factory._create_wrapper(_schema("bash"))

    assert wrapper is not None
    assert wrapper.name == "bash"


def test_hint_enumerates_registered_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tolokaforge.adapters.available_adapters",
        lambda: ["native", "fake_plugin_a", "fake_plugin_b"],
    )

    factory = ToolFactory(db_client=None, trial_id="t-repro")

    with pytest.raises(ToolConfigurationError) as exc_info:
        factory._create_wrapper(_schema("foreign_tool_x"))

    message = exc_info.value.message
    for adapter in ("native", "fake_plugin_a", "fake_plugin_b"):
        assert adapter in message


def test_hint_falls_back_when_adapter_discovery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> list[str]:
        raise RuntimeError("discovery blew up")

    monkeypatch.setattr("tolokaforge.adapters.available_adapters", _boom)

    factory = ToolFactory(db_client=None, trial_id="t-repro")

    with pytest.raises(ToolConfigurationError) as exc_info:
        factory._create_wrapper(_schema("foreign_tool_x"))

    message = exc_info.value.message
    # Primary raise is not masked; hint still carries the fallback adapter list
    # and the two required config-key tokens.
    assert "foreign_tool_x" in message
    assert "native" in message
    assert "evaluation.harness_adapter" in message
    assert "mcp_server" in message
