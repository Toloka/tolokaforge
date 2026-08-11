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
    ToolLifecycleContext,
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
        wrapper.start(ToolLifecycleContext(trial_id="task_0"))
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
        wrapper.start(ToolLifecycleContext(trial_id="mytask_2"))
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
        wrapper.start(ToolLifecycleContext(trial_id="task_1"))
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
            wrapper.start(ToolLifecycleContext(trial_id="fail_0"))

    @patch("subprocess.run")
    @patch("os.makedirs")
    @patch("os.path.isdir", return_value=True)
    @patch("os.path.isfile", return_value=True)
    def test_start_copies_tests(self, mock_isfile, mock_isdir, mock_makedirs, mock_run, wrapper):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        wrapper.start(ToolLifecycleContext(trial_id="copy_0"))
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


class TestCoreStackTerminalBenchMounts:
    def test_task_pack_mounts_bind_same_absolute_path(self, tmp_path):
        pack = tmp_path / "pack"
        pack.mkdir()
        stack = core_stack(task_pack_mounts=[pack])
        runner = stack.services["runner"]
        binds = [m for m in runner.mounts if m.source == str(pack.resolve())]
        assert binds, "task pack should be bind-mounted at its absolute host path"
        assert binds[0].target == str(pack.resolve())

    def test_extra_runner_binds_are_applied(self, tmp_path):
        host = tmp_path / "logs"
        host.mkdir()
        stack = core_stack(extra_runner_binds=[(host, "/tmp/tb-logs")])
        runner = stack.services["runner"]
        targets = [m.target for m in runner.mounts]
        assert "/tmp/tb-logs" in targets

    def test_mount_docker_socket_adds_bind_and_relaxes_caps(self):
        stack = core_stack(mount_docker_socket=True)
        runner = stack.services["runner"]
        sock_mounts = [m for m in runner.mounts if m.source == "/var/run/docker.sock"]
        assert len(sock_mounts) == 1
        assert sock_mounts[0].target == "/var/run/docker.sock"
        # Relaxed profile means no explicit cap drop — docker CLI needs extra caps.
        assert runner.resources is None or runner.resources.cap_drop == []


class TestTerminalBenchAdapterDockerStackRequirements:
    """Adapter declares its host-socket needs through the generic hook."""

    def test_requirements_request_socket_passthrough(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        pack = tmp_path / "pack"
        pack.mkdir()
        adapter = TerminalBenchAdapter({"task_packs": [str(pack)]})

        reqs = adapter.docker_stack_requirements()

        assert reqs.mount_docker_socket is True
        assert pack.resolve() in reqs.task_pack_mounts
        assert (
            Path(TerminalBenchAdapter.LOGS_HOST_ROOT),
            TerminalBenchAdapter.LOGS_HOST_ROOT,
        ) in reqs.extra_runner_binds

    def test_missing_pack_is_excluded_from_mounts(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        ghost = tmp_path / "missing"
        adapter = TerminalBenchAdapter({"task_packs": [str(ghost)]})

        reqs = adapter.docker_stack_requirements()
        assert ghost.resolve() not in reqs.task_pack_mounts

    def test_first_pack_drives_discovery_and_runner_paths(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        pack = tmp_path / "pack"
        pack.mkdir()
        adapter = TerminalBenchAdapter({"task_packs": [str(pack)]})

        # Adapter self-resolves its discovery + runner paths from task_packs;
        # orchestrator no longer needs to set them.
        assert adapter.terminal_bench_dir == pack.resolve()
        assert adapter.runner_task_dir == str(pack.resolve())
        assert adapter.logs_host_root == TerminalBenchAdapter.LOGS_HOST_ROOT

    def test_to_core_stack_kwargs_renders_only_set_fields(self, tmp_path):
        from tolokaforge.adapters.base import DockerStackRequirements

        empty = DockerStackRequirements().to_core_stack_kwargs()
        assert empty == {}

        pack = tmp_path / "pack"
        pack.mkdir()
        kwargs = DockerStackRequirements(
            task_pack_mounts=[pack],
            mount_docker_socket=True,
        ).to_core_stack_kwargs()
        assert kwargs == {
            "task_pack_mounts": [pack],
            "mount_docker_socket": True,
        }


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
