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
