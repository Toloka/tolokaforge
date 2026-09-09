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

from tolokaforge.adapters._task_loader import _builtin_tool_schemas, load_task_yaml
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


# ---------------------------------------------------------------------------
# ToolPolicy.output_max_chars — adapter bridge
# ---------------------------------------------------------------------------


class _StubCappedTool:
    """Stub builtin whose ``policy.output_max_chars`` names a per-tool cap."""

    def __init__(self) -> None:
        from tolokaforge.tools.registry import ToolPolicy

        self.policy = ToolPolicy(output_max_chars=512)

    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "stub_capped",
                "description": "A capped stub tool.",
                "parameters": {"type": "object", "properties": {}},
            },
        }


def test_builtin_tool_schemas_lifts_output_max_chars_from_policy(monkeypatch):
    """A builtin whose policy names a per-tool cap surfaces the value on the
    rich schema ``_builtin_tool_schemas`` returns, so ``_actor_tool_schemas``
    can lift it onto the emitted ``ToolSchema``.
    """
    from tolokaforge.tools.builtin import registry as builtin_registry

    monkeypatch.setattr(builtin_registry, "is_builtin", lambda name: name == "stub_capped")
    monkeypatch.setattr(builtin_registry, "get_class", lambda name: _StubCappedTool)

    schemas = _builtin_tool_schemas(["stub_capped"])

    assert schemas["stub_capped"]["output_max_chars"] == 512


def test_builtin_tool_schemas_lifts_none_for_a_policy_without_a_cap():
    """A shipped builtin whose policy leaves ``output_max_chars`` at the
    default ``None`` surfaces ``None`` on the rich schema. Locks the default
    path: no per-tool cap declared, no per-tool cap threaded.
    """
    schemas = _builtin_tool_schemas(["read_file"])

    assert schemas["read_file"]["output_max_chars"] is None


def test_native_adapter_emits_output_max_chars_on_the_wire_toolschema(monkeypatch, tmp_path: Path):
    """The full adapter path: a builtin whose ``ToolPolicy.output_max_chars``
    is 512 surfaces on the ``ToolSchema`` NativeAdapter emits into the wire
    payload the runner will send back at ``RegisterTrial``.
    """
    from tolokaforge.tools.builtin import registry as builtin_registry

    monkeypatch.setattr(builtin_registry, "is_builtin", lambda name: name == "stub_capped")
    monkeypatch.setattr(builtin_registry, "get_class", lambda name: _StubCappedTool)

    task_dir = tmp_path / "capped_task"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "capped_task",
                "name": "capped task",
                "description": "one builtin with a policy cap",
                "category": "compute",
                "max_turns": 2,
                "interaction_mode": "conversational",
                "initial_user_message": "poll",
                "initial_state": {},
                "tools": {"agent": {"enabled": ["stub_capped"]}, "user": {"enabled": []}},
                "actors": {"user": {"mode": "llm"}},
            }
        )
    )
    adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(tmp_path)})
    td = adapter.to_task_description("capped_task")

    stub = next(t for t in td.agent_tools if t.name == "stub_capped")
    assert stub.output_max_chars == 512


# ---------------------------------------------------------------------------
# tools.<actor>.<tool_name>.output_max_chars — task-yaml override
# ---------------------------------------------------------------------------


class _StubUncappedTool:
    """Stub builtin whose ``policy`` leaves ``output_max_chars`` at ``None``."""

    def __init__(self) -> None:
        from tolokaforge.tools.registry import ToolPolicy

        self.policy = ToolPolicy()

    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "stub_uncapped",
                "description": "An uncapped stub tool.",
                "parameters": {"type": "object", "properties": {}},
            },
        }


def _task_dir_with_stub_block(tmp_path: Path, tool_name: str, block: dict) -> Path:
    """Write a minimal one-tool task.yaml under ``tmp_path`` and return its parent."""
    task_dir = tmp_path / "override_task"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "override_task",
                "name": "override task",
                "description": "one builtin, one block",
                "category": "compute",
                "max_turns": 2,
                "interaction_mode": "conversational",
                "initial_user_message": "poll",
                "initial_state": {},
                "tools": {
                    "agent": {"enabled": [tool_name], tool_name: block},
                    "user": {"enabled": []},
                },
                "actors": {"user": {"mode": "llm"}},
            }
        )
    )
    return tmp_path


@pytest.mark.parametrize(
    ("task_yaml_cap", "tool_name", "expected_emitted_cap"),
    [
        # (task_yaml_cap, tool_stub_name, expected emitted ToolSchema.output_max_chars)
        (None, "stub_uncapped", None),  # neither axis set
        (None, "stub_capped", 512),  # only tool-declared
        (512, "stub_uncapped", 512),  # only task-yaml
        (256, "stub_capped", 256),  # task-yaml tighter
        (1024, "stub_capped", 512),  # tool-declared tighter
        (512, "stub_capped", 512),  # equal, stable
    ],
    ids=[
        "neither_set",
        "only_tool_declared",
        "only_task_yaml",
        "task_yaml_tighter",
        "tool_declared_tighter",
        "equal",
    ],
)
def test_native_adapter_composes_task_yaml_and_tool_declared_output_max_chars(
    monkeypatch,
    tmp_path: Path,
    task_yaml_cap: int | None,
    tool_name: str,
    expected_emitted_cap: int | None,
):
    """The emitted ``ToolSchema.output_max_chars`` is
    ``min(task_yaml_cap, tool_declared_cap)`` when both are set, whichever is
    set alone otherwise, ``None`` when neither is set. Locks the composition
    at ``native._actor_tool_schemas``.
    """
    from tolokaforge.tools.builtin import registry as builtin_registry

    classes = {"stub_capped": _StubCappedTool, "stub_uncapped": _StubUncappedTool}
    monkeypatch.setattr(builtin_registry, "is_builtin", lambda name: name in classes)
    monkeypatch.setattr(builtin_registry, "get_class", lambda name: classes[name])

    block: dict = {}
    if task_yaml_cap is not None:
        block["output_max_chars"] = task_yaml_cap
    base_dir = _task_dir_with_stub_block(tmp_path, tool_name, block)
    adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(base_dir)})
    td = adapter.to_task_description("override_task")

    tool = next(t for t in td.agent_tools if t.name == tool_name)
    assert tool.output_max_chars == expected_emitted_cap


def test_search_kb_task_yaml_override_composes_with_the_canonical_schema(tmp_path: Path):
    """``search_kb`` takes a different construction path
    (``create_search_kb_schema()`` bypasses ``_builtin_tool_schemas``), but
    the task-yaml override still composes with the schema's own
    ``output_max_chars`` under the same tighter-wins rule the generic
    branch applies. Locks the third-axis contract for the special-cased
    tool so a future refactor cannot silently drop the override there.
    """
    base_dir = _task_dir_with_stub_block(tmp_path, "search_kb", {"output_max_chars": 256})
    adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(base_dir)})
    td = adapter.to_task_description("override_task")

    tool = next(t for t in td.agent_tools if t.name == "search_kb")
    assert tool.output_max_chars == 256


def test_search_kb_without_override_preserves_canonical_schema(tmp_path: Path):
    """Absent an override, ``search_kb`` emits whatever ``create_search_kb_schema``
    declares — today ``None``. Pins the neither-axis-set row for the special
    branch so a canonical-schema change lights up here first.
    """
    base_dir = _task_dir_with_stub_block(tmp_path, "search_kb", {})
    adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(base_dir)})
    td = adapter.to_task_description("override_task")

    tool = next(t for t in td.agent_tools if t.name == "search_kb")
    assert tool.output_max_chars is None


def test_tool_configs_strips_output_max_chars_and_keeps_other_kwargs(tmp_path: Path):
    """``tool_configs`` returns a mapping the runner can splat verbatim: the
    reserved ``output_max_chars`` key never appears, and other kwargs pass
    through unchanged.
    """
    from tolokaforge.adapters._task_loader import ToolActor, tool_configs

    base_dir = _task_dir_with_stub_block(
        tmp_path, "stub", {"other_kwarg": "x", "output_max_chars": 512}
    )
    task, _ = load_task_yaml(base_dir / "override_task" / "task.yaml")

    configs = tool_configs(task, ToolActor.AGENT)

    assert configs == {"stub": {"other_kwarg": "x"}}
    assert "output_max_chars" not in configs.get("stub", {})


def test_tool_configs_drops_entry_when_block_names_only_reserved_keys(tmp_path: Path):
    """A block that declares nothing but reserved keys yields no entry in
    :func:`tool_configs` — the runner receives no unknown kwargs to reject.
    """
    from tolokaforge.adapters._task_loader import ToolActor, tool_configs

    base_dir = _task_dir_with_stub_block(tmp_path, "stub", {"output_max_chars": 512})
    task, _ = load_task_yaml(base_dir / "override_task" / "task.yaml")

    configs = tool_configs(task, ToolActor.AGENT)

    assert "output_max_chars" not in configs.get("stub", {})


@pytest.mark.parametrize(
    "bad_value",
    [0, -1, 3.14, "512", True, False],
    ids=["zero", "negative", "float", "string", "bool_true", "bool_false"],
)
def test_tool_output_max_chars_override_rejects_non_positive_int(tmp_path: Path, bad_value):
    """A non-int, non-positive, or bool ``output_max_chars`` fails loud at
    authoring time rather than reaching the wire as a nonsense cap.
    """
    from tolokaforge.adapters._task_loader import (
        ToolActor,
        tool_output_max_chars_overrides,
    )

    base_dir = _task_dir_with_stub_block(tmp_path, "stub", {"output_max_chars": bad_value})
    task, _ = load_task_yaml(base_dir / "override_task" / "task.yaml")

    with pytest.raises(ValueError, match=r"tools\.agent\.stub\.output_max_chars"):
        tool_output_max_chars_overrides(task, ToolActor.AGENT)


# ---------------------------------------------------------------------------
# ToolPolicy.timeout_s — adapter bridge
# ---------------------------------------------------------------------------


class _StubShortTimeoutTool:
    """Stub builtin whose ``policy.timeout_s`` declares a tight per-tool budget."""

    def __init__(self) -> None:
        from tolokaforge.tools.registry import ToolPolicy

        self.policy = ToolPolicy(timeout_s=5.0)

    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "stub_short_timeout",
                "description": "A stub tool with a tight timeout.",
                "parameters": {"type": "object", "properties": {}},
            },
        }


def test_builtin_tool_schemas_lifts_timeout_s_from_policy(monkeypatch):
    """A builtin whose ``ToolPolicy`` declares a per-tool ``timeout_s`` surfaces
    the value on the rich schema ``_builtin_tool_schemas`` returns, so
    ``_actor_tool_schemas`` can lift it onto the emitted ``ToolSchema``.
    """
    from tolokaforge.tools.builtin import registry as builtin_registry

    monkeypatch.setattr(builtin_registry, "is_builtin", lambda name: name == "stub_short_timeout")
    monkeypatch.setattr(builtin_registry, "get_class", lambda name: _StubShortTimeoutTool)

    schemas = _builtin_tool_schemas(["stub_short_timeout"])

    assert schemas["stub_short_timeout"]["timeout_s"] == 5.0


def test_builtin_tool_schemas_lifts_default_when_policy_is_default():
    """A shipped builtin whose ``ToolPolicy`` leaves ``timeout_s`` at the class
    default (30.0) surfaces 30.0 on the rich schema, so the adapter emits
    ``ToolSchema.timeout_s == 30.0`` — matching the ``ToolPolicy.timeout_s``
    field default. ``bash`` is the canonical example.
    """
    schemas = _builtin_tool_schemas(["bash"])

    assert schemas["bash"]["timeout_s"] == 30.0


def test_native_adapter_emits_declared_timeout_s_on_the_wire_toolschema(
    monkeypatch, tmp_path: Path
):
    """The full adapter path: a builtin whose ``ToolPolicy.timeout_s`` is 5.0
    surfaces on the ``ToolSchema`` NativeAdapter emits into the wire payload the
    runner will send back at ``RegisterTrial``.
    """
    from tolokaforge.tools.builtin import registry as builtin_registry

    monkeypatch.setattr(builtin_registry, "is_builtin", lambda name: name == "stub_short_timeout")
    monkeypatch.setattr(builtin_registry, "get_class", lambda name: _StubShortTimeoutTool)

    task_dir = tmp_path / "short_timeout_task"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "short_timeout_task",
                "name": "short-timeout task",
                "description": "one builtin with a tight policy timeout",
                "category": "compute",
                "max_turns": 2,
                "interaction_mode": "conversational",
                "initial_user_message": "poll",
                "initial_state": {},
                "tools": {
                    "agent": {"enabled": ["stub_short_timeout"]},
                    "user": {"enabled": []},
                },
                "actors": {"user": {"mode": "llm"}},
            }
        )
    )
    adapter = NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(tmp_path)})
    td = adapter.to_task_description("short_timeout_task")

    stub = next(t for t in td.agent_tools if t.name == "stub_short_timeout")
    assert stub.timeout_s == 5.0
