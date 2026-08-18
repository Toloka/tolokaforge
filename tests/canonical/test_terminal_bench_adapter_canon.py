"""Canonical tests for TerminalBenchAdapter — compares against golden snapshots."""

import json
import re
from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _normalize_paths(obj):
    """Replace absolute paths + the content digest with stable placeholders.

    Adapter output contains two machine-varying strings: the absolute
    prefix leading to ``tests/data/terminal_bench_tasks/`` (differs by
    checkout root), and the staging directory name
    ``<staging_root>/echo-hello-<16-hex-digest>/…`` (differs by the
    per-test ``tmp_path``). Both are normalised to keep the snapshot
    portable across machines and CI.
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
    """Harness mode's synthesised substrate — layered image + two declared builds."""

    def test_synthesised_compose(self, tbench_harness_adapter, canon_snapshot):
        """The layered compose file carries a build-only base service and a CLI layer."""
        import yaml

        env = tbench_harness_adapter._environment("echo-hello")
        snap = canon_snapshot("tbench_echo_hello_harness")

        with env.compose_file.open() as f:
            actual = yaml.safe_load(f)
        snap.assert_match(actual, "synthesised_compose.json")

    def test_harness_dockerfile(self, tbench_harness_adapter, canon_snapshot):
        """The generated image layer is one FROM + one COPY + one RUN."""
        env = tbench_harness_adapter._environment("echo-hello")
        snap = canon_snapshot("tbench_echo_hello_harness")

        dockerfile = (env.staging_dir / "_harness" / "harness.Dockerfile").read_text()
        snap.assert_match({"dockerfile": dockerfile}, "harness_dockerfile.json")

    def test_harness_spec_wire_shape(self, canon_snapshot):
        """ADR 0011 Pattern B: the spec's serialised shape is pinned, so a
        field added to ``HarnessSpec`` (or dropped from the shipped YAML)
        fails here rather than silently changing what a harness trial runs."""
        from tolokaforge_coding_harnesses import HARNESSES

        snap = canon_snapshot("tbench_echo_hello_harness")
        snap.assert_match(HARNESSES["claude-code"].model_dump(mode="json"), "harness_spec.json")

    def test_synthesised_compose_uses_provider_env_defaults_when_run_config_declares_none(
        self, tbench_harness_adapter
    ):
        """A run config naming only the harness still gets that harness's
        provider envelope on the agent service — the CLI cannot reach its
        provider without it, and the operator should not have to re-derive
        an envelope the harness already declares."""
        import yaml

        env = tbench_harness_adapter._environment("echo-hello")
        with env.compose_file.open() as f:
            compose = yaml.safe_load(f)
        agent_env = compose["services"]["main"]["environment"]
        assert "ANTHROPIC_API_KEY=${TBENCH_PROVIDER_ANTHROPIC_API_KEY}" in agent_env
        assert "ANTHROPIC_BASE_URL=${TBENCH_PROVIDER_ANTHROPIC_BASE_URL}" in agent_env
        assert "sk-openrouter-test" not in env.compose_file.read_text()

    def test_synthesised_compose_carries_container_env(self, tbench_harness_adapter):
        """``HarnessSpec.container_env`` must reach the agent service's env
        block — every static hardening key claude-code declares (``IS_SANDBOX``,
        ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC``) has to survive the
        synthesis pass. This is a direct behavioural pin so a regression here
        is not merely a snapshot diff."""
        import yaml

        from tolokaforge_coding_harnesses import HARNESSES

        env = tbench_harness_adapter._environment("echo-hello")
        with env.compose_file.open() as f:
            compose = yaml.safe_load(f)
        agent_env = compose["services"]["main"]["environment"]
        assert isinstance(agent_env, list)
        for key, value in HARNESSES["claude-code"].container_env.items():
            assert (
                f"{key}={value}" in agent_env
            ), f"container_env pair {key}={value!r} missing from synthesised compose"


class TestTerminalBenchSkillsBundleCanon:
    """The substrate for a task pack that ships its own skills bundle.

    Pinned because the bundle changes what the agent can read: the ``COPY``
    line, its build-context exception, and the recorded bundle hash are the
    three ends that have to agree, and a reward earned with skills installed is
    not comparable to one earned without them.
    """

    def test_synthesised_compose(self, tbench_skills_harness_adapter, canon_snapshot):
        import yaml

        env = tbench_skills_harness_adapter._environment("echo-hello-skills")
        snap = canon_snapshot("tbench_echo_hello_skills_harness")

        with env.compose_file.open() as f:
            actual = yaml.safe_load(f)
        snap.assert_match(actual, "synthesised_compose.json")

    def test_harness_dockerfile_copies_the_bundle(
        self, tbench_skills_harness_adapter, canon_snapshot
    ):
        """The CLI install, then the pack's own skills — nothing from the host."""
        env = tbench_skills_harness_adapter._environment("echo-hello-skills")
        snap = canon_snapshot("tbench_echo_hello_skills_harness")

        dockerfile = (env.staging_dir / "_harness" / "harness.Dockerfile").read_text()
        snap.assert_match({"dockerfile": dockerfile}, "harness_dockerfile.json")

    def test_build_context_readmits_the_bundle(self, tbench_skills_harness_adapter, canon_snapshot):
        """``.dockerignore`` excludes the staging tree; the bundle is an exception."""
        env = tbench_skills_harness_adapter._environment("echo-hello-skills")
        snap = canon_snapshot("tbench_echo_hello_skills_harness")

        dockerignore = (env.staging_dir / ".dockerignore").read_text()
        snap.assert_match({"dockerignore": dockerignore}, "dockerignore.json")

    def test_metadata_records_the_bundle(self, tbench_skills_harness_adapter):
        """The hash is on the artifact, so a bundle edit is visible in the run
        record rather than only in the image."""
        metadata = tbench_skills_harness_adapter.to_task_description("echo-hello-skills").metadata
        assert len(metadata["harness_skills_bundle_sha"]) == 64


class TestTerminalBenchAdapterIntegrity:
    """Validate adapter output against source files without snapshots."""

    def test_instruction_matches_task_yaml(self, tbench_adapter, terminal_bench_tasks_dir):
        """Instruction in TaskConfig matches task.yaml content."""
        task = tbench_adapter.get_task("echo-hello")
        task_yaml_path = terminal_bench_tasks_dir / "echo-hello" / "task.yaml"

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
