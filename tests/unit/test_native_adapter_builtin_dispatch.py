"""Adapter-side contract for builtin tool config plumbing (#110).

Native adapter must lift ``tools.agent.<tool_name>: {...}`` blocks from
``task.yaml`` into ``ToolSchema.tool_config`` so the runner can splat
them into the tool class constructor at trial registration. Builtins
keep ``source = None`` to preserve the runner's source-less dispatch
arm (otherwise the dispatch routes to ``_create_tau_sync_wrapper`` and
fails with ImportError — the original bug in PR #117 v1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters._task_loader import _builtin_tool_schemas
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.runner.models import InvocationStyle

pytestmark = pytest.mark.unit

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "tasks"
_PERSISTENT_PACK = Path(__file__).resolve().parents[2] / "examples" / "native" / "persistent_tools"


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


@pytest.fixture
def two_actor_adapter(tmp_path: Path) -> NativeAdapter:
    """A pack declaring one builtin for the agent and a different one for the user.

    Written to a temp dir rather than committed: every pack in the tree declares
    ``tools.user.enabled: []``, and one that did not would be the only pack in the
    corpus sweeps whose user block carries a name.
    """
    task_dir = tmp_path / "two_actor_task"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "two_actor_task",
                "name": "two actor task",
                "description": "one builtin per actor",
                "category": "compute",
                "max_turns": 2,
                "interaction_mode": "conversational",
                "initial_user_message": "check my arithmetic",
                "initial_state": {},
                "tools": {
                    "agent": {"enabled": ["read_file"]},
                    "user": {"enabled": ["calculator"]},
                },
                "actors": {"user": {"mode": "llm"}},
            }
        )
    )
    return NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(tmp_path)})


def test_a_declared_user_tool_reaches_the_wire_as_a_source_less_builtin(two_actor_adapter):
    """``tools.user.enabled`` is a declaration the adapter honours, not a comment.

    The user's builtin is built exactly as the agent's is — ``source=None`` for the
    runner's source-less dispatch arm, and the registry's real parameters rather
    than the empty-object placeholder — and neither actor's block leaks into the
    other's list.
    """
    td = two_actor_adapter.to_task_description("two_actor_task")

    assert [t.name for t in td.agent_tools] == ["read_file"]
    assert [t.name for t in td.user_tools] == ["calculator"]

    calculator = td.user_tools[0]
    assert calculator.source is None
    assert set(calculator.parameters["properties"]) == {"expression"}
    assert calculator.parameters["required"] == ["expression"]
    assert calculator.tool_config == {}


def test_a_user_block_naming_an_mcp_server_carries_its_source_and_its_script(tmp_path: Path):
    """The MCP arm of the user's block is built exactly as the agent's is.

    The tool carries the server script relative to the task dir, and the pack's
    files are bundled onto the wire — without the bundle the runner has no script
    to reconstruct the tool from, and a source-carrying schema would fail at
    registration inside the container.
    """
    task_dir = tmp_path / "user_mcp_task"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "mcp_server.py").write_text("")
    (task_dir / "fixtures" / "tools.json").write_text(
        json.dumps(
            [
                {
                    "name": "check_device",
                    "description": "read the device",
                    "parameters": {"type": "object", "properties": {"device_id": {}}},
                }
            ]
        )
    )
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "user_mcp_task",
                "name": "user mcp task",
                "description": "the user's tools come from an MCP server",
                "category": "support",
                "max_turns": 2,
                "interaction_mode": "conversational",
                "initial_user_message": "my device is off",
                "initial_state": {},
                "tools": {
                    "agent": {"enabled": []},
                    "user": {"enabled": ["check_device"], "mcp_server": "mcp_server.py"},
                },
                "actors": {"user": {"mode": "llm"}},
            }
        )
    )
    adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(tmp_path)})

    td = adapter.to_task_description("user_mcp_task")
    tool = td.user_tools[0]

    assert tool.name == "check_device"
    assert tool.description == "read the device"
    assert tool.source is not None
    assert tool.source.mcp_server_script == "mcp_server.py"
    assert tool.source.invocation_style == InvocationStyle.MCP_SERVER
    assert "mcp_server.py" in td.tool_artifacts


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


@pytest.fixture
def persistent_tools_adapter() -> NativeAdapter:
    from tolokaforge.core.project_loader import load_project_config

    project = load_project_config(_PERSISTENT_PACK / "project.yaml")
    defaults = project.task_defaults.model_dump(exclude_defaults=True) or None
    return NativeAdapter(
        {
            "tasks_glob": "dataset/tasks/**/task.yaml",
            "base_dir": str(_PERSISTENT_PACK),
            "project_task_defaults": defaults,
        }
    )


def _agent_tool(td, name: str):
    return next(t for t in td.agent_tools if t.name == name)


def test_persistent_pack_advertises_bash_session_schema(persistent_tools_adapter):
    """The example pack's ``bash_session: {}`` config must load through the
    adapter as a source-less builtin advertising the full wire schema."""
    td = persistent_tools_adapter.to_task_description("persistent_tools_public_example_01")
    bash = _agent_tool(td, "bash_session")

    assert bash.source is None
    props = bash.parameters["properties"]
    assert set(props) == {"command", "restart"}
    assert props["command"]["type"] == "string"
    assert props["restart"]["type"] == "boolean"
    assert bash.parameters["required"] == []


def test_persistent_pack_advertises_str_replace_editor_schema(persistent_tools_adapter):
    """The editor's full parameter set (including the four-command enum and
    the insert-specific ``insert_text``) must reach the built ToolSchema."""
    td = persistent_tools_adapter.to_task_description("persistent_tools_public_example_01")
    editor = _agent_tool(td, "str_replace_editor")

    assert editor.source is None
    props = editor.parameters["properties"]
    assert set(props) == {
        "command",
        "path",
        "view_range",
        "file_text",
        "old_str",
        "new_str",
        "insert_line",
        "insert_text",
    }
    assert props["command"]["enum"] == ["view", "create", "str_replace", "insert"]
    assert editor.parameters["required"] == ["command", "path"]
    # ``insert`` carries its text in ``insert_text`` — not ``new_str`` (that
    # is the ``str_replace`` replacement). Both must be advertised distinctly.
    assert "insert_text" in props
    assert "new_str" in props


def test_builtin_schemas_survive_compose_tool_config():
    """``_builtin_tool_schemas`` must extract full schemas for both tools even
    under a compose ``tool_config`` — guards against a schema-extraction
    failure being swallowed and a tool shipping an empty schema (#577)."""
    compose_configs = {
        "bash_session": {"service": "main", "compose_project_prefix": "trial-xyz"},
        "str_replace_editor": {"service": "main", "compose_project_prefix": "trial-xyz"},
    }
    schemas = _builtin_tool_schemas(["bash_session", "str_replace_editor"], compose_configs)

    assert set(schemas["bash_session"]["parameters"]["properties"]) == {"command", "restart"}
    editor_props = schemas["str_replace_editor"]["parameters"]["properties"]
    assert set(editor_props) == {
        "command",
        "path",
        "view_range",
        "file_text",
        "old_str",
        "new_str",
        "insert_line",
        "insert_text",
    }
    assert editor_props["command"]["enum"] == ["view", "create", "str_replace", "insert"]
