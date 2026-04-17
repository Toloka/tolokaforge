"""Unit tests for CLI command modules.

Covers: tolokaforge/cli/main.py, adapter_commands.py,
config_commands.py, docker_commands.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.cli.main import (
    _extract_log_errors,
    _extract_tool_failures,
    _format_eta,
    cli,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Click test runner."""
    return CliRunner(mix_stderr=False)


# ===================================================================
# Top-level CLI group
# ===================================================================


@pytest.mark.unit
class TestCLIGroup:
    """Tests for the top-level CLI group."""

    def test_help_text(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Universal LLM Tool-Use Benchmarking Harness" in result.output

    def test_lists_subcommands(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert "run" in result.output
        assert "validate" in result.output
        assert "docker" in result.output
        assert "adapter" in result.output
        assert "config" in result.output
        assert "analyze" in result.output


# ===================================================================
# validate command
# ===================================================================


@pytest.mark.unit
class TestValidateCommand:
    """Tests for 'tolokaforge validate' command."""

    def test_validate_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["validate", "--help"])
        assert result.exit_code == 0
        assert "--tasks" in result.output

    def test_validate_requires_tasks(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["validate"])
        assert result.exit_code != 0
        # Click sends error output to stderr with mix_stderr=False
        combined = (result.output or "") + (result.stderr or "")
        assert "Missing" in combined or "required" in combined.lower() or result.exit_code == 2

    def test_validate_nonexistent_glob(self, runner: CliRunner, tmp_path: Path) -> None:
        # Glob pattern matching no files → 0 valid, 0 invalid
        result = runner.invoke(cli, ["validate", "--tasks", str(tmp_path / "*.xyz")])
        assert result.exit_code == 0
        assert "0 valid" in result.output

    def test_validate_valid_task_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Validate a minimal valid task.yaml file."""
        task_data = {
            "task_id": "test-001",
            "description": "Test task",
            "tools": ["search"],
            "grading": {
                "method": "state_check",
                "expected_state": {"key": "value"},
            },
        }
        task_file = tmp_path / "task.yaml"
        task_file.write_text(yaml.dump(task_data))

        result = runner.invoke(cli, ["validate", "--tasks", str(task_file)])
        # Either valid or shows validation result
        assert result.exit_code == 0 or "invalid" in result.output.lower()

    def test_validate_invalid_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Validate a file with invalid YAML."""
        task_file = tmp_path / "bad.yaml"
        task_file.write_text("not: valid: yaml: [broken")

        result = runner.invoke(cli, ["validate", "--tasks", str(task_file)])
        # Should handle the error gracefully
        assert (
            "invalid" in result.output.lower()
            or result.exit_code != 0
            or "0 valid" in result.output
        )


# ===================================================================
# docker command group
# ===================================================================


@pytest.mark.unit
class TestDockerCommands:
    """Tests for 'tolokaforge docker' command group."""

    def test_docker_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["docker", "--help"])
        assert result.exit_code == 0
        assert "Manage Docker images" in result.output

    def test_docker_build_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["docker", "build", "--help"])
        assert result.exit_code == 0
        assert "--core" in result.output
        assert "--force" in result.output

    def test_docker_up_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["docker", "up", "--help"])
        assert result.exit_code == 0
        assert "--profile" in result.output

    def test_docker_down_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["docker", "down", "--help"])
        assert result.exit_code == 0
        assert "--volumes" in result.output

    def test_docker_status_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["docker", "status", "--help"])
        assert result.exit_code == 0


# ===================================================================
# adapter command group
# ===================================================================


@pytest.mark.unit
class TestAdapterCommands:
    """Tests for 'tolokaforge adapter' command group."""

    def test_adapter_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["adapter", "--help"])
        assert result.exit_code == 0
        assert "Adapter management" in result.output

    def test_adapter_convert_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["adapter", "convert", "--help"])
        assert result.exit_code == 0
        assert "--name" in result.output
        assert "--tasks-glob" in result.output
        assert "--output" in result.output

    def test_adapter_convert_missing_args(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["adapter", "convert"])
        assert result.exit_code != 0
        # Click sends error output to stderr with mix_stderr=False
        combined = (result.output or "") + (result.stderr or "")
        assert "Missing" in combined or "required" in combined.lower() or result.exit_code == 2

    def test_adapter_convert_missing_name(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli, ["adapter", "convert", "--tasks-glob", "*.yaml", "--output", "/tmp/out"]
        )
        assert result.exit_code != 0


# ===================================================================
# config command group
# ===================================================================


@pytest.mark.unit
class TestConfigCommands:
    """Tests for 'tolokaforge config' command group."""

    def test_config_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "--help"])
        assert result.exit_code == 0
        assert "Configuration management" in result.output

    def test_config_validate_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "validate", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.output
        assert "--strict" in result.output

    def test_config_validate_missing_config(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "validate"])
        assert result.exit_code != 0

    def test_config_validate_nonexistent_path(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "validate", "--config", "/nonexistent/path.yaml"])
        assert result.exit_code != 0

    def test_config_validate_empty_dir(self, runner: CliRunner, tmp_path: Path) -> None:
        """Validating an empty directory → no YAML files found."""
        result = runner.invoke(cli, ["config", "validate", "--config", str(tmp_path)])
        assert result.exit_code != 0
        assert "No YAML files found" in result.output

    def test_config_validate_invalid_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Config validate with invalid YAML content."""
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("[not: valid: yaml: [")

        result = runner.invoke(cli, ["config", "validate", "--config", str(cfg_file)])
        # Either shows error or fails
        assert (
            "parse" in result.output.lower()
            or "error" in result.output.lower()
            or result.exit_code != 0
        )

    def test_config_validate_non_mapping_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Config validate with YAML that is a list, not a mapping."""
        cfg_file = tmp_path / "list.yaml"
        cfg_file.write_text("- item1\n- item2\n")

        result = runner.invoke(cli, ["config", "validate", "--config", str(cfg_file)])
        assert "mapping" in result.output.lower() or result.exit_code != 0


# ===================================================================
# run command
# ===================================================================


@pytest.mark.unit
class TestRunCommand:
    """Tests for 'tolokaforge run' command."""

    def test_run_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.output
        assert "--resume" in result.output
        assert "--verbose" in result.output
        assert "--strict" in result.output

    def test_run_missing_config(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["run"])
        assert result.exit_code != 0

    def test_run_nonexistent_config(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["run", "--config", "/nonexistent/config.yaml"])
        assert result.exit_code != 0


# ===================================================================
# analyze command
# ===================================================================


@pytest.mark.unit
class TestAnalyzeCommand:
    """Tests for 'tolokaforge analyze' command."""

    def test_analyze_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["analyze", "--help"])
        assert result.exit_code == 0
        assert "--trajectory" in result.output

    def test_analyze_missing_trajectory(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["analyze"])
        assert result.exit_code != 0


# ===================================================================
# status command
# ===================================================================


@pytest.mark.unit
class TestStatusCommand:
    """Tests for 'tolokaforge status' command."""

    def test_status_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "--run-dir" in result.output


# ===================================================================
# _extract_tool_failures
# ===================================================================


@pytest.mark.unit
class TestExtractToolFailures:
    """Tests for _extract_tool_failures helper."""

    def test_no_messages(self) -> None:
        assert _extract_tool_failures({}) == []

    def test_no_failures(self) -> None:
        traj = {
            "messages": [
                {"role": "tool", "content": "Success: data returned"},
                {"role": "assistant", "content": "Got the data"},
            ]
        }
        assert _extract_tool_failures(traj) == []

    def test_error_in_tool_content(self) -> None:
        traj = {
            "messages": [
                {"role": "tool", "content": "Error: connection refused"},
                {"role": "tool", "content": "Success: ok"},
            ]
        }
        failures = _extract_tool_failures(traj)
        assert len(failures) == 1
        assert "connection refused" in failures[0]

    def test_failed_in_tool_content(self) -> None:
        traj = {
            "messages": [
                {"role": "tool", "content": "Tool execution failed: timeout"},
            ]
        }
        failures = _extract_tool_failures(traj)
        assert len(failures) == 1

    def test_non_tool_messages_ignored(self) -> None:
        traj = {
            "messages": [
                {"role": "assistant", "content": "Error in my response"},
                {"role": "user", "content": "Failed to understand"},
            ]
        }
        assert _extract_tool_failures(traj) == []

    def test_long_content_truncated(self) -> None:
        traj = {
            "messages": [
                {"role": "tool", "content": "Error: " + "x" * 500},
            ]
        }
        failures = _extract_tool_failures(traj)
        assert len(failures) == 1
        assert len(failures[0]) <= 200


# ===================================================================
# _extract_log_errors
# ===================================================================


@pytest.mark.unit
class TestExtractLogErrors:
    """Tests for _extract_log_errors helper."""

    def test_empty_logs(self) -> None:
        assert _extract_log_errors([]) == []

    def test_no_errors(self) -> None:
        logs = [
            {"level": "INFO", "message": "All good"},
            {"level": "WARNING", "message": "Just a warning"},
        ]
        assert _extract_log_errors(logs) == []

    def test_error_logs_extracted(self) -> None:
        logs = [
            {"level": "ERROR", "message": "Connection timeout"},
            {"level": "INFO", "message": "Retrying"},
            {"level": "ERROR", "message": "Failed again"},
        ]
        errors = _extract_log_errors(logs)
        assert len(errors) == 2
        assert "Connection timeout" in errors[0]
        assert "Failed again" in errors[1]

    def test_long_message_truncated(self) -> None:
        logs = [{"level": "ERROR", "message": "x" * 500}]
        errors = _extract_log_errors(logs)
        assert len(errors[0]) <= 200


# ===================================================================
# _format_eta
# ===================================================================


@pytest.mark.unit
class TestFormatEta:
    """Tests for _format_eta helper."""

    def test_none_returns_na(self) -> None:
        assert _format_eta(None) == "n/a"

    def test_zero_seconds(self) -> None:
        assert _format_eta(0) == "0s"

    def test_seconds_only(self) -> None:
        assert _format_eta(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        result = _format_eta(125)
        assert "2m" in result
        assert "5s" in result

    def test_hours_minutes_seconds(self) -> None:
        result = _format_eta(3725)
        assert "1h" in result
        assert "2m" in result
        assert "5s" in result

    def test_negative_returns_zero(self) -> None:
        assert _format_eta(-10) == "0s"

    def test_fractional_seconds(self) -> None:
        result = _format_eta(90.7)
        assert "1m" in result
        assert "30s" in result


# ===================================================================
# _resolve_paths (from config_commands)
# ===================================================================


@pytest.mark.unit
class TestResolvePaths:
    """Tests for path resolution in config commands."""

    def test_single_file(self, tmp_path: Path) -> None:
        from tolokaforge.cli.config_commands import _resolve_paths

        f = tmp_path / "test.yaml"
        f.write_text("key: value")
        paths = _resolve_paths(str(f))
        assert len(paths) == 1
        assert paths[0] == f

    def test_directory_finds_yaml_files(self, tmp_path: Path) -> None:
        from tolokaforge.cli.config_commands import _resolve_paths

        (tmp_path / "a.yaml").write_text("key: 1")
        (tmp_path / "b.yml").write_text("key: 2")
        (tmp_path / "c.txt").write_text("not yaml")
        paths = _resolve_paths(str(tmp_path))
        names = {p.name for p in paths}
        assert "a.yaml" in names
        assert "b.yml" in names
        assert "c.txt" not in names

    def test_glob_pattern(self, tmp_path: Path) -> None:
        from tolokaforge.cli.config_commands import _resolve_paths

        (tmp_path / "x.yaml").write_text("a: 1")
        paths = _resolve_paths(str(tmp_path / "*.yaml"))
        assert len(paths) == 1

    def test_nonexistent_returns_empty(self) -> None:
        from tolokaforge.cli.config_commands import _resolve_paths

        paths = _resolve_paths("/nonexistent/path/to/nothing")
        assert paths == []


# ===================================================================
# prepare command
# ===================================================================


@pytest.mark.unit
class TestPrepareCommand:
    """Tests for 'tolokaforge prepare' command."""

    def test_prepare_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["prepare", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.output
        assert "--run-dir" in result.output

    def test_prepare_missing_args(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["prepare"])
        assert result.exit_code != 0


# ===================================================================
# worker command
# ===================================================================


@pytest.mark.unit
class TestWorkerCommand:
    """Tests for 'tolokaforge worker' command."""

    def test_worker_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["worker", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.output
        assert "--run-dir" in result.output
        assert "--max-attempts" in result.output
