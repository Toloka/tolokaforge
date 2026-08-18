"""Unit tests for terminal-bench adapter and Docker Compose exec wrapper."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from tolokaforge_coding_harnesses.testing import (
    FakeEntryPoint,
    build_plugin,
    bundle_yaml,
    install_plugins,
)

from tolokaforge.docker.policy import Capability
from tolokaforge.docker.stacks.core import core_stack
from tolokaforge.runner.models import (
    AdapterType,
    EnvironmentManifest,
    EnvironmentPatch,
    InvocationStyle,
    NetworkPolicy,
    ToolSchema,
    ToolSource,
)
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    ToolConfigurationError,
    ToolExecutionError,
    ToolFactory,
    ToolLifecycleContext,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("env_backed_secrets")]


# =============================================================================
# Models: new enum values
# =============================================================================


class TestAdapterTypeEnum:
    def test_terminal_bench_value(self):
        assert AdapterType.TERMINAL_BENCH == "terminal_bench"
        assert AdapterType.TERMINAL_BENCH.value == "terminal_bench"

    def test_all_adapter_types_present(self):
        names = {e.value for e in AdapterType}
        assert "terminal_bench" in names
        assert "native" in names


class TestInvocationStyleEnum:
    def test_docker_compose_exec_value(self):
        assert InvocationStyle.DOCKER_COMPOSE_EXEC == "docker_compose_exec"

    def test_all_styles_present(self):
        names = {e.value for e in InvocationStyle}
        assert "docker_compose_exec" in names
        assert "tau_sync" in names
        assert "mcp_async" in names
        assert "mcp_server" in names


class TestToolSourceExtra:
    def test_extra_defaults_to_empty_dict(self):
        source = ToolSource(toolset="t", module_path="m", class_name="c")
        assert source.extra == {}

    def test_extra_accepts_arbitrary_data(self):
        source = ToolSource(
            toolset="terminal_bench",
            module_path="",
            class_name="bash",
            invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
            extra={
                "compose_file": "docker-compose.yaml",
                "task_dir": "/tasks/test",
                "service": "main",
                "env_vars": {"FOO": "bar"},
            },
        )
        assert source.extra["compose_file"] == "docker-compose.yaml"
        assert source.extra["env_vars"]["FOO"] == "bar"

    def test_extra_roundtrip_serialization(self):
        source = ToolSource(
            toolset="t",
            module_path="m",
            class_name="c",
            extra={"key": "value"},
        )
        dumped = source.model_dump()
        restored = ToolSource.model_validate(dumped)
        assert restored.extra == {"key": "value"}


# =============================================================================
# DockerComposeExecToolWrapper
# =============================================================================


@pytest.fixture
def wrapper_schema():
    """Minimal ToolSchema for the bash tool with the exec-only ``extra`` shape."""
    return ToolSchema(
        name="bash",
        description="Execute bash command",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        category="compute",
        timeout_s=60.0,
        source=ToolSource(
            toolset="terminal_bench",
            module_path="",
            class_name="bash",
            invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
            extra={"service": "main", "compose_project_prefix": "tbench_"},
        ),
    )


@pytest.fixture
def wrapper(wrapper_schema):
    return DockerComposeExecToolWrapper(
        tool_schema=wrapper_schema,
        service="main",
        compose_project_prefix="tbench_",
    )


class TestDockerComposeExecWrapperInit:
    def test_records_service_and_prefix_only(self, wrapper):
        assert wrapper._service == "main"
        assert wrapper._project_prefix == "tbench_"
        assert wrapper._trial_id is None
        assert wrapper._container is None

    def test_start_resolves_container_no_subprocess(self, wrapper_schema):
        w = DockerComposeExecToolWrapper(
            tool_schema=wrapper_schema,
            service="main",
            compose_project_prefix="tbench_",
        )
        with (
            patch("subprocess.run") as run_mock,
            patch("subprocess.Popen") as popen_mock,
        ):
            w.start(ToolLifecycleContext(trial_id="task-1:0"))
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        assert w._trial_id == "task-1:0"
        assert w._container == "tbench_task-1_0_main"


class TestDockerComposeExecWrapperExec:
    def test_exec_sync_success(self, wrapper):
        wrapper.start(ToolLifecycleContext(trial_id="task-1:0"))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="hello world\n", stderr=""
            )
            result = wrapper._exec_sync("echo hello world", 30.0)
            assert result == "hello world\n"
            argv = mock_run.call_args.args[0]
            assert argv == [
                "docker",
                "exec",
                "-i",
                "tbench_task-1_0_main",
                "bash",
                "-c",
                "echo hello world",
            ]

    def test_exec_sync_nonzero_exit(self, wrapper):
        wrapper.start(ToolLifecycleContext(trial_id="task-1:0"))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="partial", stderr="error msg"
            )
            result = wrapper._exec_sync("bad cmd", 30.0)
            assert "partial" in result
            assert "[exit code: 1]" in result
            assert "error msg" in result

    def test_exec_before_start_fails_loud(self, wrapper):
        with pytest.raises(ToolExecutionError, match="container name unresolved"):
            wrapper._exec_sync("echo hi", 30.0)

    @pytest.mark.asyncio
    async def test_execute_async(self, wrapper):
        wrapper.start(ToolLifecycleContext(trial_id="task-1:0"))
        with patch.object(wrapper, "_exec_sync", return_value="async result") as mock:
            result = await wrapper.execute({"command": "ls"})
            assert result == "async result"
            mock.assert_called_once_with("ls", 60.0)


# =============================================================================
# ToolFactory: DOCKER_COMPOSE_EXEC dispatch
# =============================================================================


class TestToolFactoryDockerComposeExec:
    @pytest.fixture
    def factory(self):
        db_client = MagicMock()
        return ToolFactory(db_client=db_client, trial_id="test:0")

    def test_create_docker_compose_exec_wrapper(self, factory):
        schema = ToolSchema(
            name="bash",
            description="Run command",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                toolset="terminal_bench",
                module_path="",
                class_name="bash",
                invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                extra={"service": "main", "compose_project_prefix": "tbench_"},
            ),
        )
        wrapper = factory._create_wrapper(schema)
        assert isinstance(wrapper, DockerComposeExecToolWrapper)
        assert wrapper._service == "main"
        assert wrapper._project_prefix == "tbench_"

    def test_missing_service_raises(self, factory):
        schema = ToolSchema(
            name="bash",
            description="Run command",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                toolset="terminal_bench",
                module_path="",
                class_name="bash",
                invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                extra={"compose_project_prefix": "tbench_"},
            ),
        )
        with pytest.raises(ToolConfigurationError, match=r"service.*compose_project_prefix"):
            factory._create_wrapper(schema)

    def test_missing_compose_project_prefix_raises(self, factory):
        schema = ToolSchema(
            name="bash",
            description="Run command",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                toolset="terminal_bench",
                module_path="",
                class_name="bash",
                invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                extra={"service": "main"},
            ),
        )
        with pytest.raises(ToolConfigurationError, match=r"service.*compose_project_prefix"):
            factory._create_wrapper(schema)


# =============================================================================
# core_stack: DinD configuration
# =============================================================================


class TestCoreStackDinD:
    def test_default_no_dind(self):
        """Without enable_dind, stack has 2 services (db + runner)."""
        stack = core_stack()
        assert "db-service" in stack.services
        assert "runner" in stack.services
        assert "dind" not in stack.services

    def test_enable_dind_adds_sidecar(self):
        """With enable_dind, stack gets a dind service."""
        stack = core_stack(enable_dind=True)
        assert "dind" in stack.services
        assert "db-service" in stack.services
        assert "runner" in stack.services

    def test_dind_is_privileged(self):
        stack = core_stack(enable_dind=True)
        dind = stack.services["dind"]
        assert dind.privileged is True

    def test_dind_uses_prebuilt_image(self):
        stack = core_stack(enable_dind=True)
        dind = stack.services["dind"]
        assert dind.use_prebuilt_image is True
        assert dind.prebuilt_tag == "dind"
        assert dind.image_name == "docker"

    def test_runner_has_docker_host_env(self):
        stack = core_stack(enable_dind=True)
        runner = stack.services["runner"]
        assert "DOCKER_HOST" in runner.environment
        assert runner.environment["DOCKER_HOST"] == "tcp://tolokaforge-dind:2375"

    def test_runner_depends_on_dind(self):
        stack = core_stack(enable_dind=True)
        runner = stack.services["runner"]
        assert "dind" in runner.depends_on

    def test_runner_shares_workspace_volume(self):
        stack = core_stack(enable_dind=True)
        runner = stack.services["runner"]
        dind = stack.services["dind"]

        runner_vol_targets = [m.target for m in runner.mounts]
        dind_vol_targets = [m.target for m in dind.mounts]

        assert "/workspace" in runner_vol_targets
        assert "/workspace" in dind_vol_targets

    def test_no_dind_no_docker_host(self):
        stack = core_stack(enable_dind=False)
        runner = stack.services["runner"]
        assert "DOCKER_HOST" not in runner.environment

    def test_no_dind_runner_not_privileged(self):
        stack = core_stack(enable_dind=False)
        runner = stack.services["runner"]
        assert runner.privileged is False

    def test_no_dind_runner_has_strict_caps(self):
        stack = core_stack(enable_dind=False)
        runner = stack.services["runner"]
        assert runner.resources is not None
        assert runner.resources.cap_drop == [Capability.ALL]


class TestTerminalBenchAdapterDockerStackRequirements:
    """Adapter declares one :class:`ComposeImageBuild` per discovered task."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    def test_image_builds_one_per_task(self, fixture_dir, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        task_ids = adapter.get_task_ids()

        reqs = adapter.docker_stack_requirements()
        assert reqs.mount_docker_socket is False
        assert reqs.task_pack_mounts == []
        assert reqs.extra_runner_binds == []
        assert [b.service for b in reqs.image_builds] == [
            adapter._environment(tid).agent_service for tid in task_ids
        ]
        assert [b.compose_file for b in reqs.image_builds] == [
            adapter._environment(tid).compose_file for tid in task_ids
        ]

    def test_prebuild_images_false_returns_no_builds(self, fixture_dir, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "staging_root": str(tmp_path),
                "prebuild_images": False,
            }
        )
        reqs = adapter.docker_stack_requirements()
        assert reqs.image_builds == []

    def test_to_core_stack_kwargs_omits_image_builds(self, tmp_path):
        """``image_builds`` is the orchestrator's declarative seam, never a stack kwarg."""
        from tolokaforge.adapters.base import (
            ComposeImageBuild,
            DockerStackRequirements,
        )

        empty = DockerStackRequirements().to_core_stack_kwargs()
        assert empty == {}

        compose = tmp_path / "docker-compose.yaml"
        compose.write_text("services: {}\n")
        with_builds = DockerStackRequirements(
            image_builds=[ComposeImageBuild(compose_file=compose, service="main")]
        ).to_core_stack_kwargs()
        assert with_builds == {}


class TestTerminalBenchAdapterRemovedParams:
    """Removed params fail loud in ``__init__`` with a rename hint."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    def test_runner_task_dir_removed(self, fixture_dir):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        with pytest.raises(ValueError, match=r"runner_task_dir.*staging_root"):
            TerminalBenchAdapter(
                {"terminal_bench_dir": str(fixture_dir), "runner_task_dir": "/mounted"}
            )

    def test_logs_host_root_removed(self, fixture_dir):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        with pytest.raises(ValueError, match=r"logs_host_root.*_logs"):
            TerminalBenchAdapter(
                {"terminal_bench_dir": str(fixture_dir), "logs_host_root": "/tmp/x"}
            )


class TestTerminalBenchAdapterEnvironmentManifest:
    """``get_task`` emits an :class:`EnvironmentPatch`; ``to_task_description`` emits the resolved manifest.

    Structural agreement, not object identity — one patch + one resolver.
    """

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    @pytest.fixture
    def adapter(self, fixture_dir, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        return TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )

    def test_task_config_carries_environment_patch(self, adapter):
        task = adapter.get_task("echo-hello")
        assert isinstance(task.environment_manifest, EnvironmentPatch)
        stack = task.environment_manifest.stack
        assert stack is not None
        env = adapter._environment("echo-hello")
        assert stack.compose_file == env.compose_file
        assert stack.runner_service == "runner"
        assert task.environment_manifest.network_policy is NetworkPolicy.FULL_INTERNET

    def test_task_description_carries_resolved_manifest(self, adapter):
        td = adapter.to_task_description("echo-hello")
        manifest = td.environment_manifest
        assert isinstance(manifest, EnvironmentManifest)
        env = adapter._environment("echo-hello")
        assert manifest.compose_file == env.compose_file
        assert manifest.runner_service == "runner"
        assert set(manifest.services) == {"runner", "db-service", env.agent_service}
        assert {name: spec.isolation for name, spec in manifest.services.items()} == {
            "runner": "ephemeral",
            "db-service": "ephemeral",
            env.agent_service: "ephemeral",
        }
        assert manifest.requires_per_trial is True
        assert manifest.network_policy is NetworkPolicy.FULL_INTERNET

    def test_patch_and_manifest_point_at_same_compose_file(self, adapter):
        task = adapter.get_task("echo-hello")
        td = adapter.to_task_description("echo-hello")
        assert task.environment_manifest.stack.compose_file == td.environment_manifest.compose_file

    def test_tool_source_extra_is_two_key_shape(self, adapter):
        td = adapter.to_task_description("echo-hello")
        extra = td.agent_tools[0].source.extra
        env = adapter._environment("echo-hello")
        assert extra == {"service": env.agent_service, "compose_project_prefix": "tbench_"}


class TestTerminalBenchAdapterInstructionlessTask:
    """A pack whose instruction carries no text pins no opener, so the simulator
    writes turn 1.

    The key can be absent, null, or whitespace, and ``discover_tasks`` reports
    all three as a blank instruction. A blank ``initial_user_message`` is a
    task-contract error whose remedies — omit the key in ``task.yaml``, return
    ``None`` from ``get_task()`` — a terminal-bench pack cannot carry out for
    itself. The adapter must leave the field unset rather than forward the blank.
    """

    @pytest.mark.parametrize(
        "task_yaml",
        [
            pytest.param({"difficulty": "easy"}, id="instruction_absent"),
            pytest.param(
                {"difficulty": "easy", "instruction": "   \n  "}, id="instruction_whitespace"
            ),
            pytest.param({"difficulty": "easy", "instruction": None}, id="instruction_null"),
        ],
    )
    def test_get_task_leaves_initial_user_message_unset(self, tmp_path, task_yaml) -> None:
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        fixture_dir = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"
        tbench_dir = tmp_path / "tasks"
        shutil.copytree(fixture_dir, tbench_dir)
        (tbench_dir / "echo-hello" / "task.yaml").write_text(yaml.safe_dump(task_yaml))

        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(tbench_dir), "staging_root": str(tmp_path / "staging")}
        )

        assert adapter.get_task("echo-hello").initial_user_message is None


class TestTerminalBenchAdapterNoSubprocess:
    """Both accessors must stay daemon-free — canonical lane + ``--dry-run`` depend on it."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    def test_get_task_invokes_no_subprocess(self, fixture_dir, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        with (
            patch("subprocess.run") as run_mock,
            patch("subprocess.Popen") as popen_mock,
            patch("subprocess.check_call") as check_call_mock,
            patch("subprocess.check_output") as check_output_mock,
        ):
            adapter.get_task("echo-hello")
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        check_call_mock.assert_not_called()
        check_output_mock.assert_not_called()

    def test_to_task_description_invokes_no_subprocess(self, fixture_dir, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        with (
            patch("subprocess.run") as run_mock,
            patch("subprocess.Popen") as popen_mock,
            patch("subprocess.check_call") as check_call_mock,
            patch("subprocess.check_output") as check_output_mock,
        ):
            adapter.to_task_description("echo-hello")
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        check_call_mock.assert_not_called()
        check_output_mock.assert_not_called()


class TestTerminalBenchTasksSelectPerTrialBackend:
    """Backend selection is task-driven: an ``all-ephemeral`` manifest picks per-trial."""

    def test_select_backend_returns_per_trial(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        from tolokaforge.core.models import (
            EvaluationConfig,
            ModelConfig,
            OrchestratorConfig,
            RunConfig,
        )
        from tolokaforge.core.orchestrator import Orchestrator

        fixture_dir = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"
        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        run_config = RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir=str(tmp_path / "out")),
        )
        orch = Orchestrator(run_config)
        orch.adapter = adapter
        task = MagicMock()
        task.task_id = "echo-hello"
        orch.tasks = [task]

        assert orch._select_backend_from_tasks() == "per_trial"


# =============================================================================
# Task parser
# =============================================================================


class TestTaskParser:
    @pytest.fixture
    def fixture_dir(self):
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    def test_discover_finds_echo_hello(self, fixture_dir):
        from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

        tasks = discover_tasks(fixture_dir)
        assert "echo-hello" in tasks

    def test_parsed_metadata(self, fixture_dir):
        from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

        tasks = discover_tasks(fixture_dir)
        meta = tasks["echo-hello"]
        assert meta.difficulty == "easy"
        assert meta.agent_timeout_sec == 60.0
        assert meta.verifier_timeout_sec == 30.0
        assert meta.cpus == 1
        assert meta.memory_mb == 512
        assert "shell" in meta.tags

    def test_parsed_instruction(self, fixture_dir):
        from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

        tasks = discover_tasks(fixture_dir)
        meta = tasks["echo-hello"]
        assert "Hello, World!" in meta.instruction

    def test_compose_file_path(self, fixture_dir):
        from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

        tasks = discover_tasks(fixture_dir)
        meta = tasks["echo-hello"]
        assert meta.compose_file.name == "docker-compose.yaml"
        assert meta.compose_file.exists()

    def test_empty_dir_returns_no_tasks(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

        tasks = discover_tasks(tmp_path)
        assert tasks == {}


# =============================================================================
# compose_synthesis: task environment materialisation
# =============================================================================


def _write_task(
    tmp_path: Path, task_id: str, compose_body: dict, extras: dict[str, str] | None = None
):
    """Build a TerminalBenchTask on disk under ``tmp_path`` with the given compose body."""
    import yaml as _yaml
    from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask

    task_dir = tmp_path / task_id
    task_dir.mkdir()
    (task_dir / "docker-compose.yaml").write_text(_yaml.safe_dump(compose_body))
    for rel, content in (extras or {}).items():
        target = task_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return TerminalBenchTask(
        task_id=task_id,
        task_dir=task_dir,
        compose_file=task_dir / "docker-compose.yaml",
        instruction="test",
    )


def _load_synthesised(env) -> dict:
    import yaml as _yaml

    with env.compose_file.open() as f:
        return _yaml.safe_load(f)


class TestComposeSynthesisFixBillingHolds:
    """The synthesised YAML for a real example task locks the emit contract."""

    def test_emitted_shape(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )
        from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

        examples_dir = Path(__file__).parent.parent.parent / "examples" / "terminal_bench"
        tasks = discover_tasks(examples_dir)
        assert "fix-billing-holds" in tasks
        env = materialise_task_environment(tasks["fix-billing-holds"], staging_root=tmp_path)

        compose = _load_synthesised(env)
        assert set(compose["services"]) == {"runner", "db-service", "main"}
        main = compose["services"]["main"]
        assert main["image"] == "tbench-fix-billing-holds:local"
        assert main["container_name"] == "tbench_${TOLOKAFORGE_TRIAL_SLUG}_main"
        assert main["volumes"] == ["./tests:/tests", "./_logs:/logs"]

        text = env.compose_file.read_text()
        assert "T_BENCH_" not in text
        assert "${CPUS}" not in text
        assert "${MEMORY}" not in text

    def test_environment_manifest_accepts_emitted_file(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )
        from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

        from tolokaforge.runner.models import EnvironmentManifest

        examples_dir = Path(__file__).parent.parent.parent / "examples" / "terminal_bench"
        tasks = discover_tasks(examples_dir)
        env = materialise_task_environment(tasks["fix-billing-holds"], staging_root=tmp_path)

        manifest = EnvironmentManifest(compose_file=env.compose_file, runner_service="runner")
        assert manifest.runner_service == "runner"


class TestComposeSynthesisAgentServiceResolution:
    def test_sole_service_is_agent(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "app-only",
            {
                "services": {
                    "app": {
                        "image": "python:3.11-slim",
                        "command": ["sleep", "infinity"],
                    }
                }
            },
        )
        staging_root = tmp_path / "staging"
        env = materialise_task_environment(meta, staging_root=staging_root)

        assert env.agent_service == "app"
        compose = _load_synthesised(env)
        assert set(compose["services"]) == {"runner", "db-service", "app"}
        assert (
            compose["services"]["app"]["container_name"] == "tbench_${TOLOKAFORGE_TRIAL_SLUG}_app"
        )

    def test_multi_service_without_main_raises(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "multi",
            {
                "services": {
                    "alice": {"image": "python:3.11-slim", "command": ["sleep", "infinity"]},
                    "bob": {"image": "python:3.11-slim", "command": ["sleep", "infinity"]},
                }
            },
        )
        with pytest.raises(ValueError, match=r"alice.*bob|bob.*alice"):
            materialise_task_environment(meta, staging_root=tmp_path / "staging")


class TestComposeSynthesisResourceLimits:
    """Regression lock for the M3 CPUS/MEMORY resolution."""

    def test_deploy_limits_resolve_from_task_metadata(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "deploy-limits",
            {
                "services": {
                    "main": {
                        "image": "${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}",
                        "command": ["sleep", "infinity"],
                        "deploy": {
                            "resources": {
                                "limits": {"cpus": "${CPUS}", "memory": "${MEMORY}"},
                            }
                        },
                    }
                }
            },
        )
        env = materialise_task_environment(meta, staging_root=tmp_path / "staging")

        compose = _load_synthesised(env)
        limits = compose["services"]["main"]["deploy"]["resources"]["limits"]
        assert limits["cpus"] == "2"
        assert limits["memory"] == "4096M"


class TestComposeSynthesisStaging:
    def test_idempotent_second_call_returns_same_path(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "idempotent",
            {"services": {"main": {"image": "python:3.11-slim"}}},
        )
        staging_root = tmp_path / "staging"

        first = materialise_task_environment(meta, staging_root=staging_root)
        second = materialise_task_environment(meta, staging_root=staging_root)

        assert first.staging_dir == second.staging_dir
        assert first.compose_file == second.compose_file
        # Only one subdirectory under staging_root — the digest-named one.
        subdirs = [p for p in staging_root.iterdir() if p.is_dir()]
        assert len(subdirs) == 1

    def test_root_run_tests_promoted_to_tests_test_sh(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "promote-script",
            {"services": {"main": {"image": "python:3.11-slim"}}},
            extras={"run-tests.sh": "#!/bin/bash\necho ok\n"},
        )
        env = materialise_task_environment(meta, staging_root=tmp_path / "staging")

        promoted = env.staging_dir / "tests" / "test.sh"
        assert promoted.exists()
        assert "echo ok" in promoted.read_text()

    def test_existing_tests_test_sh_wins(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "existing-script",
            {"services": {"main": {"image": "python:3.11-slim"}}},
            extras={
                "run-tests.sh": "#!/bin/bash\necho root\n",
                "tests/test.sh": "#!/bin/bash\necho pack\n",
            },
        )
        env = materialise_task_environment(meta, staging_root=tmp_path / "staging")

        assert "echo pack" in (env.staging_dir / "tests" / "test.sh").read_text()

    def test_log_dirs_created(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "log-dirs",
            {"services": {"main": {"image": "python:3.11-slim"}}},
        )
        env = materialise_task_environment(meta, staging_root=tmp_path / "staging")

        assert (env.staging_dir / "_logs" / "verifier").is_dir()
        assert (env.staging_dir / "_logs" / "agent").is_dir()

    def test_pycache_excluded(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "no-cache",
            {"services": {"main": {"image": "python:3.11-slim"}}},
            extras={
                "tests/__pycache__/whatever.pyc": "bytes",
                "tests/test_something.py": "def test(): pass\n",
            },
        )
        env = materialise_task_environment(meta, staging_root=tmp_path / "staging")

        assert not (env.staging_dir / "tests" / "__pycache__").exists()
        assert (env.staging_dir / "tests" / "test_something.py").exists()


class TestComposeSynthesisReservedNameCollision:
    def test_task_declares_runner_service_raises(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "collides-with-runner",
            {
                "services": {
                    "main": {"image": "python:3.11-slim"},
                    "runner": {"image": "python:3.11-slim"},
                }
            },
        )
        with pytest.raises(
            ValueError, match="runner.*collides-with-runner|collides-with-runner.*runner"
        ):
            materialise_task_environment(meta, staging_root=tmp_path / "staging")

    def test_task_declares_db_service_raises(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "collides-with-db",
            {
                "services": {
                    "main": {"image": "python:3.11-slim"},
                    "db-service": {"image": "postgres:16"},
                }
            },
        )
        with pytest.raises(ValueError, match="db-service"):
            materialise_task_environment(meta, staging_root=tmp_path / "staging")


class TestComposeSynthesisFloatingTagRejected:
    def test_latest_tag_rejected(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "float-tag",
            {"services": {"main": {"image": "python:3.11-slim"}}},
        )
        with pytest.raises(ValueError, match="floating tag"):
            materialise_task_environment(
                meta, staging_root=tmp_path / "staging", image_tag="latest"
            )


class TestComposeSynthesisNoSubprocess:
    """Materialisation must stay daemon-free — the canonical adapter lane and dry-run depend on it."""

    def test_no_subprocess_invoked(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "no-daemon",
            {"services": {"main": {"image": "python:3.11-slim"}}},
        )
        with patch("subprocess.Popen") as popen_mock:
            materialise_task_environment(meta, staging_root=tmp_path / "staging")
        popen_mock.assert_not_called()


# =============================================================================
# Harness mode: model-name resolution, image layering, skills, provider env
# =============================================================================


class TestHarnessModelPrefix:
    """A vendor CLI reaches OpenRouter through ``*_BASE_URL``, not litellm."""

    def test_openrouter_prefix_stripped_for_a_vendor_cli(self):
        """Claude-code no longer emits ``--model`` on the CLI (env quartet
        carries the model), but the resolved model name must still land in
        the env quartet with the ``openrouter/`` prefix stripped."""
        from tolokaforge_coding_harnesses import harness_command

        command = harness_command("claude-code", "go", "openrouter/anthropic/claude-sonnet-4-6")
        assert "export ANTHROPIC_MODEL=anthropic/claude-sonnet-4-6 " in command
        assert "openrouter/anthropic/claude-sonnet-4-6" not in command

    def test_codex_gets_the_bare_model_name(self):
        """codex refuses ``openai/gpt-5-mini`` as off-catalog and drops
        OPENAI_BASE_URL — the reference fix is the last-path-segment strip."""
        import shlex

        from tolokaforge_coding_harnesses import harness_command

        argv = shlex.split(harness_command("codex", "go", "openrouter/openai/gpt-5-mini"))
        assert argv[argv.index("--model") + 1] == "gpt-5-mini"

    def test_gemini_cli_gets_the_bare_model_name(self):
        import shlex

        from tolokaforge_coding_harnesses import harness_command

        argv = shlex.split(
            harness_command("gemini-cli", "go", "openrouter/google/gemini-2.5-flash")
        )
        assert argv[argv.index("--model") + 1] == "gemini-2.5-flash"

    def test_shipped_opencode_spec_declares_strip_openrouter_prefix_false(self):
        """Opencode's config template defines a provider literally named
        ``openrouter`` — stripping the prefix would re-route
        ``openrouter/<vendor>/<model>`` to a provider its config never
        declared. Lock the shipped default."""
        from tolokaforge_coding_harnesses import HARNESSES

        assert HARNESSES["opencode"].strip_openrouter_prefix is False

    def test_opencode_preserves_openrouter_prefix_so_config_provider_block_wins(self):
        """The user-visible behavior of the flag: an ``openrouter/vendor/model``
        slug reaches the opencode CLI intact so opencode routes to its
        ``openrouter`` provider block. If this test flips, opencode 401s
        again on Muse-family models — opencode's config declares no
        ``meta`` / ``qwen`` provider, so a stripped slug re-routes into a
        nonexistent block."""
        from tolokaforge_coding_harnesses import harness_model

        assert (
            harness_model("openrouter/meta/muse-glimmer-30b", "opencode")
            == "openrouter/meta/muse-glimmer-30b"
        )

    def test_default_strip_openrouter_prefix_removes_the_prefix(self):
        """Default preserves the pre-existing behavior for every harness
        besides opencode — kimi-code, claude-code, codex, grok-build,
        gemini-cli all rely on the strip."""
        from tolokaforge_coding_harnesses import harness_model

        assert (
            harness_model("openrouter/anthropic/claude-sonnet-5", "claude-code")
            == "anthropic/claude-sonnet-5"
        )

    def test_a_bare_model_name_is_untouched_for_claude_code(self):
        from tolokaforge_coding_harnesses import harness_model

        assert (
            harness_model("anthropic/claude-sonnet-4-6", "claude-code")
            == "anthropic/claude-sonnet-4-6"
        )

    def test_only_a_leading_prefix_is_stripped(self):
        from tolokaforge_coding_harnesses import harness_model

        assert harness_model("vendor/openrouter/x") == "vendor/openrouter/x"

    def test_the_engine_loop_never_rewrites_the_model(self, tmp_path):
        """litellm needs the prefix to pick its OpenRouter handler, and the
        engine loop hands the run config's model straight to litellm — so the
        adapter must not touch it (and publishes no CLI command at all)."""
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        fixture_dir = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"
        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        metadata = adapter.to_task_description("echo-hello").metadata
        assert "agent_harness_command" not in metadata
        assert "agent_harness_model" not in metadata


def _harness_task(tmp_path: Path, task_id: str, *, with_build: bool):
    main: dict = {"image": "${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}"}
    if with_build:
        main["build"] = {"context": "./environment"}
    return _write_task(
        tmp_path,
        task_id,
        {"services": {"main": main}},
        extras={"environment/Dockerfile": "FROM python:3.11-slim\n"},
    )


class TestComposeSynthesisHarnessLayer:
    """Harness mode splits the agent image into base + CLI layer."""

    def test_default_harness_leaves_compose_and_staging_untouched(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "plain", with_build=True)
        env = materialise_task_environment(meta, staging_root=tmp_path / "staging")

        compose = _load_synthesised(env)
        assert set(compose["services"]) == {"main", "runner", "db-service"}
        assert compose["services"]["main"]["image"] == "tbench-plain:local"
        assert env.base_build_service is None
        assert not (env.staging_dir / "_harness").exists()

    def test_layered_compose_declares_base_and_layer(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "layered", with_build=True)
        env = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )

        compose = _load_synthesised(env)
        assert set(compose["services"]) == {"main", "main-base", "runner", "db-service"}

        base = compose["services"]["main-base"]
        assert base["image"] == "tbench-layered:local"
        assert base["build"] == {"context": "./environment"}
        assert base["profiles"] == ["tolokaforge-build"]

        main = compose["services"]["main"]
        assert main["image"] == "tbench-layered:local-claude-code-2.1.233"
        assert main["build"] == {
            "context": ".",
            "dockerfile": "_harness/harness.Dockerfile",
        }
        assert env.base_build_service == "main-base"

    def test_base_service_never_starts_with_the_stack(self, tmp_path):
        """A compose profile is what keeps the build-only service out of ``up``."""
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "profiled", with_build=True)
        env = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="codex"
        )
        compose = _load_synthesised(env)
        assert compose["services"]["main-base"]["profiles"]
        assert "profiles" not in compose["services"]["main"]

    def test_harness_dockerfile_layers_on_the_base_image(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "dockerfile", with_build=True)
        env = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="gemini-cli"
        )

        dockerfile = (env.staging_dir / "_harness" / "harness.Dockerfile").read_text()
        assert dockerfile.splitlines()[0] == "FROM tbench-dockerfile:local"
        assert "install-harness.sh npm @google/gemini-cli 0.55.1" in dockerfile
        assert (env.staging_dir / "_harness" / "install-harness.sh").exists()

    def test_task_without_build_context_declares_no_base_service(self, tmp_path):
        """A pre-built task image has nothing for the orchestrator to build first."""
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "prebuilt", with_build=False)
        env = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )

        compose = _load_synthesised(env)
        assert "main-base" not in compose["services"]
        assert env.base_build_service is None
        assert compose["services"]["main"]["image"] == "tbench-prebuilt:local-claude-code-2.1.233"

    def test_registry_base_is_pulled_not_built(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "registry", with_build=True)
        env = materialise_task_environment(
            meta,
            staging_root=tmp_path / "staging",
            image_registry="reg.example/tbench",
            image_tag="v1",
            agent_harness="codex",
        )

        compose = _load_synthesised(env)
        assert "main-base" not in compose["services"]
        assert env.base_build_service is None
        assert (
            compose["services"]["main"]["image"] == "reg.example/tbench/registry:v1-codex-0.147.0"
        )
        dockerfile = (env.staging_dir / "_harness" / "harness.Dockerfile").read_text()
        assert dockerfile.startswith("FROM reg.example/tbench/registry:v1\n")

    def test_switching_harness_changes_the_staging_digest(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "digest", with_build=True)
        a = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )
        b = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="codex"
        )
        assert a.staging_dir != b.staging_dir

    def test_unknown_harness_rejected(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "bad", with_build=True)
        with pytest.raises(ValueError, match="agent_harness"):
            materialise_task_environment(
                meta, staging_root=tmp_path / "staging", agent_harness="nope"
            )

    def test_task_declaring_base_service_name_raises_under_harness_mode(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "collide",
            {
                "services": {
                    "main": {"image": "python:3.11-slim"},
                    "main-base": {"image": "busybox"},
                }
            },
        )
        with pytest.raises(ValueError, match="main-base"):
            materialise_task_environment(
                meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
            )

    def test_the_same_task_is_fine_without_a_harness(self, tmp_path):
        """No harness, no injected base service — so nothing to collide with."""
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "collide-ok",
            {
                "services": {
                    "main": {"image": "python:3.11-slim"},
                    "main-base": {"image": "busybox"},
                }
            },
        )
        env = materialise_task_environment(meta, staging_root=tmp_path / "staging")
        assert _load_synthesised(env)["services"]["main-base"] == {"image": "busybox"}

    def test_layered_manifest_still_validates(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "manifest", with_build=True)
        env = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )
        manifest = EnvironmentManifest(compose_file=env.compose_file, runner_service="runner")
        assert "main-base" in manifest.load_compose()["services"]


def _skills_task(tmp_path: Path, task_id: str, declared: str | None, files: dict[str, str]):
    """A task pack on disk declaring *declared* as its skills bundle.

    Goes through ``discover_tasks`` rather than constructing the dataclass, so
    the declaration is validated the way a real pack's is.
    """
    from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

    task_dir = tmp_path / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build:\n      context: ./environment\n"
        "    image: ${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}\n"
    )
    (task_dir / "environment").mkdir()
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.11-slim\n")
    declaration = "" if declared is None else f"harness_skills_dir: {declared}\n"
    (task_dir / "task.yaml").write_text(f"instruction: do the thing\n{declaration}")
    for rel, content in files.items():
        target = task_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return discover_tasks(tmp_path)[task_id]


class TestHarnessSkillsBundle:
    """A task pack ships its own skills; the operator's home directory never does.

    The parity policy refuses the out-of-tree host's ``~/.claude/skills``
    smuggling because a
    reward that depends on the eval machine's home directory is not a
    reproducible reward. These tests lock the replacement: the bundle is
    declared by the pack, contained inside it, copied only by a harness that
    reads skills, and hashed onto the artifact when it is.
    """

    def test_a_declared_bundle_is_parsed_as_declared(self, tmp_path):
        meta = _skills_task(tmp_path, "declared", "skills/", {"skills/README.md": "s"})
        assert meta.harness_skills_dir == "skills/"

    def test_no_declaration_is_no_bundle(self, tmp_path):
        meta = _skills_task(tmp_path, "bare", None, {})
        assert meta.harness_skills_dir is None

    def test_an_absolute_path_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="absolute path"):
            _skills_task(tmp_path, "absolute", "/etc/skills", {})

    def test_a_traversal_out_of_the_pack_is_refused(self, tmp_path):
        """``../`` is the direct route back to the operator's own files."""
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "SKILL.md").write_text("smuggled")
        with pytest.raises(ValueError, match="outside the task directory"):
            _skills_task(tmp_path, "traversal", "../outside", {})

    def test_a_symlink_out_of_the_pack_is_refused(self, tmp_path):
        """The traversal a pure string check would wave through.

        ``shutil.copytree`` follows symlinks when staging, so a link pointing at
        the operator's skills would copy their contents into the build context —
        exactly the contamination the policy rejects, wearing a relative path.
        """
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "SKILL.md").write_text("smuggled")
        task_dir = tmp_path / "linked"
        task_dir.mkdir()
        (task_dir / "skills").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="outside the task directory"):
            _skills_task(tmp_path, "linked", "skills", {})

    def test_a_missing_directory_is_refused(self, tmp_path):
        """A typo must not read as "this task ships no skills"."""
        with pytest.raises(ValueError, match="not a directory"):
            _skills_task(tmp_path, "missing", "skils/", {})

    def test_a_file_is_not_a_bundle(self, tmp_path):
        with pytest.raises(ValueError, match="not a directory"):
            _skills_task(tmp_path, "file", "skills", {"skills": "not a directory"})

    def test_the_layer_copies_the_bundle_for_a_harness_that_reads_skills(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _skills_task(tmp_path, "copied", "skills/", {"skills/README.md": "s"})
        env = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )
        dockerfile = (env.staging_dir / "_harness" / "harness.Dockerfile").read_text()
        assert "COPY skills/. /root/.claude/skills/" in dockerfile

    def test_the_bundle_survives_the_build_context_exclusions(self, tmp_path):
        """``.dockerignore`` excludes the staging tree, so the COPY needs an exception.

        Without it the layer's ``COPY`` fails the build outright — the bundle is
        not silently skipped, but the failure is a `docker build` error many
        steps from the cause.
        """
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _skills_task(tmp_path, "context", "skills/", {"skills/nested/SKILL.md": "s"})
        env = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )
        patterns = (env.staging_dir / ".dockerignore").read_text().split()
        assert "!skills" in patterns
        assert "!skills/**" in patterns

    def test_the_copy_line_follows_the_cli_install(self, tmp_path):
        """The CLI is installed before its skills land, so the layer cache
        invalidates on a bundle edit without reinstalling the CLI."""
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _skills_task(tmp_path, "ordered", "skills/", {"skills/README.md": "s"})
        env = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )
        lines = (env.staging_dir / "_harness" / "harness.Dockerfile").read_text().splitlines()
        assert lines[-1].startswith("COPY skills/.")
        assert lines[-2].startswith("RUN sh /opt/tolokaforge/install-harness.sh")

    def test_a_harness_that_reads_no_skills_drops_the_bundle_loudly(self, tmp_path):
        """Dropped, not refused — one task still runs under every harness."""
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _skills_task(tmp_path, "dropped", "skills/", {"skills/README.md": "s"})
        with pytest.warns(UserWarning, match="declares no skills_dir_target"):
            env = materialise_task_environment(
                meta, staging_root=tmp_path / "staging", agent_harness="codex"
            )
        dockerfile = (env.staging_dir / "_harness" / "harness.Dockerfile").read_text()
        assert "COPY skills" not in dockerfile

    def test_a_pack_without_skills_warns_about_nothing(self, tmp_path, recwarn):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _skills_task(tmp_path, "quiet", None, {})
        materialise_task_environment(meta, staging_root=tmp_path / "staging", agent_harness="codex")
        assert [w for w in recwarn if "skills_dir_target" in str(w.message)] == []

    def test_editing_the_bundle_restages_the_build_context(self, tmp_path):
        """The staged Dockerfile is only valid for the bundle it was built
        against, so a bundle edit must not reuse the previous staging dir."""
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _skills_task(tmp_path, "restaged", "skills/", {"skills/README.md": "before"})
        first = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )
        (meta.task_dir / "skills" / "README.md").write_text("after")
        second = materialise_task_environment(
            meta, staging_root=tmp_path / "staging", agent_harness="claude-code"
        )
        assert first.staging_dir != second.staging_dir

    def test_the_bundle_digest_reads_path_and_content(self, tmp_path):
        """A rename with identical bytes is a different bundle: the path is how
        the CLI discovers a skill."""
        from tolokaforge_adapter_terminal_bench.compose_synthesis import skills_bundle_digest

        meta = _skills_task(tmp_path, "digest", "skills/", {"skills/a/SKILL.md": "same"})
        before = skills_bundle_digest(meta.task_dir, meta.harness_skills_dir)

        (meta.task_dir / "skills" / "a" / "SKILL.md").rename(meta.task_dir / "skills" / "b.md")
        renamed = skills_bundle_digest(meta.task_dir, meta.harness_skills_dir)
        assert renamed != before

        (meta.task_dir / "skills" / "b.md").write_text("edited")
        assert skills_bundle_digest(meta.task_dir, meta.harness_skills_dir) != renamed

    def test_the_artifact_records_the_bundle_that_reached_the_image(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter
        from tolokaforge_adapter_terminal_bench.compose_synthesis import skills_bundle_digest

        fixture_dir = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "task_ids": ["echo-hello-skills"],
                "staging_root": str(tmp_path),
                "agent_harness": "claude-code",
                "agent_model": "m",
            }
        )
        metadata = adapter.to_task_description("echo-hello-skills").metadata
        assert metadata["harness_skills_bundle_sha"] == skills_bundle_digest(
            fixture_dir / "echo-hello-skills", "skills/"
        )

    def test_a_task_without_a_bundle_omits_the_key(self, tmp_path):
        """Absent has to stay distinguishable from "hashed to something"."""
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        fixture_dir = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "task_ids": ["echo-hello"],
                "staging_root": str(tmp_path),
                "agent_harness": "claude-code",
                "agent_model": "m",
            }
        )
        metadata = adapter.to_task_description("echo-hello").metadata
        assert "harness_skills_bundle_sha" not in metadata

    def test_a_dropped_bundle_is_not_recorded_as_installed(self, tmp_path):
        """The harness read no skills, so the artifact must not claim a bundle."""
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        fixture_dir = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "task_ids": ["echo-hello-skills"],
                "staging_root": str(tmp_path),
                "agent_harness": "codex",
                "agent_model": "m",
            }
        )
        metadata = adapter.to_task_description("echo-hello-skills").metadata
        assert "harness_skills_bundle_sha" not in metadata

    @staticmethod
    def _spec_with_target(target: str):
        from tolokaforge_coding_harnesses import HarnessSpec

        return HarnessSpec(
            install_source="cli",
            version="1.0.0",
            argv_prefix=("cli",),
            argv_suffix=(),
            skills_dir_target=target,
        )

    @pytest.mark.parametrize(
        "target",
        [
            "~/.claude/skills/",
            "$HOME/.claude/skills/",
            ".claude/skills/",
        ],
    )
    def test_a_target_nothing_will_expand_is_refused_at_registry_load(self, target):
        """Only two shapes are a skills path: absolute, or rooted at a
        ``${VAR}`` construct the resolver answers.

        A brace-less ``$HOME/...`` is the case worth naming: it is legal in a
        ``config_files`` key, because a shell writes that file. Nothing expands
        it here — the resolver leaves it alone and Docker would read it off the
        image's own ``ENV``, so the bundle would land somewhere the CLI never
        looks and the trial would still record skills.
        """
        with pytest.raises(ValueError, match="skills_dir_target"):
            self._spec_with_target(target)

    def test_a_construct_rooted_target_is_accepted(self):
        """The resolver answers it before delivery sees it."""
        assert self._spec_with_target("${HOME}/.claude/skills/").skills_dir_target == (
            "${HOME}/.claude/skills/"
        )


class TestTerminalBenchAdapterHarnessImageBuilds:
    """Harness mode declares two builds, base before layer."""

    def test_base_precedes_layer(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        examples_dir = Path(__file__).parent.parent.parent / "examples" / "terminal_bench"
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(examples_dir),
                "task_ids": ["fix-billing-holds"],
                "staging_root": str(tmp_path),
                "agent_harness": "claude-code",
                "agent_model": "m",
            }
        )
        reqs = adapter.docker_stack_requirements()
        assert [b.service for b in reqs.image_builds] == ["main-base", "main"]

    def test_default_harness_declares_one_build(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        examples_dir = Path(__file__).parent.parent.parent / "examples" / "terminal_bench"
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(examples_dir),
                "task_ids": ["fix-billing-holds"],
                "staging_root": str(tmp_path),
            }
        )
        reqs = adapter.docker_stack_requirements()
        assert [b.service for b in reqs.image_builds] == ["main"]

    def test_unknown_harness_rejected_at_construction(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        fixture_dir = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"
        with pytest.raises(ValueError, match="agent_harness"):
            TerminalBenchAdapter(
                {
                    "terminal_bench_dir": str(fixture_dir),
                    "staging_root": str(tmp_path),
                    "agent_harness": "terminus-3",
                    "agent_model": "m",
                }
            )


class TestProviderEnvWire:
    """Provider credentials reach the container through the per-trial ``.env``."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    def _adapter(self, fixture_dir, tmp_path, **extra):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        return TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "staging_root": str(tmp_path),
                "agent_harness": "claude-code",
                "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
                **extra,
            }
        )

    def test_keys_land_in_stack_inputs(self, fixture_dir, tmp_path):
        adapter = self._adapter(
            fixture_dir,
            tmp_path,
            agent_provider_env={
                "ANTHROPIC_API_KEY": "sk-test",
                "ANTHROPIC_BASE_URL": "https://proxy.example",
            },
        )
        patch_ = adapter.get_task("echo-hello").environment_manifest
        assert patch_.stack.inputs == {
            "TBENCH_PROVIDER_ANTHROPIC_API_KEY": "sk-test",
            "TBENCH_PROVIDER_ANTHROPIC_BASE_URL": "https://proxy.example",
        }

    def test_keys_survive_into_the_resolved_manifest(self, fixture_dir, tmp_path):
        adapter = self._adapter(
            fixture_dir, tmp_path, agent_provider_env={"OPENAI_API_KEY": "sk-openai"}
        )
        manifest = adapter.to_task_description("echo-hello").environment_manifest
        assert manifest is not None
        assert manifest.stack_inputs["TBENCH_PROVIDER_OPENAI_API_KEY"] == "sk-openai"

    def test_agent_service_binds_them_to_a_namespaced_compose_input(self, fixture_dir, tmp_path):
        """Names only in the compose file — the value comes from the ``.env``."""
        adapter = self._adapter(
            fixture_dir, tmp_path, agent_provider_env={"GOOGLE_API_KEY": "sk-google"}
        )
        env = adapter._environment("echo-hello")
        environment = _load_synthesised(env)["services"]["main"]["environment"]
        assert "GOOGLE_API_KEY=${TBENCH_PROVIDER_GOOGLE_API_KEY}" in environment
        assert "TEST_DIR=/tests" in environment
        assert "sk-google" not in env.compose_file.read_text()

    def test_the_compose_input_is_namespaced_away_from_the_provider_name(
        self, fixture_dir, tmp_path
    ):
        """Compose reads ``${VAR}`` from the invoking shell before the per-trial
        ``.env``. Interpolating the bare provider name would let whatever
        ``ANTHROPIC_API_KEY`` the operator's shell holds silently replace the
        declared value — a real key inside a benchmark container, and in its
        trial artifacts. Nothing sets the prefixed name by accident."""
        adapter = self._adapter(
            fixture_dir, tmp_path, agent_provider_env={"ANTHROPIC_API_KEY": "sk-declared"}
        )
        text = adapter._environment("echo-hello").compose_file.read_text()
        assert "${TBENCH_PROVIDER_ANTHROPIC_API_KEY}" in text
        assert "${ANTHROPIC_API_KEY}" not in text
        # A bare name would be a pass-through, which resolves the same unsafe way.
        environment = _load_synthesised(adapter._environment("echo-hello"))["services"]["main"][
            "environment"
        ]
        assert "ANTHROPIC_API_KEY" not in environment

    def test_task_bound_key_is_not_overwritten(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "bound",
            {
                "services": {
                    "main": {"image": "python:3.11-slim", "environment": ["OPENAI_API_KEY=task"]}
                }
            },
        )
        env = materialise_task_environment(
            meta,
            staging_root=tmp_path / "staging",
            agent_harness="codex",
            provider_env_keys=["OPENAI_API_KEY"],
        )
        environment = _load_synthesised(env)["services"]["main"]["environment"]
        assert "OPENAI_API_KEY=${TBENCH_PROVIDER_OPENAI_API_KEY}" in environment
        assert "OPENAI_API_KEY=task" not in environment

    def test_mapping_shaped_environment_keeps_its_shape(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _write_task(
            tmp_path,
            "mapping",
            {"services": {"main": {"image": "python:3.11-slim", "environment": {"FOO": "bar"}}}},
        )
        env = materialise_task_environment(
            meta,
            staging_root=tmp_path / "staging",
            agent_harness="codex",
            provider_env_keys=["ANTHROPIC_API_KEY"],
        )
        environment = _load_synthesised(env)["services"]["main"]["environment"]
        assert environment["ANTHROPIC_API_KEY"] == "${TBENCH_PROVIDER_ANTHROPIC_API_KEY}"
        assert environment["FOO"] == "bar"

    def test_non_forwardable_key_rejected(self, fixture_dir, tmp_path):
        with pytest.raises(ValueError, match="AWS_SECRET_ACCESS_KEY"):
            self._adapter(
                fixture_dir, tmp_path, agent_provider_env={"AWS_SECRET_ACCESS_KEY": "nope"}
            )

    def test_rejection_names_the_accepted_set(self, fixture_dir, tmp_path):
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            self._adapter(fixture_dir, tmp_path, agent_provider_env={"PATH": "/usr/bin"})

    def test_secret_reference_is_expanded(self, fixture_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-secret-manager")
        adapter = self._adapter(
            fixture_dir,
            tmp_path,
            agent_provider_env={"ANTHROPIC_API_KEY": "${secret:ANTHROPIC_API_KEY}"},
        )
        assert adapter.agent_provider_env["ANTHROPIC_API_KEY"] == "sk-from-secret-manager"

    def test_unresolvable_secret_reference_fails_loud(self, fixture_dir, tmp_path, monkeypatch):
        from tolokaforge.secrets import UnresolvedReferenceError

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(UnresolvedReferenceError):
            self._adapter(
                fixture_dir,
                tmp_path,
                agent_provider_env={"ANTHROPIC_API_KEY": "${secret:ANTHROPIC_API_KEY}"},
            )

    def test_multiline_value_refused(self, fixture_dir, tmp_path):
        """Each value is one ``.env`` line; a newline would truncate it silently."""
        with pytest.raises(ValueError, match="newline"):
            self._adapter(
                fixture_dir, tmp_path, agent_provider_env={"OPENAI_API_KEY": "sk-a\nsk-b"}
            )

    def test_engine_loop_forwards_nothing_by_default(self, fixture_dir, tmp_path):
        """No harness, no shipped envelope: the engine's own LLM layer holds
        the credentials, so nothing reaches the task container."""
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        assert adapter.agent_provider_env == {}
        assert adapter.get_task("echo-hello").environment_manifest.stack.inputs == {}

    def test_switching_keys_changes_the_staging_digest(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.compose_synthesis import (
            materialise_task_environment,
        )

        meta = _harness_task(tmp_path, "keydigest", with_build=True)
        a = materialise_task_environment(
            meta, staging_root=tmp_path / "s", agent_harness="codex", provider_env_keys=[]
        )
        b = materialise_task_environment(
            meta,
            staging_root=tmp_path / "s",
            agent_harness="codex",
            provider_env_keys=["OPENAI_API_KEY"],
        )
        assert a.staging_dir != b.staging_dir


class TestHarnessProviderEnvOverlay:
    """A harness ships the provider envelope its CLI needs; a run config
    adjusts it key by key instead of restating it."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    def _adapter(self, fixture_dir, tmp_path, **extra):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        return TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "staging_root": str(tmp_path),
                "agent_harness": "claude-code",
                "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
                **extra,
            }
        )

    def test_claude_code_ships_the_anthropic_pair(self):
        from tolokaforge_coding_harnesses import HARNESSES

        assert HARNESSES["claude-code"].provider_env == {
            "ANTHROPIC_API_KEY": "${secret:OPENROUTER_API_KEY}",
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        }

    def test_shipped_envelope_applies_when_the_run_config_declares_none(
        self, fixture_dir, tmp_path
    ):
        adapter = self._adapter(fixture_dir, tmp_path)
        assert adapter.agent_provider_env == {
            "ANTHROPIC_API_KEY": "sk-openrouter-test",
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        }

    def test_declared_key_wins_and_the_rest_of_the_envelope_survives(self, fixture_dir, tmp_path):
        """Union, not replacement: an operator pointing the CLI at a different
        endpoint should not have to restate the credential to keep it."""
        adapter = self._adapter(
            fixture_dir,
            tmp_path,
            agent_provider_env={"ANTHROPIC_BASE_URL": "https://different.example"},
        )
        assert adapter.agent_provider_env == {
            "ANTHROPIC_API_KEY": "sk-openrouter-test",
            "ANTHROPIC_BASE_URL": "https://different.example",
        }

    def test_shipped_keys_reach_the_agent_service(self, fixture_dir, tmp_path):
        adapter = self._adapter(fixture_dir, tmp_path)
        environment = _load_synthesised(adapter._environment("echo-hello"))["services"]["main"][
            "environment"
        ]
        assert "ANTHROPIC_API_KEY=${TBENCH_PROVIDER_ANTHROPIC_API_KEY}" in environment
        assert "ANTHROPIC_BASE_URL=${TBENCH_PROVIDER_ANTHROPIC_BASE_URL}" in environment

    def test_unresolvable_shipped_secret_names_the_harness(
        self, fixture_dir, tmp_path, monkeypatch
    ):
        """The failure has to point at the harness that shipped the reference —
        the run config never mentioned the key, so naming ``agent_provider_env``
        would send the operator to a block that does not exist."""
        from tolokaforge.secrets import UnresolvedReferenceError

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(UnresolvedReferenceError, match="claude-code"):
            self._adapter(fixture_dir, tmp_path)


class TestHarnessPresetsFileOverlay:
    """An operator ships harness entries without an adapter release."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    @staticmethod
    def _overlay(tmp_path: Path, body: str) -> Path:
        path = tmp_path / "harness_presets.yaml"
        path.write_text(body)
        return path

    def _adapter(self, fixture_dir, tmp_path, presets: Path | None, **extra):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        params = {
            "terminal_bench_dir": str(fixture_dir),
            "staging_root": str(tmp_path / "staging"),
            "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
            **extra,
        }
        if presets is not None:
            params["harness_presets_file"] = str(presets)
        return TerminalBenchAdapter(params)

    def test_overlay_entry_replaces_the_shipped_spec(self, fixture_dir, tmp_path):
        """Whole-entry replacement, and it has to reach every surface that
        reads the spec — the recorded version and the layered image tag."""
        presets = self._overlay(
            tmp_path,
            "harnesses:\n"
            "  claude-code:\n"
            "    install_source: '@anthropic-ai/claude-code'\n"
            "    version: '0.0.0-overlay'\n"
            "    argv_prefix: [claude]\n"
            "    argv_suffix: ['--print']\n",
        )
        adapter = self._adapter(fixture_dir, tmp_path, presets, agent_harness="claude-code")
        assert adapter.harnesses["claude-code"].version == "0.0.0-overlay"
        # The shipped entry's other fields are gone, not merged underneath.
        assert adapter.harnesses["claude-code"].env_model_vars == ()
        td = adapter.to_task_description("echo-hello")
        assert td.metadata["agent_harness_version"] == "0.0.0-overlay"
        assert " --model " in td.metadata["agent_harness_command"]
        compose = _load_synthesised(adapter._environment("echo-hello"))
        assert compose["services"]["main"]["image"].endswith("claude-code-0.0.0-overlay")

    def test_shipped_entries_the_overlay_leaves_alone_are_untouched(self, fixture_dir, tmp_path):
        from tolokaforge_coding_harnesses import HARNESSES

        presets = self._overlay(
            tmp_path,
            "harnesses:\n"
            "  codex:\n"
            "    install_source: '@openai/codex'\n"
            "    version: '0.0.0-overlay'\n"
            "    argv_prefix: [codex, exec]\n"
            "    argv_suffix: []\n",
        )
        adapter = self._adapter(fixture_dir, tmp_path, presets)
        assert adapter.harnesses["claude-code"] == HARNESSES["claude-code"]

    def test_overlay_can_add_a_harness(self, fixture_dir, tmp_path):
        presets = self._overlay(
            tmp_path,
            "harnesses:\n"
            "  in-house-cli:\n"
            "    install_source: '@acme/in-house-cli'\n"
            "    version: '1.2.3'\n"
            "    argv_prefix: [acme]\n"
            "    argv_suffix: ['--go']\n",
        )
        adapter = self._adapter(
            fixture_dir, tmp_path, presets, agent_harness="in-house-cli", agent_model="m"
        )
        command = adapter.to_task_description("echo-hello").metadata["agent_harness_command"]
        assert command.startswith("acme --model m --go ")

    def test_missing_overlay_file_is_refused_at_construction(self, fixture_dir, tmp_path):
        missing = tmp_path / "absent.yaml"
        with pytest.raises(ValueError, match="does not exist") as excinfo:
            self._adapter(fixture_dir, tmp_path, missing)
        assert str(missing) in str(excinfo.value)

    def test_invalid_overlay_entry_names_the_harness(self, fixture_dir, tmp_path):
        presets = self._overlay(
            tmp_path,
            "harnesses:\n  codex:\n    install_source: '@openai/codex'\n    argv_prefix: [codex]\n",
        )
        with pytest.raises(ValueError, match="codex"):
            self._adapter(fixture_dir, tmp_path, presets)

    def test_no_overlay_leaves_the_shipped_registry(self, fixture_dir, tmp_path):
        from tolokaforge_coding_harnesses import HARNESSES

        assert self._adapter(fixture_dir, tmp_path, None).harnesses == HARNESSES

    def test_shipped_gemini_litellm_overlay_resolves(self, fixture_dir, tmp_path, monkeypatch):
        """The shipped overlay example at
        ``examples/terminal_bench/gemini_litellm_overlay.yaml`` is the
        sanctioned path to route gemini-cli via a LiteLLM gateway. It must
        resolve into a valid ``HarnessSpec`` — a stale field name or a
        missed allow-list widen surfaces here rather than as a load-time
        crash on the operator's machine."""
        monkeypatch.setenv("LITELLM_API_KEY", "sk-litellm-test")
        monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.example.test")
        examples_dir = Path(__file__).parent.parent.parent / "examples" / "terminal_bench"
        overlay = examples_dir / "gemini_litellm_overlay.yaml"
        adapter = self._adapter(
            fixture_dir,
            tmp_path,
            overlay,
            agent_harness="gemini-cli",
            agent_model="gemini-3.1-pro-preview",
        )
        spec = adapter.harnesses["gemini-cli"]
        assert spec.container_env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
        assert spec.container_env["GOOGLE_GEMINI_BASE_URL"] == ("${secret:LITELLM_BASE_URL}/gemini")
        assert spec.provider_env == {"GEMINI_API_KEY": "${secret:LITELLM_API_KEY}"}


class TestHarnessRegistryPluginDiscovery:
    """A pip-installed bundle reaches a trial the adapter runs.

    Registry composition itself is locked in
    ``tolokaforge_coding_harnesses/tests/unit/test_registry_composition.py``;
    what is left here is the adapter seam — the ``disable_harness_plugins`` param
    and a discovered harness assembling into a task description.
    """

    _TASKS_DIR = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    @pytest.fixture(autouse=True)
    def _isolated_discovery(self, monkeypatch):
        """Drop the per-group entry-point cache around every case.

        Harness-registry discovery caches its scan, so an injected plugin set
        would otherwise leak into the next case.
        """
        from tolokaforge_coding_harnesses._registry import _clear_discovery_cache

        _clear_discovery_cache()
        yield
        _clear_discovery_cache()

    @pytest.fixture
    def plugin(self, tmp_path, monkeypatch):
        """Build an importable plugin package shipping a registry YAML."""

        def _build(
            package: str,
            distribution: str | None,
            harnesses: str,
            version: str = "1.0.0",
        ) -> FakeEntryPoint:
            return build_plugin(tmp_path, monkeypatch, package, distribution, harnesses, version)

        return _build

    _install = staticmethod(install_plugins)
    _bundle = staticmethod(bundle_yaml)

    def test_disable_harness_plugins_bypasses_discovery(self, monkeypatch, tmp_path):
        """An audit run pins the registry to what the adapter ships, whatever
        else happens to be installed in the environment — so discovery must not
        run at all, not merely have its result discarded."""
        import importlib.metadata

        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        from tolokaforge_coding_harnesses import (
            HARNESSES,
            resolve_effective_registry,
        )

        def _refuse(**kwargs):
            raise AssertionError(f"entry-point discovery ran for group {kwargs.get('group')!r}")

        monkeypatch.setattr(importlib.metadata, "entry_points", _refuse)
        resolved = resolve_effective_registry(discover_plugins=False)
        assert resolved.harnesses == HARNESSES
        assert resolved.plugin_bundles == ()
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(self._TASKS_DIR),
                "staging_root": str(tmp_path / "staging"),
                "disable_harness_plugins": True,
            }
        )
        assert adapter.harnesses == HARNESSES

    def test_adapter_runs_a_harness_a_plugin_contributed(self, monkeypatch, plugin, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        self._install(
            monkeypatch,
            plugin("acme_harnesses", "acme-tbench-harnesses", self._bundle("acme-cli", "4.5.6")),
        )
        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(self._TASKS_DIR),
                "staging_root": str(tmp_path / "staging"),
                "agent_harness": "acme-cli",
                "agent_model": "m",
            }
        )
        command = adapter.to_task_description("echo-hello").metadata["agent_harness_command"]
        assert command.startswith("acme-cli --model m --go ")


class TestHarnessTaskDescriptionMetadata:
    """What the engine core reads to decide a trial runs a harness CLI."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    def test_harness_command_carries_the_instruction(self, fixture_dir, tmp_path):
        """claude-code publishes an instruction-on-stdin command; the task's
        instruction is the single ``printf`` argument, and the resolved model
        (with ``openrouter/`` stripped) lands in the env quartet."""
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "staging_root": str(tmp_path),
                "agent_harness": "claude-code",
                "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
            }
        )
        td = adapter.to_task_description("echo-hello")
        assert td.metadata["agent_harness"] == "claude-code"
        command = td.metadata["agent_harness_command"]
        instruction = adapter.get_task("echo-hello").initial_user_message
        # Env quartet carries the resolved model, no ``--model`` on the CLI.
        assert "export ANTHROPIC_MODEL=anthropic/claude-sonnet-4-6 " in command
        assert " --model " not in command
        # Instruction reaches the CLI on stdin.
        assert f"printf %s {instruction!r}".replace("'", "'") in command or (
            f"printf %s '{instruction}'" in command
        )
        assert command.rstrip().endswith(
            "claude --verbose --output-format=stream-json "
            "--permission-mode=bypassPermissions --print"
        )
        assert td.metadata["agent_harness_version"] == "2.1.233"

    def test_default_harness_publishes_no_command(self, fixture_dir, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        td = adapter.to_task_description("echo-hello")
        assert td.metadata["agent_harness"] == "engine-loop"
        assert "agent_harness_command" not in td.metadata

    def test_harness_mode_gives_bash_the_whole_agent_budget(self, fixture_dir, tmp_path):
        """One exec runs the entire trial, so the per-call timeout is the trial's."""
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter
        from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "staging_root": str(tmp_path),
                "agent_harness": "codex",
                "agent_model": "m",
            }
        )
        expected = discover_tasks(fixture_dir)["echo-hello"].agent_timeout_sec
        assert adapter.to_task_description("echo-hello").agent_tools[0].timeout_s == expected

    def test_default_harness_keeps_the_per_call_timeout(self, fixture_dir, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        assert adapter.to_task_description("echo-hello").agent_tools[0].timeout_s == 120.0
