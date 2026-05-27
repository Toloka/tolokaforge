"""Adapter-side contract for builtin tool config plumbing (#110).

Native adapter must lift ``tools.agent.<tool_name>: {...}`` blocks from
``task.yaml`` into ``ToolSchema.tool_config`` so the runner can splat
them into the tool class constructor at trial registration. Builtins
keep ``source = None`` to preserve the runner's source-less dispatch
arm (otherwise the dispatch routes to ``_create_tau_sync_wrapper`` and
fails with ImportError — the original bug in PR #117 v1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "tasks"


@pytest.fixture
def mobile_adapter() -> NativeAdapter:
    return NativeAdapter(
        {
            "tasks_glob": "synth_mobile_01/task.yaml",
            "base_dir": str(_DATA_DIR),
        }
    )


def test_mobile_schema_carries_apps_in_tool_config(mobile_adapter):
    td = mobile_adapter.to_task_description("synth_mobile_01")
    mobile = next(t for t in td.agent_tools if t.name == "mobile")

    assert mobile.source is None
    assert mobile.tool_config == {
        "apps": {
            "CityMap": "http://mock-web:8080/task/mobile/app_citymap/",
            "Notepad": "http://mock-web:8080/task/mobile/app_notepad/",
        },
        "initial_app": "CityMap",
    }


def test_builtin_without_per_tool_config_has_empty_tool_config(mobile_adapter):
    """``read_file`` is enabled but has no ``tools.agent.read_file:`` block
    — its tool_config must be empty, not absent or None."""
    td = mobile_adapter.to_task_description("synth_mobile_01")
    read_file = next(t for t in td.agent_tools if t.name == "read_file")
    assert read_file.source is None
    assert read_file.tool_config == {}


def test_mobile_parameter_schema_is_rich_not_empty(mobile_adapter):
    """``MobileTool.get_schema`` returns a rich parameter schema that
    enumerates the configured apps under ``actions[].app_name.enum``."""
    td = mobile_adapter.to_task_description("synth_mobile_01")
    mobile = next(t for t in td.agent_tools if t.name == "mobile")
    actions = mobile.parameters["properties"]["actions"]
    array_branch = next(b for b in actions["anyOf"] if b.get("type") == "array")
    app_name_enum = array_branch["items"]["properties"]["app_name"]["enum"]
    assert set(app_name_enum) == {"CityMap", "Notepad"}


def test_non_dict_per_tool_config_raises():
    """``tools.agent.mobile: true`` is a YAML typo (the user meant a
    mapping). Surface the error at TaskDescription-build time."""
    adapter = NativeAdapter(
        {
            "tasks_glob": "bad_mobile/task.yaml",
            "base_dir": str(_DATA_DIR),
        }
    )
    with pytest.raises(ValueError, match="tools.agent.mobile must be a mapping"):
        adapter.to_task_description("bad_mobile")
