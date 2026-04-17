"""Unit tests for file manipulation and bash tool execution."""

from pathlib import Path

import pytest

from tolokaforge.tools.builtin.bash import BashTool
from tolokaforge.tools.builtin.files import ReadFileTool, WriteFileTool

pytestmark = pytest.mark.unit


def test_read_file_supports_legacy_env_alias(tmp_path: Path):
    (tmp_path / "problem.txt").write_text("42", encoding="utf-8")
    tool = ReadFileTool(base_path=str(tmp_path))

    result = tool.execute("/env/fs/agent-visible/problem.txt")

    assert result.success is True
    assert result.output == "42"


def test_write_file_supports_work_alias(tmp_path: Path):
    tool = WriteFileTool(base_path=str(tmp_path))

    result = tool.execute("/work/submissions/report.md", "hello")

    assert result.success is True
    assert (tmp_path / "submissions" / "report.md").read_text(encoding="utf-8") == "hello"


def test_bash_tool_uses_configured_workdir(tmp_path: Path):
    (tmp_path / "sample.txt").write_text("ok", encoding="utf-8")
    tool = BashTool(workdir=tmp_path)

    result = tool.execute("cat /work/sample.txt")

    assert result.success is True
    assert "ok" in result.output
