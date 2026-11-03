"""Tests for BuiltinFileToolWrapper and builtin tool schema resolution."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.adapters._task_loader import (
    BUILTIN_MUTATES_STATE_BY_CATEGORY,
    _builtin_tool_schemas,
    builtin_mutates_state,
)
from tolokaforge.core.tool_effects import ToolEffects
from tolokaforge.runner.models import MutatesStateRule, ToolSchema
from tolokaforge.tools.builtin import registry as builtin_registry
from tolokaforge.tools.builtin.http_request import SAFE_METHODS_READ
from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

# ---------------------------------------------------------------------------
# _builtin_tool_schemas helper
# ---------------------------------------------------------------------------


_READ_RULE = {"read_when": {"args": {"command": {"regex": "^ls"}}}}


def _stub_tool_class(policy: ToolPolicy) -> type[Tool]:
    class _Stub(Tool):
        def __init__(self) -> None:
            super().__init__(name="stub", description="stub", policy=policy)

        def get_schema(self) -> dict:
            return {"type": "function", "function": {"name": "stub", "parameters": {}}}

        def execute(self, **kwargs: object) -> ToolResult:
            raise NotImplementedError

    return _Stub


class TestBuiltinToolSchemas:
    """Tests for the module-level _builtin_tool_schemas helper."""

    @pytest.mark.unit
    def test_read_file_schema_has_path_parameter(self):
        schemas = _builtin_tool_schemas(["read_file"])
        assert "read_file" in schemas
        params = schemas["read_file"]["parameters"]
        assert "path" in params["properties"]
        assert "path" in params.get("required", [])

    @pytest.mark.unit
    def test_write_file_schema_has_path_and_content(self):
        schemas = _builtin_tool_schemas(["write_file"])
        assert "write_file" in schemas
        params = schemas["write_file"]["parameters"]
        assert "path" in params["properties"]
        assert "content" in params["properties"]
        assert "path" in params.get("required", [])
        assert "content" in params.get("required", [])

    @pytest.mark.unit
    def test_json_db_schemas_survive_and_name_jsonpath_for_the_update_path(self):
        schemas = _builtin_tool_schemas(["db_query", "db_update"])
        assert set(schemas) == {"db_query", "db_update"}
        op_item = schemas["db_update"]["parameters"]["properties"]["ops"]["items"]
        assert (
            "JSONPath, e.g. `$.tickets[0].status`" in op_item["properties"]["path"]["description"]
        )

    @pytest.mark.unit
    def test_unknown_tool_is_skipped(self):
        schemas = _builtin_tool_schemas(["nonexistent_tool"])
        assert "nonexistent_tool" not in schemas

    @pytest.mark.unit
    def test_mixed_known_and_unknown(self):
        schemas = _builtin_tool_schemas(["read_file", "nonexistent", "write_file"])
        assert set(schemas.keys()) == {"read_file", "write_file"}

    @pytest.mark.unit
    def test_empty_list(self):
        schemas = _builtin_tool_schemas([])
        assert schemas == {}

    @pytest.mark.unit
    def test_description_is_nonempty_string(self):
        schemas = _builtin_tool_schemas(["read_file", "write_file"])
        for _name, info in schemas.items():
            assert isinstance(info["description"], str)
            assert len(info["description"]) > 0

    @pytest.mark.unit
    def test_mutates_state_is_the_policy_category_read_as_a_declaration(self):
        """ADR-0061: the engine authors its builtins, so ``ToolPolicy.category`` is
        the declaration — ``read`` never mutates, ``write`` does, and ``compute``
        (a shell, an HTTP client) says nothing and stays off the wire, exactly
        like an MCP tool with no ``readOnlyHint``.
        """
        schemas = _builtin_tool_schemas(
            ["read_file", "write_file", "db_query", "db_update", "bash"]
        )

        assert schemas["read_file"]["mutates_state"] is False
        assert schemas["db_query"]["mutates_state"] is False
        assert schemas["write_file"]["mutates_state"] is True
        assert schemas["db_update"]["mutates_state"] is True
        assert "mutates_state" not in schemas["bash"]

    @pytest.mark.unit
    def test_every_category_has_a_deliberate_mutates_state_row(self):
        """A category added to the enum must be mapped on purpose, not guessed."""
        assert set(BUILTIN_MUTATES_STATE_BY_CATEGORY) == set(ToolCategory)
        assert builtin_mutates_state(ToolPolicy(category=ToolCategory.READ), tool_name="t") is False
        assert builtin_mutates_state(ToolPolicy(category=ToolCategory.WRITE), tool_name="t") is True
        assert (
            builtin_mutates_state(ToolPolicy(category=ToolCategory.COMPUTE), tool_name="t") is None
        )

    @pytest.mark.unit
    def test_http_request_declares_its_safe_methods_as_reads(self):
        """RFC 9110's safe methods read; every other method, and a method spelled in
        another case, can change state."""
        declared = _builtin_tool_schemas(["http_request"])["http_request"]["mutates_state"]

        assert declared == MutatesStateRule.model_validate(SAFE_METHODS_READ)
        effects = ToolEffects.from_schemas(
            [ToolSchema(name="http_request", description="", parameters={}, mutates_state=declared)]
        )
        reads = {
            method: not effects.can_change_state("http_request", json.dumps({"method": method}))
            for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE", "get")
        }
        assert reads == {
            "GET": True,
            "HEAD": True,
            "OPTIONS": True,
            "POST": False,
            "PUT": False,
            "PATCH": False,
            "DELETE": False,
            "get": False,
        }

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("category", "declared", "emitted"),
        [
            (ToolCategory.READ, False, False),
            (ToolCategory.WRITE, True, True),
            (ToolCategory.COMPUTE, False, False),
            (ToolCategory.COMPUTE, True, True),
            (ToolCategory.COMPUTE, _READ_RULE, MutatesStateRule.model_validate(_READ_RULE)),
        ],
        ids=["read-false", "write-true", "compute-false", "compute-true", "compute-rule"],
    )
    def test_a_policy_declaration_the_category_admits_is_the_declaration(
        self, category: ToolCategory, declared: bool | dict, emitted: object
    ):
        policy = ToolPolicy(category=category, mutates_state=declared)

        assert builtin_mutates_state(policy, tool_name="t") == emitted

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("category", "declared"),
        [
            (ToolCategory.READ, True),
            (ToolCategory.READ, _READ_RULE),
            (ToolCategory.WRITE, False),
            (ToolCategory.WRITE, _READ_RULE),
        ],
        ids=["read-true", "read-rule", "write-false", "write-rule"],
    )
    def test_a_policy_declaration_contradicting_its_category_fails_the_schema(
        self, monkeypatch: pytest.MonkeyPatch, category: ToolCategory, declared: bool | dict
    ):
        """Raised out of schema assembly, not swallowed as an unloadable builtin."""
        policy = ToolPolicy(category=category, mutates_state=declared)
        monkeypatch.setattr(builtin_registry, "is_builtin", lambda name: name == "stub")
        monkeypatch.setattr(builtin_registry, "get_class", lambda name: _stub_tool_class(policy))

        with pytest.raises(ValueError, match=r"builtin tool 'stub' is a '\w+' tool, which"):
            _builtin_tool_schemas(["stub"])

    @pytest.mark.unit
    def test_a_policy_rule_the_rule_model_refuses_fails_the_schema(self):
        policy = ToolPolicy(
            mutates_state={"read_when": {"args": {"method": {"equals_binding": "m"}}}}
        )

        with pytest.raises(ValueError, match="tool 'stub' in the builtin's ToolPolicy"):
            builtin_mutates_state(policy, tool_name="stub")


# ---------------------------------------------------------------------------
# BuiltinFileToolWrapper
# ---------------------------------------------------------------------------


class TestBuiltinFileToolWrapper:
    """Tests for BuiltinFileToolWrapper creation and execution."""

    @pytest.mark.unit
    def test_create_read_file_wrapper(self):
        from tolokaforge.runner.tool_factory import BuiltinFileToolWrapper

        schema = MagicMock()
        schema.name = "read_file"
        wrapper = BuiltinFileToolWrapper(schema)
        assert wrapper._tool.__class__.__name__ == "ReadFileTool"

    @pytest.mark.unit
    def test_create_write_file_wrapper(self):
        from tolokaforge.runner.tool_factory import BuiltinFileToolWrapper

        schema = MagicMock()
        schema.name = "write_file"
        wrapper = BuiltinFileToolWrapper(schema)
        assert wrapper._tool.__class__.__name__ == "WriteFileTool"

    @pytest.mark.unit
    def test_unsupported_tool_raises(self):
        from tolokaforge.runner.tool_factory import BuiltinFileToolWrapper, ToolConfigurationError

        schema = MagicMock()
        schema.name = "unknown_tool"
        with pytest.raises(ToolConfigurationError):
            BuiltinFileToolWrapper(schema)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_read_file_execute(self):
        from tolokaforge.runner.tool_factory import BuiltinFileToolWrapper

        schema = MagicMock()
        schema.name = "read_file"
        wrapper = BuiltinFileToolWrapper(schema)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world")
            tmp_path = f.name

        # Set base_path to the parent of our temp file
        wrapper._tool.base_path = Path(tmp_path).parent

        result = await wrapper.execute({"path": Path(tmp_path).name})
        assert "hello world" in result
        Path(tmp_path).unlink()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_write_file_execute(self):
        from tolokaforge.runner.tool_factory import BuiltinFileToolWrapper

        schema = MagicMock()
        schema.name = "write_file"
        wrapper = BuiltinFileToolWrapper(schema)

        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper._tool.base_path = Path(tmpdir)
            result = await wrapper.execute({"path": "test.txt", "content": "written content"})
            assert "success" in result.lower() or "written" in result.lower()
            written = (Path(tmpdir) / "test.txt").read_text()
            assert written == "written content"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_raises_on_tool_failure(self):
        """Builtin tool failures must raise so the runner records EXECUTION_STATUS_ERROR.

        Previously the wrapper returned ``f"Error: {result.error}"`` on
        failure, which the runner saw as a success string. That corrupted
        ``tool_success_rate``, ``failure_attribution``, and ``error_count``.
        """
        from tolokaforge.runner.tool_factory import (
            BuiltinFileToolWrapper,
            ToolExecutionError,
        )
        from tolokaforge.tools.registry import ToolResult

        schema = MagicMock()
        schema.name = "read_file"
        wrapper = BuiltinFileToolWrapper(schema)

        # Force the underlying tool to return failure.
        failing_tool = MagicMock()
        failing_tool.execute.return_value = ToolResult(
            success=False, output="", error="Path traversal blocked"
        )
        wrapper._tool = failing_tool

        with pytest.raises(ToolExecutionError) as exc_info:
            await wrapper.execute({"path": "../../etc/passwd"})

        assert exc_info.value.tool_name == "read_file"
        assert "Path traversal blocked" in exc_info.value.message

    @pytest.mark.unit
    def test_create_list_dir_wrapper(self):
        from tolokaforge.runner.tool_factory import BuiltinFileToolWrapper

        schema = MagicMock()
        schema.name = "list_dir"
        wrapper = BuiltinFileToolWrapper(schema)
        assert wrapper._tool.__class__.__name__ == "ListDirTool"

    @pytest.mark.unit
    def test_runner_file_tools_use_work_base_path(self):
        """All three file tools must target /work — not the library default of
        /env/fs/agent-visible — so they see what the runner provisions and what
        BashTool writes.
        """
        from pathlib import Path

        from tolokaforge.runner.tool_factory import BuiltinFileToolWrapper

        for name in ("read_file", "write_file", "list_dir"):
            schema = MagicMock()
            schema.name = name
            wrapper = BuiltinFileToolWrapper(schema)
            assert wrapper._tool.base_path == Path(
                "/work"
            ), f"{name} wrapper must use /work base_path, got {wrapper._tool.base_path}"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_raises_with_default_message_when_error_missing(self):
        from tolokaforge.runner.tool_factory import (
            BuiltinFileToolWrapper,
            ToolExecutionError,
        )
        from tolokaforge.tools.registry import ToolResult

        schema = MagicMock()
        schema.name = "write_file"
        wrapper = BuiltinFileToolWrapper(schema)

        failing_tool = MagicMock()
        failing_tool.execute.return_value = ToolResult(success=False, output="", error="")
        wrapper._tool = failing_tool

        with pytest.raises(ToolExecutionError) as exc_info:
            await wrapper.execute({"path": "x", "content": "y"})

        assert "Tool returned failure" in exc_info.value.message
