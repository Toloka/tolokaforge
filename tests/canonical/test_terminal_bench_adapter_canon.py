"""Canonical tests for TerminalBenchAdapter — compares against golden snapshots."""

import json
import re
from pathlib import Path

import pytest

from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

pytestmark = pytest.mark.canonical

TEST_DATA_DIR = Path(__file__).parent.parent / "data"
TBENCH_TASKS_DIR = TEST_DATA_DIR / "terminal_bench_tasks"


def _normalize_paths(obj):
    """Replace absolute task_dir paths with a stable placeholder.

    Adapter output contains machine-specific absolute paths (e.g.
    ``/Users/alice/work/...`` vs ``/home/runner/work/...``).  We replace
    the prefix up to ``tests/data/`` with ``<ROOT>/`` so that golden
    snapshots are portable across machines and CI.
    """
    text = json.dumps(obj)
    text = re.sub(
        r'"[^"]*?(/tests/data/terminal_bench_tasks/)',
        r'"<ROOT>\1',
        text,
    )
    return json.loads(text)


@pytest.fixture
def tbench_adapter() -> TerminalBenchAdapter:
    """Create TerminalBenchAdapter pointed at tests/data/terminal_bench_tasks/."""
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(TBENCH_TASKS_DIR),
        }
    )


class TestTerminalBenchAdapterCanon:
    """Canonical tests for TerminalBenchAdapter task loading and serialisation."""

    def test_task_discovery(self, tbench_adapter):
        """Adapter discovers the echo-hello fixture task."""
        task_ids = tbench_adapter.get_task_ids()
        assert task_ids == ["echo-hello"]

    def test_task_config(self, tbench_adapter, canon_snapshot):
        """TaskConfig has correct adapter_type, category, and instruction."""
        task = tbench_adapter.get_task("echo-hello")
        snap = canon_snapshot("tbench_echo_hello")

        actual = _normalize_paths(task.model_dump(mode="json"))
        snap.assert_match(actual, "task_config.json")

    def test_task_description(self, tbench_adapter, canon_snapshot):
        """TaskDescription is correctly serialised for the Runner."""
        td = tbench_adapter.to_task_description("echo-hello")
        snap = canon_snapshot("tbench_echo_hello")

        actual = _normalize_paths(td.model_dump(mode="json"))
        snap.assert_match(actual, "task_description.json")

    def test_tool_schemas(self, tbench_adapter, canon_snapshot):
        """Agent gets a single bash tool with DOCKER_COMPOSE_EXEC invocation style."""
        td = tbench_adapter.to_task_description("echo-hello")
        snap = canon_snapshot("tbench_echo_hello")

        actual = _normalize_paths([t.model_dump(mode="json") for t in td.agent_tools])
        snap.assert_match(actual, "tool_schemas.json")

    def test_grading_config(self, tbench_adapter, canon_snapshot):
        """GradingConfig uses custom_checks with 1.0 weight."""
        grading = tbench_adapter.get_grading_config("echo-hello")
        snap = canon_snapshot("tbench_echo_hello")

        actual = grading.model_dump(mode="json")
        snap.assert_match(actual, "grading_config.json")


class TestTerminalBenchAdapterIntegrity:
    """Validate adapter output against source files without snapshots."""

    def test_instruction_matches_task_yaml(self, tbench_adapter):
        """Instruction in TaskConfig matches task.yaml content."""
        task = tbench_adapter.get_task("echo-hello")
        task_yaml_path = TBENCH_TASKS_DIR / "echo-hello" / "task.yaml"

        import yaml

        with open(task_yaml_path) as f:
            raw = yaml.safe_load(f)

        assert task.initial_user_message.strip() == raw["instruction"].strip()

    def test_task_description_adapter_type(self, tbench_adapter):
        """TaskDescription has TERMINAL_BENCH adapter type."""
        td = tbench_adapter.to_task_description("echo-hello")
        assert td.adapter_type == "terminal_bench"

    def test_tool_source_invocation_style(self, tbench_adapter):
        """Bash tool uses DOCKER_COMPOSE_EXEC invocation style."""
        td = tbench_adapter.to_task_description("echo-hello")
        assert len(td.agent_tools) == 1
        tool = td.agent_tools[0]
        assert tool.name == "bash"
        assert tool.source.invocation_style == "docker_compose_exec"

    def test_tool_source_extra_has_compose_paths(self, tbench_adapter):
        """ToolSource.extra contains compose_file, task_dir, service, env_vars."""
        td = tbench_adapter.to_task_description("echo-hello")
        extra = td.agent_tools[0].source.extra
        assert extra["compose_file"] == "docker-compose.yaml"
        assert extra["service"] == "main"
        assert "T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME" in extra["env_vars"]
        assert "T_BENCH_CONTAINER_LOGS_PATH" in extra["env_vars"]

    def test_metadata_from_toml(self, tbench_adapter):
        """TaskDescription metadata contains difficulty and tags from task.toml."""
        td = tbench_adapter.to_task_description("echo-hello")
        assert td.metadata["difficulty"] == "easy"
        assert "shell" in td.metadata["tags"]
        assert td.metadata["verifier_timeout_sec"] == 30.0

    def test_task_id_filter(self):
        """task_ids param filters discovered tasks."""
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(TBENCH_TASKS_DIR),
                "task_ids": ["nonexistent"],
            }
        )
        assert adapter.get_task_ids() == []

    def test_runner_task_dir_override(self):
        """runner_task_dir param overrides task_dir in ToolSource.extra."""
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(TBENCH_TASKS_DIR),
                "runner_task_dir": "/mounted/tasks",
            }
        )
        td = adapter.to_task_description("echo-hello")
        assert td.agent_tools[0].source.extra["task_dir"] == "/mounted/tasks/echo-hello"
