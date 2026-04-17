"""Unit tests for tolokaforge/core/grading/judge.py — LLM Judge module.

Covers: path sandboxing, core tool executors, tool pack assembly,
JSON response parsing, and LLMJudge grading (single-call + agentic).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.grading.judge import (
    _CORE_EXECUTORS,
    _CORE_TOOL_DEFS,
    _OFFICE_TOOL_DEFS,
    _TOOL_PACKS,
    LLMJudge,
    _exec_glob_files,
    _exec_grep_workspace,
    _exec_list_files,
    _exec_read_file,
    _exec_run_shell,
    _parse_json_response,
    _safe_resolve,
    get_judge_tools,
)
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Create a small workspace with test files."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "readme.txt").write_text("Hello world\nLine 2\nLine 3\nLine 4\nLine 5\n")
    (ws / "data.json").write_text('{"key": "value"}')
    sub = ws / "subdir"
    sub.mkdir()
    (sub / "nested.py").write_text("# Python file\nprint('hi')\n")
    return ws


# ===================================================================
# _safe_resolve
# ===================================================================


@pytest.mark.unit
class TestSafeResolve:
    """Tests for path sandboxing helper."""

    def test_valid_relative_path(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        result = _safe_resolve(ws, "file.txt")
        assert result is not None
        assert str(result).startswith(str(ws.resolve()))

    def test_subdir_path(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "sub").mkdir()
        result = _safe_resolve(ws, "sub/file.txt")
        assert result is not None

    def test_traversal_outside_workspace_returns_none(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        result = _safe_resolve(ws, "../../etc/passwd")
        assert result is None

    def test_dot_path_resolves_to_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        result = _safe_resolve(ws, ".")
        assert result is not None


# ===================================================================
# _exec_list_files
# ===================================================================


@pytest.mark.unit
class TestExecListFiles:
    """Tests for list_files core tool executor."""

    def test_lists_files_in_workspace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_list_files(ws, {"directory": "."})
        assert "readme.txt" in result
        assert "data.json" in result

    def test_lists_subdirectory(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_list_files(ws, {"directory": "subdir"})
        assert "nested.py" in result

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_list_files(ws, {"directory": "no_such_dir"})
        assert "not found" in result.lower()

    def test_empty_directory(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        (ws / "empty").mkdir()
        result = _exec_list_files(ws, {"directory": "empty"})
        assert "empty" in result.lower()

    def test_outside_workspace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_list_files(ws, {"directory": "../../"})
        assert "outside workspace" in result.lower() or "not found" in result.lower()

    def test_hidden_files_excluded(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        (ws / ".hidden").write_text("secret")
        result = _exec_list_files(ws, {"directory": "."})
        assert ".hidden" not in result

    def test_directory_shows_dir_tag(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_list_files(ws, {"directory": "."})
        assert "[dir]" in result


# ===================================================================
# _exec_read_file
# ===================================================================


@pytest.mark.unit
class TestExecReadFile:
    """Tests for read_file core tool executor."""

    def test_reads_text_file(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_read_file(ws, {"path": "readme.txt"})
        assert "Hello world" in result

    def test_file_not_found(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_read_file(ws, {"path": "nonexistent.txt"})
        assert "not found" in result.lower()

    def test_outside_workspace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_read_file(ws, {"path": "../../etc/passwd"})
        assert "outside workspace" in result.lower()

    def test_offset_and_limit(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        # Read from line 2, limit 2 lines
        result = _exec_read_file(ws, {"path": "readme.txt", "offset": 2, "limit": 2})
        assert "Line 2" in result
        assert "Line 3" in result
        # Line 1 should NOT be present
        assert "Hello world" not in result

    def test_max_lines_default(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_read_file(ws, {"path": "readme.txt", "max_lines": 2})
        # With default offset=1 and limit=0, max_lines caps at 2 lines
        # The result should contain first 2 lines of content
        assert "Hello world" in result
        assert "Line 2" in result
        # Should have a truncation indicator since file has 5 lines
        assert "total lines" in result.lower() or "..." in result

    def test_binary_file_marker(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        (ws / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result = _exec_read_file(ws, {"path": "image.png"})
        assert "Binary file" in result or "binary" in result.lower()

    def test_json_file(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_read_file(ws, {"path": "data.json"})
        assert '"key"' in result


# ===================================================================
# _exec_glob_files
# ===================================================================


@pytest.mark.unit
class TestExecGlobFiles:
    """Tests for glob_files core tool executor."""

    def test_glob_py_files(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_glob_files(ws, {"pattern": "**/*.py"})
        assert "nested.py" in result

    def test_glob_txt_files(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_glob_files(ws, {"pattern": "*.txt"})
        assert "readme.txt" in result

    def test_glob_no_matches(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_glob_files(ws, {"pattern": "*.xyz"})
        assert "no files matching" in result.lower()

    def test_glob_in_subdirectory(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_glob_files(ws, {"pattern": "*.py", "path": "subdir"})
        assert "nested.py" in result

    def test_glob_outside_workspace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_glob_files(ws, {"pattern": "*", "path": "../../"})
        assert "outside workspace" in result.lower() or "not found" in result.lower()


# ===================================================================
# _exec_grep_workspace
# ===================================================================


@pytest.mark.unit
class TestExecGrepWorkspace:
    """Tests for grep_workspace core tool executor."""

    def test_grep_content_match(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {"pattern": "Hello"})
        assert "readme.txt" in result

    def test_grep_no_matches(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {"pattern": "ZZZZNOTFOUND"})
        assert "no matches" in result.lower()

    def test_grep_case_insensitive(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {"pattern": "hello", "case_insensitive": True})
        assert "readme.txt" in result

    def test_grep_file_glob_filter(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {"pattern": "print", "file_glob": "*.py"})
        assert "nested.py" in result

    def test_grep_output_mode_files(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {"pattern": "Hello", "output_mode": "files"})
        assert "readme.txt" in result
        # In files mode, should not include line numbers with colons
        # (just file paths)

    def test_grep_output_mode_count(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {"pattern": "Line", "output_mode": "count"})
        assert "readme.txt" in result

    def test_grep_needs_pattern_or_glob(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {})
        assert "provide" in result.lower()

    def test_grep_file_glob_only(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {"file_glob": "*.json"})
        assert "data.json" in result

    def test_grep_context_lines(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_grep_workspace(ws, {"pattern": "Line 3", "context_lines": 1})
        # Should include context lines around the match
        assert "Line 2" in result or "Line 4" in result


# ===================================================================
# _exec_run_shell
# ===================================================================


@pytest.mark.unit
class TestExecRunShell:
    """Tests for run_shell core tool executor."""

    def test_echo_command(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_run_shell(ws, {"command": "echo hello_from_shell"})
        assert "hello_from_shell" in result

    def test_empty_command(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_run_shell(ws, {"command": ""})
        assert "error" in result.lower()

    def test_timeout_command(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_run_shell(ws, {"command": "sleep 10", "timeout": 1})
        assert "timed out" in result.lower()

    def test_cwd_is_workspace(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path)
        result = _exec_run_shell(ws, {"command": "pwd"})
        assert str(ws) in result


# ===================================================================
# get_judge_tools
# ===================================================================


@pytest.mark.unit
class TestGetJudgeTools:
    """Tests for tool pack assembly."""

    def test_core_tools_always_present(self) -> None:
        defs, executors = get_judge_tools()
        tool_names = {d["function"]["name"] for d in defs}
        assert "list_files" in tool_names
        assert "read_file" in tool_names
        assert "glob_files" in tool_names
        assert "grep_workspace" in tool_names
        assert "run_shell" in tool_names
        assert "submit_grade" in tool_names

    def test_core_executors_present(self) -> None:
        defs, executors = get_judge_tools()
        assert "list_files" in executors
        assert "read_file" in executors

    def test_office_pack_adds_tools(self) -> None:
        defs, executors = get_judge_tools(tool_packs=["office"])
        tool_names = {d["function"]["name"] for d in defs}
        assert "read_xlsx_cell" in tool_names
        assert "read_xlsx_range" in tool_names
        assert "read_docx_content" in tool_names

    def test_office_pack_executors(self) -> None:
        defs, executors = get_judge_tools(tool_packs=["office"])
        assert "read_xlsx_cell" in executors
        assert "read_docx_content" in executors

    def test_unknown_pack_ignored(self) -> None:
        defs, executors = get_judge_tools(tool_packs=["nonexistent_pack"])
        # Should still have core tools
        tool_names = {d["function"]["name"] for d in defs}
        assert "list_files" in tool_names
        # Should NOT have office tools
        assert "read_xlsx_cell" not in tool_names

    def test_no_packs_returns_core_only(self) -> None:
        defs_none, _ = get_judge_tools(tool_packs=None)
        defs_empty, _ = get_judge_tools(tool_packs=[])
        assert len(defs_none) == len(defs_empty) == len(_CORE_TOOL_DEFS)

    def test_tool_pack_registry_has_office(self) -> None:
        assert "office" in _TOOL_PACKS


# ===================================================================
# _parse_json_response
# ===================================================================


@pytest.mark.unit
class TestParseJsonResponse:
    """Tests for JSON parsing from LLM responses."""

    def test_plain_json(self) -> None:
        text = '{"score": 0.8, "reasoning": "good work"}'
        result = _parse_json_response(text)
        assert result["score"] == 0.8
        assert result["reasoning"] == "good work"

    def test_json_in_code_block(self) -> None:
        text = 'Some preamble\n```json\n{"score": 0.5, "reasoning": "ok"}\n```\nmore text'
        result = _parse_json_response(text)
        assert result["score"] == 0.5

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="Could not parse JSON"):
            _parse_json_response("This is not JSON at all")

    def test_nested_json(self) -> None:
        text = '{"score": 1.0, "details": {"sub": "value"}}'
        result = _parse_json_response(text)
        assert result["details"]["sub"] == "value"

    def test_empty_object(self) -> None:
        result = _parse_json_response("{}")
        assert result == {}

    def test_json_with_array(self) -> None:
        text = '{"scores": [0.1, 0.2, 0.3]}'
        result = _parse_json_response(text)
        assert len(result["scores"]) == 3


# ===================================================================
# LLMJudge — constructor
# ===================================================================


@pytest.mark.unit
class TestLLMJudgeInit:
    """Tests for LLMJudge constructor."""

    @patch("tolokaforge.core.grading.judge.LLMClient")
    def test_constructor_creates_client(self, mock_llm_cls: MagicMock) -> None:
        config = ModelConfig(provider="openai", name="gpt-4")
        judge = LLMJudge(config)
        mock_llm_cls.assert_called_once_with(config)
        assert judge.client is mock_llm_cls.return_value


# ===================================================================
# LLMJudge._grade_single_call
# ===================================================================


@pytest.mark.unit
class TestLLMJudgeSingleCall:
    """Tests for single-call (non-agentic) grading."""

    def _make_judge(self) -> tuple[LLMJudge, MagicMock]:
        with patch("tolokaforge.core.grading.judge.LLMClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            config = ModelConfig(provider="openai", name="gpt-4")
            judge = LLMJudge(config)
        return judge, mock_client

    def test_successful_grading(self) -> None:
        judge, mock_client = self._make_judge()
        mock_result = MagicMock()
        mock_result.text = '{"score": 0.85, "reasoning": "Well done"}'
        mock_client.generate.return_value = mock_result

        messages = [
            Message(role=MessageRole.USER, content="Do the task"),
            Message(role=MessageRole.ASSISTANT, content="Done!"),
        ]
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["score", "reasoning"],
        }

        score, reasons = judge.grade(messages, "Check quality", schema, "Test task")
        assert score == 0.85
        assert reasons == "Well done"
        mock_client.generate.assert_called_once()

    def test_score_clamped_to_0_1(self) -> None:
        judge, mock_client = self._make_judge()
        mock_result = MagicMock()
        mock_result.text = '{"score": 1.5, "reasoning": "Overrated"}'
        mock_client.generate.return_value = mock_result

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["score", "reasoning"],
        }
        score, _ = judge.grade(messages, "rubric", schema)
        assert score == 1.0  # Clamped

    def test_score_clamped_negative(self) -> None:
        judge, mock_client = self._make_judge()
        mock_result = MagicMock()
        mock_result.text = '{"score": -0.5, "reasoning": "Bad"}'
        mock_client.generate.return_value = mock_result

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["score", "reasoning"],
        }
        score, _ = judge.grade(messages, "rubric", schema)
        assert score == 0.0  # Clamped

    def test_llm_failure_returns_half_score(self) -> None:
        judge, mock_client = self._make_judge()
        mock_client.generate.side_effect = RuntimeError("API down")

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}
        score, reasons = judge.grade(messages, "rubric", schema)
        assert score == 0.5
        assert "Judge failed" in reasons

    def test_transcript_includes_tool_calls(self) -> None:
        judge, mock_client = self._make_judge()
        mock_result = MagicMock()
        mock_result.text = '{"score": 0.7, "reasoning": "ok"}'
        mock_client.generate.return_value = mock_result

        messages = [
            Message(
                role=MessageRole.ASSISTANT,
                content="Let me check",
                tool_calls=[ToolCall(id="tc1", name="search", arguments={"q": "test"})],
            ),
        ]
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["score", "reasoning"],
        }
        score, _ = judge.grade(messages, "rubric", schema)
        # Verify the prompt included tool call info
        call_args = mock_client.generate.call_args
        prompt_messages = call_args.kwargs.get("messages", call_args[1].get("messages", []))
        prompt_text = prompt_messages[0].content
        assert "search" in prompt_text

    def test_reasons_key_alias(self) -> None:
        """Judge output using 'reasons' key instead of 'reasoning'."""
        judge, mock_client = self._make_judge()
        mock_result = MagicMock()
        mock_result.text = '{"score": 0.6, "reasons": "alternative key"}'
        mock_client.generate.return_value = mock_result

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reasons": {"type": "string"},
            },
            "required": ["score", "reasons"],
        }
        score, reasons = judge.grade(messages, "rubric", schema)
        assert score == 0.6
        assert reasons == "alternative key"


# ===================================================================
# LLMJudge.grade — routing
# ===================================================================


@pytest.mark.unit
class TestLLMJudgeRouting:
    """Tests for grade() routing between single-call and agentic."""

    def _make_judge(self) -> tuple[LLMJudge, MagicMock]:
        with patch("tolokaforge.core.grading.judge.LLMClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            config = ModelConfig(provider="openai", name="gpt-4")
            judge = LLMJudge(config)
        return judge, mock_client

    def test_non_agentic_goes_to_single_call(self) -> None:
        judge, mock_client = self._make_judge()
        mock_result = MagicMock()
        mock_result.text = '{"score": 0.9, "reasoning": "good"}'
        mock_client.generate.return_value = mock_result

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["score", "reasoning"],
        }
        score, _ = judge.grade(messages, "rubric", schema, agentic=False)
        assert score == 0.9

    def test_agentic_without_workspace_falls_back_to_single_call(self) -> None:
        judge, mock_client = self._make_judge()
        mock_result = MagicMock()
        mock_result.text = '{"score": 0.7, "reasoning": "fallback"}'
        mock_client.generate.return_value = mock_result

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["score", "reasoning"],
        }
        # agentic=True but workspace_dir=None → single-call
        score, reasons = judge.grade(messages, "rubric", schema, agentic=True, workspace_dir=None)
        assert score == 0.7
        assert reasons == "fallback"


# ===================================================================
# LLMJudge._grade_agentic
# ===================================================================


@pytest.mark.unit
class TestLLMJudgeAgentic:
    """Tests for agentic (tool-using) grading."""

    def _make_judge(self) -> tuple[LLMJudge, MagicMock]:
        with patch("tolokaforge.core.grading.judge.LLMClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            config = ModelConfig(provider="openai", name="gpt-4")
            judge = LLMJudge(config)
        return judge, mock_client

    def test_agentic_submit_grade_tool(self, tmp_path: Path) -> None:
        """Judge calls submit_grade tool → score returned."""
        judge, mock_client = self._make_judge()
        ws = _make_workspace(tmp_path)

        # First call: list_files tool, second call: submit_grade
        list_result = MagicMock()
        list_result.text = "Let me inspect files"
        list_tc = ToolCall(id="tc1", name="list_files", arguments={"directory": "."})
        list_result.tool_calls = [list_tc]

        submit_result = MagicMock()
        submit_result.text = ""
        submit_tc = ToolCall(
            id="tc2",
            name="submit_grade",
            arguments={"score": 0.95, "reasoning": "Excellent work"},
        )
        submit_result.tool_calls = [submit_tc]

        mock_client.generate.side_effect = [list_result, submit_result]

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {
            "type": "object",
            "properties": {"score": {"type": "number"}, "reasoning": {"type": "string"}},
        }
        score, reasoning = judge.grade(messages, "rubric", schema, workspace_dir=ws, agentic=True)
        assert score == 0.95
        assert reasoning == "Excellent work"

    def test_agentic_json_response_without_tools(self, tmp_path: Path) -> None:
        """Judge returns JSON without calling submit_grade → parsed."""
        judge, mock_client = self._make_judge()
        ws = _make_workspace(tmp_path)

        result = MagicMock()
        result.text = '{"score": 0.6, "reasoning": "Average"}'
        result.tool_calls = []  # No tool calls
        mock_client.generate.return_value = result

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["score", "reasoning"],
        }
        score, reasoning = judge.grade(messages, "rubric", schema, workspace_dir=ws, agentic=True)
        assert score == 0.6

    def test_agentic_llm_error_returns_half(self, tmp_path: Path) -> None:
        """LLM call fails during agentic grading → 0.5 default."""
        judge, mock_client = self._make_judge()
        ws = _make_workspace(tmp_path)
        mock_client.generate.side_effect = RuntimeError("LLM error")

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}
        score, reasoning = judge.grade(messages, "rubric", schema, workspace_dir=ws, agentic=True)
        assert score == 0.5
        assert "failed" in reasoning.lower()

    def test_agentic_exhausts_turns_returns_half(self, tmp_path: Path) -> None:
        """Judge never submits grade within max turns → 0.5 default."""
        judge, mock_client = self._make_judge()
        ws = _make_workspace(tmp_path)

        # Always return non-parseable text with no tool calls
        result = MagicMock()
        result.text = "I need more information..."
        result.tool_calls = []
        mock_client.generate.return_value = result

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}
        score, reasoning = judge.grade(messages, "rubric", schema, workspace_dir=ws, agentic=True)
        assert score == 0.5
        assert "turn limit" in reasoning.lower() or "submit" in reasoning.lower()

    def test_agentic_custom_system_prompt(self, tmp_path: Path) -> None:
        """Custom system_prompt is passed to the LLM."""
        judge, mock_client = self._make_judge()
        ws = _make_workspace(tmp_path)

        result = MagicMock()
        result.text = ""
        tc = ToolCall(
            id="tc1",
            name="submit_grade",
            arguments={"score": 0.8, "reasoning": "Custom prompt works"},
        )
        result.tool_calls = [tc]
        mock_client.generate.return_value = result

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}
        score, _ = judge.grade(
            messages,
            "rubric",
            schema,
            workspace_dir=ws,
            agentic=True,
            system_prompt="Custom judge persona",
        )
        assert score == 0.8
        # Verify custom system prompt was used
        call_args = mock_client.generate.call_args
        assert call_args.kwargs.get("system") == "Custom judge persona"

    def test_agentic_unknown_tool(self, tmp_path: Path) -> None:
        """Judge calls an unknown tool → 'Unknown tool' response."""
        judge, mock_client = self._make_judge()
        ws = _make_workspace(tmp_path)

        # First call: unknown tool, second call: submit_grade
        unknown_result = MagicMock()
        unknown_result.text = ""
        unknown_tc = ToolCall(id="tc1", name="nonexistent_tool", arguments={})
        unknown_result.tool_calls = [unknown_tc]

        submit_result = MagicMock()
        submit_result.text = ""
        submit_tc = ToolCall(
            id="tc2",
            name="submit_grade",
            arguments={"score": 0.5, "reasoning": "Had issues"},
        )
        submit_result.tool_calls = [submit_tc]

        mock_client.generate.side_effect = [unknown_result, submit_result]

        messages = [Message(role=MessageRole.USER, content="task")]
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}
        score, _ = judge.grade(messages, "rubric", schema, workspace_dir=ws, agentic=True)
        assert score == 0.5


# ===================================================================
# Tool definition structure
# ===================================================================


@pytest.mark.unit
class TestToolDefinitionStructure:
    """Validate the structure of core tool definitions."""

    def test_all_core_tools_have_function_key(self) -> None:
        for tool_def in _CORE_TOOL_DEFS:
            assert tool_def["type"] == "function"
            assert "function" in tool_def
            assert "name" in tool_def["function"]
            assert "description" in tool_def["function"]

    def test_submit_grade_has_required_params(self) -> None:
        submit = next(d for d in _CORE_TOOL_DEFS if d["function"]["name"] == "submit_grade")
        params = submit["function"]["parameters"]
        assert "score" in params["properties"]
        assert "reasoning" in params["properties"]
        assert set(params["required"]) == {"score", "reasoning"}

    def test_core_executor_count_matches_defs(self) -> None:
        # Core executors don't include submit_grade (handled inline)
        core_tool_names = {d["function"]["name"] for d in _CORE_TOOL_DEFS}
        executor_names = set(_CORE_EXECUTORS.keys())
        # submit_grade is in defs but not in executors
        assert executor_names == core_tool_names - {"submit_grade"}

    def test_office_tool_defs_structure(self) -> None:
        for tool_def in _OFFICE_TOOL_DEFS:
            assert tool_def["type"] == "function"
            assert "function" in tool_def
            assert "name" in tool_def["function"]
