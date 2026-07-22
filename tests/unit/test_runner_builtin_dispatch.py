"""Runner-side contract for builtin tool reconstruction (#110, #121).

``ToolFactory._create_wrapper`` must:
- route source-less builtins to the right wrapper class via the
  unified registry (no hardcoded name tuples / dicts);
- splat ``ToolSchema.tool_config`` into the tool's ``__init__``;
- raise ``ToolConfigurationError`` listing accepted kwargs when
  ``tool_config`` contains an unknown key — typos must not be
  silently dropped.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    BuiltinFileToolWrapper,
    BuiltinGenericToolWrapper,
    PersistentShellToolWrapper,
    ToolConfigurationError,
    ToolFactory,
)

pytestmark = pytest.mark.unit

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "tasks"


@pytest.fixture
def factory() -> ToolFactory:
    return ToolFactory(MagicMock(), trial_id="test-trial")


def test_calculator_routes_to_generic_wrapper_with_no_kwargs(factory):
    schema = ToolSchema(
        name="calculator",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    wrapper = factory._create_wrapper(schema)
    assert isinstance(wrapper, BuiltinGenericToolWrapper)


def test_read_file_routes_to_file_wrapper(factory):
    schema = ToolSchema(
        name="read_file",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    wrapper = factory._create_wrapper(schema)
    assert isinstance(wrapper, BuiltinFileToolWrapper)


def test_bash_session_routes_to_persistent_shell_wrapper(factory):
    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    wrapper = factory._create_wrapper(schema)
    assert isinstance(wrapper, PersistentShellToolWrapper)
    assert wrapper.has_lifecycle is True


def test_mobile_end_to_end_through_native_adapter(factory):
    """The full chain that broke in PR #117 v1: NativeAdapter →
    ToolFactory._create_wrapper → BuiltinGenericToolWrapper with
    MobileTool.apps populated."""
    adapter = NativeAdapter(
        {
            "tasks_glob": "synth_mobile_01/task.yaml",
            "base_dir": str(_DATA_DIR),
        }
    )
    td = adapter.to_task_description("synth_mobile_01")
    mobile_schema = next(t for t in td.agent_tools if t.name == "mobile")

    wrapper = factory._create_wrapper(mobile_schema)
    assert isinstance(wrapper, BuiltinGenericToolWrapper)

    from tolokaforge.tools.builtin.mobile import MobileTool

    assert isinstance(wrapper._tool, MobileTool)
    assert wrapper._tool.apps == {
        "CityMap": "http://mock-web:8080/task/mobile/app_citymap/",
        "Notepad": "http://mock-web:8080/task/mobile/app_notepad/",
    }


def test_unknown_tool_config_key_raises_clearly(factory):
    """Misnamed kwargs must surface with a helpful error listing
    accepted kwargs."""
    schema = ToolSchema(
        name="mobile",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config={
            "apps": {"CityMap": "http://example/"},
            "intial_app": "CityMap",  # typo
        },
    )
    with pytest.raises(ToolConfigurationError) as exc_info:
        factory._create_wrapper(schema)
    msg = str(exc_info.value)
    assert "intial_app" in msg
    assert "initial_app" in msg
    assert "mobile" in msg


def test_calculator_with_any_tool_config_key_raises(factory):
    """CalculatorTool takes no kwargs — anything in tool_config must raise."""
    schema = ToolSchema(
        name="calculator",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config={"precision": 4},
    )
    with pytest.raises(ToolConfigurationError) as exc_info:
        factory._create_wrapper(schema)
    assert "precision" in str(exc_info.value)
