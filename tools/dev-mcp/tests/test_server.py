"""Basic tests for dev-mcp server module."""

import asyncio
from pathlib import Path

import pytest


@pytest.mark.unit
class TestRepoRootDetection:
    """Verify _find_repo_root locates the repository root."""

    def test_repo_root_contains_pyproject(self):
        from dev_mcp.server import REPO_ROOT

        assert (REPO_ROOT / "pyproject.toml").exists()

    def test_repo_root_contains_tolokaforge(self):
        from dev_mcp.server import REPO_ROOT

        assert (REPO_ROOT / "tolokaforge").is_dir()


@pytest.mark.unit
class TestSubprocessUtils:
    """Verify subprocess helper utilities."""

    def test_truncate_short_text(self):
        from dev_mcp.subprocess_utils import _truncate

        log_path = Path("/tmp/test.log")
        text = "short"
        assert _truncate(text, 100, log_path) == "short"

    def test_truncate_long_text(self):
        from dev_mcp.subprocess_utils import _truncate

        log_path = Path("/tmp/test.log")
        text = "x" * 200
        result = _truncate(text, 50, log_path)
        assert "<TRUNCATED>" in result
        assert "Full output is in" in result
        assert str(log_path) in result
        # Should contain beginning and end
        assert result.startswith("x" * 25)
        assert result.endswith("x" * 25)

    def test_format_result_success(self):
        from dev_mcp.subprocess_utils import format_result

        log_path = Path("/tmp/test.log")
        result = format_result(
            exit_code=0,
            output="hello world",
            log_path=log_path,
        )
        assert "Exit code: 0" in result
        assert "hello world" in result
        assert str(log_path) in result

    def test_format_result_timeout(self):
        from dev_mcp.subprocess_utils import format_result

        log_path = Path("/tmp/test.log")
        result = format_result(
            exit_code=None,
            output="partial",
            log_path=log_path,
            timed_out=True,
            timeout=30,
        )
        assert "TIMED OUT after 30s" in result
        assert "output (partial)" in result

    def test_max_output_default(self):
        # Without env var set, should return default
        import os

        from dev_mcp.subprocess_utils import _max_output_chars

        os.environ.pop("DEV_MCP_MAX_OUTPUT", None)
        assert _max_output_chars() == 20_000

    def test_run_command_tees_to_log_file(self):
        """Verify run_command writes output to the log file in real-time."""
        from dev_mcp.subprocess_utils import run_command

        result = asyncio.run(
            run_command(
                ["echo", "hello from tee test"],
                cwd=Path("."),
                timeout=10,
                tool_name="tee_test",
            )
        )
        assert "Exit code: 0" in result
        assert "hello from tee test" in result
        # Extract log path from result and verify file was written
        for line in result.splitlines():
            if line.startswith("Log: "):
                log_path = Path(line.removeprefix("Log: ").strip())
                assert log_path.exists(), f"Log file should exist: {log_path}"
                content = log_path.read_text()
                assert "hello from tee test" in content
                break
        else:
            pytest.fail("Could not find 'Log:' line in result")

    def test_run_command_truncates_large_output(self):
        """Verify large output gets truncated with head+tail and pointer to log file."""
        import os

        from dev_mcp.subprocess_utils import run_command

        # Set a small limit for testing
        os.environ["DEV_MCP_MAX_OUTPUT"] = "200"
        try:
            # Generate output larger than the limit
            result = asyncio.run(
                run_command(
                    ["python3", "-c", "print('A' * 500)"],
                    cwd=Path("."),
                    timeout=10,
                    tool_name="truncate_test",
                )
            )
            assert "<TRUNCATED>" in result
            assert "Full output is in" in result
        finally:
            os.environ.pop("DEV_MCP_MAX_OUTPUT", None)


@pytest.mark.unit
class TestEnvironmentHelpers:
    """Verify environment variable helpers."""

    def test_has_any_llm_key_false(self):
        import os

        from dev_mcp.server import _has_any_llm_key

        # Clear all LLM keys
        for key in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY"]:
            os.environ.pop(key, None)
        assert not _has_any_llm_key()

    def test_has_any_llm_key_true(self):
        import os

        from dev_mcp.server import _has_any_llm_key

        os.environ["OPENAI_API_KEY"] = "test-key"
        try:
            assert _has_any_llm_key()
        finally:
            del os.environ["OPENAI_API_KEY"]

    def test_check_env_vars_missing(self):
        import os

        from dev_mcp.server import _check_env_vars

        os.environ.pop("NONEXISTENT_VAR", None)
        missing = _check_env_vars("NONEXISTENT_VAR")
        assert "NONEXISTENT_VAR" in missing

    def test_check_env_vars_present(self):
        import os

        from dev_mcp.server import _check_env_vars

        os.environ["TEST_VAR_EXISTS"] = "yes"
        try:
            missing = _check_env_vars("TEST_VAR_EXISTS")
            assert missing == []
        finally:
            del os.environ["TEST_VAR_EXISTS"]
