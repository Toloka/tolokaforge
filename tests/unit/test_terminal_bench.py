"""Unit tests for terminal-bench adapter and Docker Compose exec wrapper."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.docker.policy import Capability
from tolokaforge.docker.stacks.core import core_stack
from tolokaforge.runner.models import (
    AdapterType,
    InvocationStyle,
    ToolSchema,
    ToolSource,
)
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    ToolConfigurationError,
    ToolFactory,
)

pytestmark = pytest.mark.unit


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
        assert "tau" in names
        assert "tlk_mcp_core" in names


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
    """Minimal ToolSchema for the bash tool."""
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
            extra={
                "compose_file": "docker-compose.yaml",
                "task_dir": "/tasks/test-task",
                "service": "main",
                "env_vars": {"T_BENCH_TEST_DIR": "/tests"},
            },
        ),
    )


@pytest.fixture
def wrapper(wrapper_schema):
    return DockerComposeExecToolWrapper(
        tool_schema=wrapper_schema,
        compose_file="docker-compose.yaml",
        task_dir="/tasks/test-task",
        service="main",
        env_vars={"T_BENCH_TEST_DIR": "/tests"},
    )


class TestDockerComposeExecWrapperInit:
    def test_initial_state(self, wrapper):
        assert wrapper.compose_file == "docker-compose.yaml"
        assert wrapper.task_dir == "/tasks/test-task"
        assert wrapper.service == "main"
        assert wrapper.project_name is None
        assert wrapper._started is False

    def test_default_service(self, wrapper_schema):
        w = DockerComposeExecToolWrapper(
            tool_schema=wrapper_schema,
            compose_file="dc.yaml",
            task_dir="/tmp",
        )
        assert w.service == "main"
        assert w.env_vars == {}


class TestDockerComposeExecWrapperComposeCmdBuilder:
    def test_compose_cmd_builds_correctly(self, wrapper):
        wrapper.project_name = "test_project"
        cmd = wrapper._compose_cmd("up", "-d", "--wait")
        assert cmd == [
            "docker",
            "compose",
            "-f",
            "docker-compose.yaml",
            "-p",
            "test_project",
            "up",
            "-d",
            "--wait",
        ]

    def test_compose_cmd_exec(self, wrapper):
        wrapper.project_name = "proj"
        cmd = wrapper._compose_cmd("exec", "-T", "main", "bash", "-c", "echo hi")
        assert cmd[0:6] == ["docker", "compose", "-f", "docker-compose.yaml", "-p", "proj"]
        assert cmd[6:] == ["exec", "-T", "main", "bash", "-c", "echo hi"]


class TestDockerComposeExecWrapperStart:
    @patch("subprocess.run")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=True)
    @patch("os.path.isfile", return_value=True)
    def test_start_sets_project_name(
        self, mock_isfile, mock_isdir, mock_makedirs, mock_run, wrapper
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        wrapper.start("tbench_task_0")
        assert wrapper.project_name == "tbench_task_0"
        assert wrapper._started is True

    @patch("subprocess.run")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=False)
    @patch("os.path.isfile", return_value=False)
    def test_start_overrides_container_name(
        self, mock_isfile, mock_isdir, mock_makedirs, mock_run, wrapper
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        wrapper.start("tbench_mytask_2")
        assert (
            wrapper.env_vars["T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME"] == "tbench_mytask_2_main"
        )

    @patch("subprocess.run")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=False)
    @patch("os.path.isfile", return_value=False)
    def test_start_sets_unique_log_paths(
        self, mock_isfile, mock_isdir, mock_makedirs, mock_run, wrapper
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        wrapper.start("tbench_task_1")
        assert wrapper.env_vars["T_BENCH_TASK_LOGS_PATH"] == "/workspace/logs/tbench_task_1"
        assert (
            wrapper.env_vars["T_BENCH_TASK_AGENT_LOGS_PATH"]
            == "/workspace/agent_logs/tbench_task_1"
        )

    @patch("subprocess.run")
    @patch("os.makedirs")
    def test_start_raises_on_compose_failure(self, mock_makedirs, mock_run, wrapper):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: service failed"
        )
        with pytest.raises(RuntimeError, match="docker compose up failed"):
            wrapper.start("tbench_fail_0")

    @patch("subprocess.run")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=True)
    @patch("os.path.isfile", return_value=True)
    def test_start_copies_tests(self, mock_isfile, mock_isdir, mock_makedirs, mock_run, wrapper):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        wrapper.start("tbench_copy_0")
        # Should have: compose up, cp tests, cp run-tests.sh, mkdir logs
        assert mock_run.call_count >= 4


class TestDockerComposeExecWrapperStop:
    @patch("subprocess.run")
    def test_stop_when_started(self, mock_run, wrapper):
        wrapper._started = True
        wrapper.project_name = "tbench_test_0"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        wrapper.stop()
        assert wrapper._started is False
        # Verify docker compose down was called
        call_args = mock_run.call_args[0][0]
        assert "down" in call_args

    def test_stop_noop_when_not_started(self, wrapper):
        wrapper._started = False
        wrapper.project_name = None
        wrapper.stop()  # Should not raise

    @patch("subprocess.run")
    def test_cleanup_calls_stop(self, mock_run, wrapper):
        wrapper._started = True
        wrapper.project_name = "tbench_cleanup_0"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        wrapper.cleanup()
        assert wrapper._started is False


class TestDockerComposeExecWrapperExec:
    def test_exec_sync_success(self, wrapper):
        wrapper.project_name = "proj"
        with patch.object(wrapper, "_run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="hello world\n", stderr=""
            )
            result = wrapper._exec_sync("echo hello world", 30.0)
            assert result == "hello world\n"

    def test_exec_sync_nonzero_exit(self, wrapper):
        wrapper.project_name = "proj"
        with patch.object(wrapper, "_run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="partial", stderr="error msg"
            )
            result = wrapper._exec_sync("bad cmd", 30.0)
            assert "partial" in result
            assert "[exit code: 1]" in result
            assert "error msg" in result

    @pytest.mark.asyncio
    async def test_execute_async(self, wrapper):
        wrapper.project_name = "proj"
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
                extra={
                    "compose_file": "docker-compose.yaml",
                    "task_dir": "/tasks/test",
                    "service": "main",
                    "env_vars": {"KEY": "val"},
                },
            ),
        )
        wrapper = factory._create_wrapper(schema)
        assert isinstance(wrapper, DockerComposeExecToolWrapper)
        assert wrapper.compose_file == "docker-compose.yaml"
        assert wrapper.task_dir == "/tasks/test"
        assert wrapper.service == "main"
        assert wrapper.env_vars == {"KEY": "val"}

    def test_missing_compose_file_raises(self, factory):
        schema = ToolSchema(
            name="bash",
            description="Run command",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                toolset="terminal_bench",
                module_path="",
                class_name="bash",
                invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                extra={"task_dir": "/tasks/test"},
            ),
        )
        with pytest.raises(ToolConfigurationError, match="compose_file"):
            factory._create_wrapper(schema)

    def test_missing_task_dir_raises(self, factory):
        schema = ToolSchema(
            name="bash",
            description="Run command",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                toolset="terminal_bench",
                module_path="",
                class_name="bash",
                invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                extra={"compose_file": "dc.yaml"},
            ),
        )
        with pytest.raises(ToolConfigurationError, match="task_dir"):
            factory._create_wrapper(schema)

    def test_default_service_is_main(self, factory):
        schema = ToolSchema(
            name="bash",
            description="Run command",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                toolset="terminal_bench",
                module_path="",
                class_name="bash",
                invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                extra={"compose_file": "dc.yaml", "task_dir": "/t"},
            ),
        )
        wrapper = factory._create_wrapper(schema)
        assert wrapper.service == "main"


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
# Compose env var resolution
# =============================================================================


class TestComposeEnvVars:
    def test_default_image_name(self):
        from tolokaforge_adapter_terminal_bench.compose_env import resolve_tbench_env_vars
        from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask

        meta = TerminalBenchTask(
            task_id="my-task",
            task_dir=Path("/tasks/my-task"),
            compose_file=Path("/tasks/my-task/docker-compose.yaml"),
            instruction="test",
        )
        env = resolve_tbench_env_vars(meta)
        assert env["T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME"] == "tbench_my-task"

    def test_registry_image_name(self):
        from tolokaforge_adapter_terminal_bench.compose_env import resolve_tbench_env_vars
        from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask

        meta = TerminalBenchTask(
            task_id="my-task",
            task_dir=Path("/tasks/my-task"),
            compose_file=Path("/tasks/my-task/docker-compose.yaml"),
            instruction="test",
        )
        env = resolve_tbench_env_vars(meta, image_registry="registry.io/tbench")
        assert env["T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME"] == "registry.io/tbench/my-task:latest"

    def test_log_paths_under_workspace(self):
        from tolokaforge_adapter_terminal_bench.compose_env import resolve_tbench_env_vars
        from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask

        meta = TerminalBenchTask(
            task_id="my-task",
            task_dir=Path("/tasks/my-task"),
            compose_file=Path("/tasks/my-task/docker-compose.yaml"),
            instruction="test",
        )
        env = resolve_tbench_env_vars(meta)
        assert env["T_BENCH_TASK_LOGS_PATH"].startswith("/workspace/")
        assert env["T_BENCH_TASK_AGENT_LOGS_PATH"].startswith("/workspace/")

    def test_resource_limits(self):
        from tolokaforge_adapter_terminal_bench.compose_env import resolve_tbench_env_vars
        from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask

        meta = TerminalBenchTask(
            task_id="t",
            task_dir=Path("/t"),
            compose_file=Path("/t/dc.yaml"),
            instruction="",
            cpus=4,
            memory_mb=8192,
        )
        env = resolve_tbench_env_vars(meta)
        assert env["CPUS"] == "4"
        assert env["MEMORY"] == "8192M"
