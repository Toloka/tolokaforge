"""Tests for BuiltinFileToolWrapper and builtin tool schema resolution."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.adapters.native import _builtin_tool_schemas

# ---------------------------------------------------------------------------
# _builtin_tool_schemas helper
# ---------------------------------------------------------------------------


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
