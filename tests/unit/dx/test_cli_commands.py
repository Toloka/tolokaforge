"""Unit tests for CLI command modules.

Covers: tolokaforge/dx/cli/main.py, adapter_commands.py,
config_commands.py, docker_commands.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.dx.cli.main import (
    _extract_log_errors,
    _extract_tool_failures,
    _format_eta,
    _load_task_under_its_project,
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


def _write_task_pack(directory: Path) -> Path:
    """Write a minimal loadable task pack and return its ``task.yaml``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "grading.yaml").write_text("{}\n")
    task_file = directory / "task.yaml"
    task_file.write_text(yaml.dump({"task_id": directory.name, "description": "A task."}))
    return task_file


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

    def test_validate_fails_on_a_glob_that_matches_nothing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A pattern selecting no file is an invocation error, not a vacuous pass.

        The degenerate input a sweep over real packs never produces, and the
        shape a CI glob silently drifts into (#764).
        """
        pattern = str(tmp_path / "*.xyz")

        result = runner.invoke(cli, ["validate", "--tasks", pattern])

        assert result.exit_code == 1
        assert pattern in result.stderr

    def test_validate_exits_zero_when_every_task_is_valid(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        task_file = _write_task_pack(tmp_path / "good")

        result = runner.invoke(cli, ["validate", "--tasks", str(task_file)])

        assert result.exit_code == 0, result.stderr
        assert "1 valid, 0 invalid" in result.stderr

    def test_validate_exits_one_and_still_reports_every_task(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """One malformed task fails the command without hiding the valid one."""
        _write_task_pack(tmp_path / "good")
        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "task.yaml").write_text("description: no task_id here\n")

        result = runner.invoke(cli, ["validate", "--tasks", str(tmp_path / "**" / "task.yaml")])

        assert result.exit_code == 1
        assert "✓" in result.stderr
        assert "✗" in result.stderr
        assert "1 valid, 1 invalid" in result.stderr

    def test_validate_loads_a_task_under_its_projects_task_defaults(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The validate path layers ``project.task_defaults`` the way a run does."""
        (tmp_path / "project.yaml").write_text(
            yaml.dump(
                {
                    "name": "layered",
                    "task_defaults": {"max_turns": 7, "system_prompt": "voice.md"},
                }
            )
        )
        task_file = _write_task_pack(tmp_path / "tasks" / "inherits")

        result = runner.invoke(cli, ["validate", "--tasks", str(task_file)])
        assert result.exit_code == 0, result.stderr

        task_config, _, _ = _load_task_under_its_project(task_file)
        assert task_config.max_turns == 7
        # Anchored to the project directory the default was declared in.
        assert task_config.system_prompt == str(tmp_path / "voice.md")

    def test_validate_reports_a_project_that_fails_to_load_against_its_task(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A broken ``project.yaml`` fails its tasks, naming the file — not the glob."""
        (tmp_path / "project.yaml").write_text("- not a mapping\n")
        task_file = _write_task_pack(tmp_path / "tasks" / "orphaned")

        result = runner.invoke(cli, ["validate", "--tasks", str(task_file)])

        assert result.exit_code == 1
        assert "project.yaml" in result.stderr
        assert "0 valid, 1 invalid" in result.stderr

    def test_validate_invalid_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Validate a file with invalid YAML."""
        task_file = tmp_path / "bad.yaml"
        task_file.write_text("not: valid: yaml: [broken")

        result = runner.invoke(cli, ["validate", "--tasks", str(task_file)])

        assert result.exit_code == 1
        assert "0 valid, 1 invalid" in result.stderr

    @pytest.mark.parametrize(
        ("adapter_type", "exit_code", "summary"),
        [("tau", 0, "1 valid, 0 invalid"), ("native", 1, "0 valid, 1 invalid")],
        ids=["adapter_validate_cannot_read", "the_native_reading_validate_owns"],
    )
    def test_validate_says_so_when_it_cannot_check_a_tasks_tools(
        self,
        runner: CliRunner,
        tmp_path: Path,
        adapter_type: str,
        exit_code: int,
        summary: str,
    ) -> None:
        """A tool set validate cannot read is printed as unchecked, never as a pass.

        ``validate`` reads every task through the native loader, so for a task an
        external adapter owns it has no tool set to check names against. The same
        pack declared native is rejected, which is what makes the first row a
        report of ignorance rather than an accidental clean bill of health.
        """
        directory = tmp_path / f"pack_{adapter_type}"
        directory.mkdir(parents=True)
        (directory / "grading.yaml").write_text(
            yaml.dump(
                {
                    # Weighted, so the only thing separating the two rows is whether
                    # validate can read the pack's tool set.
                    "combine": {"weights": {"trace_checks": 1.0}},
                    "trace_checks": {
                        "constraints": [
                            {
                                "id": "probe",
                                "description": "the agent called the tool",
                                "require": {
                                    "present": {
                                        "match": {
                                            "kind": "tool_call",
                                            "tool": {"equals": "nothing_declares_this"},
                                        }
                                    }
                                },
                            }
                        ]
                    },
                }
            )
        )
        task_file = directory / "task.yaml"
        task_file.write_text(
            yaml.dump(
                {
                    "task_id": directory.name,
                    "description": "A task.",
                    "adapter_type": adapter_type,
                }
            )
        )

        result = runner.invoke(cli, ["validate", "--tasks", str(task_file)], env={"COLUMNS": "400"})

        assert result.exit_code == exit_code, result.stderr
        assert summary in result.stderr
        if exit_code == 0:
            assert "not checked" in result.stderr
            assert "could not be resolved" in result.stderr


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
        assert "No YAML files found" in result.stderr

    def test_config_validate_invalid_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Config validate with invalid YAML content."""
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("[not: valid: yaml: [")

        result = runner.invoke(cli, ["config", "validate", "--config", str(cfg_file)])
        # Either shows error or fails — Rich output routes to stderr
        # via the shared display console.
        assert (
            "parse" in result.stderr.lower()
            or "error" in result.stderr.lower()
            or result.exit_code != 0
        )

    def test_config_validate_non_mapping_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Config validate with YAML that is a list, not a mapping."""
        cfg_file = tmp_path / "list.yaml"
        cfg_file.write_text("- item1\n- item2\n")

        result = runner.invoke(cli, ["config", "validate", "--config", str(cfg_file)])
        assert "mapping" in result.stderr.lower() or result.exit_code != 0


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
        assert "--run-dir" in result.output
        assert "--verbose" in result.output
        assert "--strict" in result.output
        assert "--workers" in result.output

    def test_run_missing_config(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["run"])
        assert result.exit_code != 0

    def test_run_nonexistent_config(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["run", "--config", "/nonexistent/config.yaml"])
        assert result.exit_code != 0

    def test_run_workers_rejects_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        # Positive-int validation happens at click-parse time — no need
        # for a real config file since the exit fires before loading.
        # Click's IntRange failure writes to stderr and exits with 2;
        # asserting on exit_code alone is enough to pin the validation
        # (message goes to stderr, which CliRunner routes separately).
        result = runner.invoke(
            cli,
            ["run", "--config", str(tmp_path / "x.yaml"), "--workers", "0"],
        )
        assert result.exit_code != 0

    def test_run_workers_rejects_negative(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli,
            ["run", "--config", str(tmp_path / "x.yaml"), "--workers", "-3"],
        )
        assert result.exit_code != 0

    def test_run_workers_help_names_compute_workers(self, runner: CliRunner) -> None:
        # The help text points authors at the canonical home so the
        # flag doesn't quietly ship the compute vs orchestrator dual-
        # home confusion into new pack authoring.
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "compute.workers" in result.output


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
        from tolokaforge.dx.cli.config import _resolve_paths

        f = tmp_path / "test.yaml"
        f.write_text("key: value")
        paths = _resolve_paths(str(f))
        assert len(paths) == 1
        assert paths[0] == f

    def test_directory_finds_yaml_files(self, tmp_path: Path) -> None:
        from tolokaforge.dx.cli.config import _resolve_paths

        (tmp_path / "a.yaml").write_text("key: 1")
        (tmp_path / "b.yml").write_text("key: 2")
        (tmp_path / "c.txt").write_text("not yaml")
        paths = _resolve_paths(str(tmp_path))
        names = {p.name for p in paths}
        assert "a.yaml" in names
        assert "b.yml" in names
        assert "c.txt" not in names

    def test_glob_pattern(self, tmp_path: Path) -> None:
        from tolokaforge.dx.cli.config import _resolve_paths

        (tmp_path / "x.yaml").write_text("a: 1")
        paths = _resolve_paths(str(tmp_path / "*.yaml"))
        assert len(paths) == 1

    def test_nonexistent_returns_empty(self) -> None:
        from tolokaforge.dx.cli.config import _resolve_paths

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
