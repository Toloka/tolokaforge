"""Runner-side raise for a source-less non-builtin tool carries an actionable hint.

Locks the four content properties on ``ToolConfigurationError.message`` at
``tool_factory.py``: the tool name, the ``evaluation.harness_adapter`` config
key, the alternate ``tools.<actor>.mcp_server`` fix, and the current fallback
phrasing. The runner subset cannot import ``tolokaforge.adapters`` (partition
enforced by :mod:`tests.canonical.test_runner_subset_partition`), so the
registered-adapter enumeration lives on the emit-time raise at
:class:`NativeAdapter` (see :mod:`tests.unit.adapters.test_native_adapter_source_hint`).
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


def test_builtin_tool_never_raises_source_hint() -> None:
    factory = ToolFactory(db_client=None, trial_id="t-repro")

    wrapper = factory._create_wrapper(_schema("bash"))

    assert wrapper is not None
    assert wrapper.name == "bash"
