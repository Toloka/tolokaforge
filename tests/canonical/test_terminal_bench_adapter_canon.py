"""Canonical tests for TerminalBenchAdapter — compares against golden snapshots."""

import json
import re
from pathlib import Path

import pytest
import yaml
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _normalize_paths(obj):
    """Replace absolute paths + the content digest with stable placeholders.

    Adapter output contains three machine-varying strings: the absolute
    prefix leading to ``tests/data/terminal_bench_tasks/`` (differs by
    checkout root), the task staging directory name
    ``<staging_root>/echo-hello-<16-hex-digest>/…`` (differs by the
    per-test ``tmp_path``), and the shared engine staging directory
    ``<staging_root>/engine-<16-hex-digest>/…``. All three are normalised
    to keep the snapshot portable across machines and CI.
    """
    text = json.dumps(obj)
    text = re.sub(
        r'"[^"]*?(/tests/data/terminal_bench_tasks/)',
        r'"<ROOT>\1',
        text,
    )
    text = re.sub(
        r'"[^"]*?/echo-hello-[0-9a-f]{16}/',
        r'"<STAGING>/echo-hello/',
        text,
    )
    text = re.sub(
        r'"[^"]*?/engine-[0-9a-f]{16}/',
        r'"<STAGING>/engine/',
        text,
    )
    return json.loads(text)


@pytest.fixture
def tbench_adapter(test_data_dir, tmp_path) -> TerminalBenchAdapter:
    """Create TerminalBenchAdapter pointed at tests/data/terminal_bench_tasks/."""
    tbench_tasks_dir = test_data_dir / "terminal_bench_tasks"
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(tbench_tasks_dir),
            "staging_root": str(tmp_path / "staging"),
        }
    )


@pytest.fixture
def tbench_harness_adapter(test_data_dir, tmp_path) -> TerminalBenchAdapter:
    """The same adapter under harness mode — Claude Code layered onto the task image."""
    tbench_tasks_dir = test_data_dir / "terminal_bench_tasks"
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(tbench_tasks_dir),
            "staging_root": str(tmp_path / "staging"),
            "agent_harness": "claude-code",
            "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
        }
    )


@pytest.fixture
def tbench_skills_harness_adapter(test_data_dir, tmp_path) -> TerminalBenchAdapter:
    """Harness mode against the fixture task that ships its own skills bundle."""
    tbench_tasks_dir = test_data_dir / "terminal_bench_tasks"
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(tbench_tasks_dir),
            "task_ids": ["echo-hello-skills"],
            "staging_root": str(tmp_path / "staging"),
            "agent_harness": "claude-code",
            "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
        }
    )


class TestTerminalBenchAdapterCanon:
    """Canonical tests for TerminalBenchAdapter task loading and serialisation."""

    def test_task_discovery(self, tbench_adapter):
        """Adapter discovers both fixture tasks — plain, and skills-carrying."""
        task_ids = tbench_adapter.get_task_ids()
        assert task_ids == ["echo-hello", "echo-hello-skills"]

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


class TestTerminalBenchHarnessModeCanon:
    """Harness mode's synthesised substrate — layered image + two declared builds.

    The agent service's ``image:`` is pinned here because it is the whole
    freshness contract: it carries a digest of the layer's build context, so a
    change to what the layer bakes in has to show up as a moved snapshot line
    rather than as a silently reused image.
    """

    def test_synthesised_compose(self, tbench_harness_adapter, canon_snapshot):
        """The layered compose file carries a build-only base service and a CLI layer."""
        env = tbench_harness_adapter._environment("echo-hello")
        snap = canon_snapshot("tbench_echo_hello_harness")

        with env.compose_file.open() as f:
            actual = yaml.safe_load(f)
        snap.assert_match(actual, "synthesised_compose.json")


class TestTerminalBenchSkillsBundleCanon:
    """The substrate for a task pack that ships its own skills bundle.

    Pinned because the bundle changes what the agent can read: the ``COPY``
    line, its build-context exception, and the layered image ref that carries
    the bundle's bytes are the ends that have to agree, and a reward earned
    with skills installed is not comparable to one earned without them.
    """

    def test_synthesised_compose(self, tbench_skills_harness_adapter, canon_snapshot):
        env = tbench_skills_harness_adapter._environment("echo-hello-skills")
        snap = canon_snapshot("tbench_echo_hello_skills_harness")

        with env.compose_file.open() as f:
            actual = yaml.safe_load(f)
        snap.assert_match(actual, "synthesised_compose.json")


class TestHarnessSpecWireShape:
    def test_claude_code_spec_wire_shape(self, canon_snapshot):
        """ADR 0011 Pattern B: the spec's serialised shape is pinned, so a
        field added to ``HarnessSpec`` (or dropped from the shipped YAML)
        fails here rather than silently changing what a harness trial runs."""
        from tolokaforge_coding_harnesses import HARNESSES

        snap = canon_snapshot("tbench_echo_hello_harness")
        snap.assert_match(HARNESSES["claude-code"].model_dump(mode="json"), "harness_spec.json")


class TestTerminalBenchAdapterIntegrity:
    """Validate adapter output against source files without snapshots."""

    def test_instruction_matches_task_yaml(self, tbench_adapter, terminal_bench_tasks_dir):
        """Instruction in TaskConfig matches task.yaml content."""
        task = tbench_adapter.get_task("echo-hello")
        task_yaml_path = terminal_bench_tasks_dir / "echo-hello" / "task.yaml"

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

    def test_tool_source_extra_shape(self, tbench_adapter):
        """``ToolSource.extra`` carries exactly the two keys ``PerTrialRuntimeBackend`` + wrapper need."""
        td = tbench_adapter.to_task_description("echo-hello")
        extra = td.agent_tools[0].source.extra
        assert extra == {"service": "main", "compose_project_prefix": "tbench_"}

    def test_metadata_from_toml(self, tbench_adapter):
        """TaskDescription metadata contains difficulty and tags from task.toml."""
        td = tbench_adapter.to_task_description("echo-hello")
        assert td.metadata["difficulty"] == "easy"
        assert "shell" in td.metadata["tags"]
        assert td.metadata["verifier_timeout_sec"] == 30.0

    def test_task_id_filter(self, terminal_bench_tasks_dir):
        """task_ids param filters discovered tasks."""
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(terminal_bench_tasks_dir),
                "task_ids": ["nonexistent"],
            }
        )
        assert adapter.get_task_ids() == []
