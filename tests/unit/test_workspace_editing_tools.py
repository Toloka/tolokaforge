"""Tests for workspace editing tools (Phase 1 tooling parity)."""

from pathlib import Path

import pytest

from tolokaforge.tools.builtin.files import (
    AppendFileTool,
    CopyFileTool,
    DeleteFileTool,
    GrepWorkspaceTool,
    MoveFileTool,
    ReadFileTool,
    ReplaceLinesTool,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# read_file enhancements
# ---------------------------------------------------------------------------


class TestReadFileEnhancements:
    def test_read_with_line_numbers(self, tmp_path: Path):
        (tmp_path / "code.py").write_text("a\nb\nc\n", encoding="utf-8")
        tool = ReadFileTool(base_path=str(tmp_path))
        result = tool.execute("code.py", with_line_numbers=True)
        assert result.success
        assert "     1\ta\n" in result.output
        assert "     3\tc\n" in result.output

    def test_read_with_offset_and_limit(self, tmp_path: Path):
        (tmp_path / "data.txt").write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
        tool = ReadFileTool(base_path=str(tmp_path))
        result = tool.execute("data.txt", offset=2, limit=2)
        assert result.success
        assert result.output == "L2\nL3\n"
        assert result.metadata["start_line"] == 2
        assert result.metadata["lines_returned"] == 2
        assert result.metadata["total_lines"] == 5

    def test_read_offset_beyond_file(self, tmp_path: Path):
        (tmp_path / "small.txt").write_text("one\n", encoding="utf-8")
        tool = ReadFileTool(base_path=str(tmp_path))
        result = tool.execute("small.txt", offset=100)
        assert result.success
        assert result.output == ""
        assert result.metadata["lines_returned"] == 0

    def test_read_defaults_unchanged(self, tmp_path: Path):
        (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
        tool = ReadFileTool(base_path=str(tmp_path))
        result = tool.execute("f.txt")
        assert result.success
        assert result.output == "hello\n"


# ---------------------------------------------------------------------------
# replace_lines
# ---------------------------------------------------------------------------


class TestReplaceLines:
    def test_basic_replace(self, tmp_path: Path):
        (tmp_path / "code.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        tool = ReplaceLinesTool(base_path=str(tmp_path))
        result = tool.execute("code.py", old_text="return 1", new_text="return 42")
        assert result.success
        assert (tmp_path / "code.py").read_text() == "def foo():\n    return 42\n"

    def test_replace_not_found(self, tmp_path: Path):
        (tmp_path / "f.txt").write_text("abc", encoding="utf-8")
        tool = ReplaceLinesTool(base_path=str(tmp_path))
        result = tool.execute("f.txt", old_text="xyz", new_text="123")
        assert not result.success
        assert "not found" in result.error

    def test_replace_ambiguous(self, tmp_path: Path):
        (tmp_path / "f.txt").write_text("aaa\naaa\n", encoding="utf-8")
        tool = ReplaceLinesTool(base_path=str(tmp_path))
        result = tool.execute("f.txt", old_text="aaa", new_text="bbb")
        assert not result.success
        assert "2 times" in result.error


# ---------------------------------------------------------------------------
# append_file
# ---------------------------------------------------------------------------


class TestAppendFile:
    def test_append_existing(self, tmp_path: Path):
        (tmp_path / "log.txt").write_text("line1\n", encoding="utf-8")
        tool = AppendFileTool(base_path=str(tmp_path))
        result = tool.execute("log.txt", content="line2\n")
        assert result.success
        assert (tmp_path / "log.txt").read_text() == "line1\nline2\n"

    def test_append_creates_file(self, tmp_path: Path):
        tool = AppendFileTool(base_path=str(tmp_path))
        result = tool.execute("new.txt", content="first\n")
        assert result.success
        assert (tmp_path / "new.txt").read_text() == "first\n"


# ---------------------------------------------------------------------------
# move_file
# ---------------------------------------------------------------------------


class TestMoveFile:
    def test_move_file(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("data", encoding="utf-8")
        tool = MoveFileTool(base_path=str(tmp_path))
        result = tool.execute("a.txt", destination="b.txt")
        assert result.success
        assert not (tmp_path / "a.txt").exists()
        assert (tmp_path / "b.txt").read_text() == "data"


# ---------------------------------------------------------------------------
# copy_file
# ---------------------------------------------------------------------------


class TestCopyFile:
    def test_copy_file(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("data", encoding="utf-8")
        tool = CopyFileTool(base_path=str(tmp_path))
        result = tool.execute("a.txt", destination="b.txt")
        assert result.success
        assert (tmp_path / "a.txt").read_text() == "data"
        assert (tmp_path / "b.txt").read_text() == "data"


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


class TestDeleteFile:
    def test_delete_file(self, tmp_path: Path):
        (tmp_path / "doomed.txt").write_text("bye", encoding="utf-8")
        tool = DeleteFileTool(base_path=str(tmp_path))
        result = tool.execute("doomed.txt")
        assert result.success
        assert not (tmp_path / "doomed.txt").exists()


# ---------------------------------------------------------------------------
# grep_workspace
# ---------------------------------------------------------------------------


class TestGrepWorkspace:
    def _setup_workspace(self, tmp_path: Path):
        (tmp_path / "hello.py").write_text("def hello():\n    print('hi')\n", encoding="utf-8")
        (tmp_path / "world.txt").write_text("hello world\ngoodbye world\n", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("import os\n", encoding="utf-8")

    def test_content_search(self, tmp_path: Path):
        self._setup_workspace(tmp_path)
        tool = GrepWorkspaceTool(base_path=str(tmp_path))
        result = tool.execute(pattern="hello")
        assert result.success
        assert "hello.py:1:" in result.output
        assert "world.txt:1:" in result.output

    def test_file_glob_only(self, tmp_path: Path):
        self._setup_workspace(tmp_path)
        tool = GrepWorkspaceTool(base_path=str(tmp_path))
        result = tool.execute(file_glob="*.py")
        assert result.success
        assert "hello.py" in result.output
        assert "deep.py" in result.output
        assert "world.txt" not in result.output
