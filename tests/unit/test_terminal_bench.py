"""Unit tests for terminal-bench adapter and Docker Compose exec wrapper."""

import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tests.utils.harness_plugins import (
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
# Harness mode: install script, command construction, image layering
# =============================================================================


class TestInstallHarnessScript:
    """The install script is the only place a harness's install steps live."""

    _DEFAULT_DOWNLOAD = 'echo "installer $*" >> {record}\n'

    @staticmethod
    def _run_script(tmp_path: Path, *args: str, download: Path | None = None):
        """Run the script with fake package managers on ``PATH``.

        Each fake appends its argv to one record file, so a dispatch assertion
        reads as the request the method made of its tool — behavioural rather
        than source-scraping. ``curl`` additionally copies *download* to the
        path behind ``-o``, standing in for what the URL would have served.
        """
        from tolokaforge_adapter_terminal_bench.harness import INSTALL_SCRIPT

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        record = tmp_path / "tool-argv.txt"
        if download is None:
            download = tmp_path / "downloaded"
            download.write_text(TestInstallHarnessScript._DEFAULT_DOWNLOAD.format(record=record))

        def _fake(name: str, body: str = "") -> None:
            path = bin_dir / name
            path.write_text(f'#!/bin/sh\necho "$@" >> {record}\n{body}')
            path.chmod(0o755)

        _fake("npm")
        _fake("pip")
        _fake(
            "curl",
            'out=""\nprev=""\nfor arg in "$@"; do\n'
            '  if [ "$prev" = "-o" ]; then out="$arg"; fi\n'
            '  prev="$arg"\ndone\n'
            f'if [ -n "$out" ]; then cp {download} "$out"; fi\n',
        )
        # ``install-harness.sh`` first checks for a Node ≥ 18. On the CI runner
        # the check hits real node; on a developer laptop where PATH is
        # deliberately restricted (below), it wouldn't. A fake ``node``
        # reporting a recent major keeps the script off its apt/apk install
        # branch so the assertion is on what ``npm`` receives.
        fake_node = bin_dir / "node"
        fake_node.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            "  -e*process.exit*) exit 0 ;;\n"
            '  *) echo "v20.0.0" ;;\n'
            "esac\n"
        )
        fake_node.chmod(0o755)
        proc = subprocess.run(
            ["sh", str(INSTALL_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "TOLOKAFORGE_HARNESS_STATE_DIR": str(tmp_path / "state"),
                "TOLOKAFORGE_HARNESS_BIN_DIR": str(tmp_path / "target-bin"),
            },
        )
        recorded = record.read_text().splitlines() if record.exists() else []
        return proc, recorded

    @staticmethod
    def _recorded_version(tmp_path: Path) -> str:
        return (tmp_path / "state" / "installed-version.txt").read_text().strip()

    def test_every_shipped_harness_installs_via_its_declared_method(self, tmp_path):
        """Each shipped entry dispatches to whichever install_method it
        declares. The expected fake-recorder output shape depends on the
        method, so this test enumerates the per-method contracts once —
        each of the four dispatch tests below pins one branch in detail."""
        from tolokaforge_adapter_terminal_bench.harness import HARNESSES

        for name, spec in HARNESSES.items():
            proc, recorded = self._run_script(
                tmp_path / name, spec.install_method, spec.install_source, spec.version
            )
            assert proc.returncode == 0, f"{name}: {proc.stderr}"
            if spec.install_method == "npm":
                assert recorded == [f"install -g {spec.install_source}@{spec.version}"], name
            elif spec.install_method == "pip":
                assert recorded == [
                    f"install --no-cache-dir {spec.install_source}=={spec.version}"
                ], name
            elif spec.install_method == "curl-bash":
                assert recorded == [
                    f"-fsSL {spec.install_source} -o /tmp/harness-installer.sh",
                    f"installer {spec.version}",
                ], name
            elif spec.install_method == "binary":
                assert recorded == [f"-fsSL {spec.install_source} -o /tmp/harness-download"], name
            else:
                raise AssertionError(f"unknown install_method for {name}: {spec.install_method!r}")
            assert self._recorded_version(tmp_path / name) == spec.version, name

    def test_pip_install_dispatch_calls_pip(self, tmp_path):
        proc, recorded = self._run_script(tmp_path, "pip", "some-harness-cli", "1.2.3")
        assert proc.returncode == 0, proc.stderr
        assert recorded == ["install --no-cache-dir some-harness-cli==1.2.3"]
        assert self._recorded_version(tmp_path) == "1.2.3"

    def test_curl_bash_dispatch_runs_the_downloaded_installer(self, tmp_path):
        """The installer is downloaded and then run: POSIX sh has no
        ``pipefail``, so piping ``curl`` into ``sh`` would leave a failed
        download green with nothing installed."""
        proc, recorded = self._run_script(
            tmp_path, "curl-bash", "https://harness.invalid/install.sh", "1.2.3"
        )
        assert proc.returncode == 0, proc.stderr
        assert recorded == [
            "-fsSL https://harness.invalid/install.sh -o /tmp/harness-installer.sh",
            "installer 1.2.3",
        ]
        assert self._recorded_version(tmp_path) == "1.2.3"

    def test_binary_dispatch_installs_the_downloaded_executable(self, tmp_path):
        proc, recorded = self._run_script(
            tmp_path, "binary", "https://harness.invalid/dl/grok", "1.2.3"
        )
        assert proc.returncode == 0, proc.stderr
        assert recorded == ["-fsSL https://harness.invalid/dl/grok -o /tmp/harness-download"]
        installed = tmp_path / "target-bin" / "grok"
        assert os.access(installed, os.X_OK)
        assert self._recorded_version(tmp_path) == "1.2.3"

    def test_binary_dispatch_unpacks_a_tarball(self, tmp_path):
        """A `.tar.gz` source carries its executables at the archive root."""
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "grok").write_text("#!/bin/sh\necho grok\n")
        (payload / "grok").chmod(0o755)
        archive = tmp_path / "harness.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload / "grok", arcname="grok")

        proc, _ = self._run_script(
            tmp_path, "binary", "https://harness.invalid/dl/grok.tar.gz", "1.2.3", download=archive
        )
        assert proc.returncode == 0, proc.stderr
        assert os.access(tmp_path / "target-bin" / "grok", os.X_OK)

    def test_floating_version_aborts_for_a_downloaded_install(self, tmp_path):
        """Neither URL method can report what an installer chose, and an
        unrecorded agent version is not a benchmark result."""
        proc, recorded = self._run_script(
            tmp_path, "curl-bash", "https://harness.invalid/install.sh", "latest"
        )
        assert proc.returncode != 0
        assert "pin a version" in proc.stderr
        assert recorded == []

    def test_unknown_method_aborts(self, tmp_path):
        proc, recorded = self._run_script(tmp_path, "brew", "some-harness-cli", "1.2.3")
        assert proc.returncode != 0
        assert "unknown install method" in proc.stderr
        assert recorded == []

    def test_missing_version_aborts(self, tmp_path):
        """An unpinned install would make the agent version unrecorded."""
        proc, recorded = self._run_script(tmp_path, "npm", "@anthropic-ai/claude-code")
        assert proc.returncode != 0
        assert "pinned" in proc.stderr
        assert recorded == []

    def test_missing_source_aborts(self, tmp_path):
        proc, recorded = self._run_script(tmp_path, "npm")
        assert proc.returncode != 0
        assert "no install source" in proc.stderr
        assert recorded == []

    def test_missing_method_aborts(self, tmp_path):
        proc, recorded = self._run_script(tmp_path)
        assert proc.returncode != 0
        assert "no install method" in proc.stderr
        assert recorded == []


class TestHarnessRegistryMeta:
    """``data/registry_meta.yaml`` — registry-wide catalogs (OpenRouter vendor
    namespaces + provider env-var allow-list) that used to be Python
    constants. Fail-loud on malformed data; adding a namespace or a key is a
    YAML edit."""

    def test_shipped_file_populates_the_module_globals(self):
        from tolokaforge_adapter_terminal_bench.harness import (
            _VENDOR_NAMESPACE_PREFIXES,
            PROVIDER_ENV_KEYS,
            SHIPPED_REGISTRY_META_FILE,
        )

        assert SHIPPED_REGISTRY_META_FILE.is_file()
        assert _VENDOR_NAMESPACE_PREFIXES  # non-empty
        assert PROVIDER_ENV_KEYS  # non-empty
        # Existing shipped harnesses' provider_env keys all belong to the
        # allow-list.
        from tolokaforge_adapter_terminal_bench.harness import HARNESSES

        for spec in HARNESSES.values():
            for key in spec.provider_env:
                assert key in PROVIDER_ENV_KEYS, (
                    f"harness ships provider_env key {key!r} outside PROVIDER_ENV_KEYS — "
                    "either add the key to registry_meta.yaml or the harness is broken."
                )

    def test_missing_file_names_the_path(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import _load_registry_meta

        with pytest.raises(FileNotFoundError, match="registry_meta.yaml"):
            _load_registry_meta(tmp_path / "does_not_exist.yaml")

    def test_non_mapping_yaml_is_refused(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text("- one\n- two\n")  # a list at the top level
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            _load_registry_meta(target)

    def test_unknown_top_level_key_is_refused(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text(
            "openrouter_vendor_namespaces: [foo/]\n"
            "provider_env_keys: [A_KEY]\n"
            "extra_key: hi\n"
        )
        with pytest.raises(ValueError):
            _load_registry_meta(target)

    def test_empty_namespaces_list_is_refused(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text("openrouter_vendor_namespaces: []\nprovider_env_keys: [A_KEY]\n")
        with pytest.raises(ValueError, match="openrouter_vendor_namespaces must not be empty"):
            _load_registry_meta(target)

    def test_empty_env_keys_list_is_refused(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text("openrouter_vendor_namespaces: [foo/]\nprovider_env_keys: []\n")
        with pytest.raises(ValueError, match="provider_env_keys must not be empty"):
            _load_registry_meta(target)

    def test_duplicate_namespace_entry_is_refused(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text(
            "openrouter_vendor_namespaces: [foo/, foo/]\nprovider_env_keys: [A_KEY]\n"
        )
        with pytest.raises(ValueError, match="duplicates"):
            _load_registry_meta(target)

    def test_blank_entry_is_refused(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text('openrouter_vendor_namespaces: ["foo/"]\nprovider_env_keys: [" "]\n')
        with pytest.raises(ValueError, match="non-blank"):
            _load_registry_meta(target)


class TestHarnessSpecRegistry:
    """The shipped registry is packaged YAML data, loaded at import."""

    def test_shipped_file_declares_the_supported_harnesses(self):
        from tolokaforge_adapter_terminal_bench.harness import (
            HARNESSES,
            SHIPPED_REGISTRY_FILE,
            load_harness_registry,
        )

        assert SHIPPED_REGISTRY_FILE.is_file()
        assert list(HARNESSES) == [
            "claude-code",
            "codex",
            "gemini-cli",
            "kimi-code",
            "opencode",
            "grok-build",
        ]
        assert load_harness_registry(SHIPPED_REGISTRY_FILE) == HARNESSES

    def test_shipped_entries_install_methods(self):
        """Five entries install via npm; Grok Build installs via curl-bash
        (the first non-npm entry, exercising install-harness.sh dispatch)."""
        from tolokaforge_adapter_terminal_bench.harness import HARNESSES

        assert {
            name: (spec.install_method, spec.install_source) for name, spec in HARNESSES.items()
        } == {
            "claude-code": ("npm", "@anthropic-ai/claude-code"),
            "codex": ("npm", "@openai/codex"),
            "gemini-cli": ("npm", "@google/gemini-cli"),
            "kimi-code": ("npm", "@moonshot-ai/kimi-code"),
            "opencode": ("npm", "opencode-ai"),
            "grok-build": ("curl-bash", "https://x.ai/cli/install.sh"),
        }

    @pytest.mark.parametrize(
        ("document", "expected"),
        [
            pytest.param(
                "harnesses:\n"
                "  claude-code:\n"
                "    install_source: p\n"
                "    version: '1'\n"
                "    argv_prefix: [claude]\n"
                "    argv_suffix: []\n"
                "    typo_field: nope\n",
                "typo_field",
                id="unknown-field",
            ),
            pytest.param(
                "harnesses:\n  claude-code:\n    version: '1'\n    argv_prefix: [claude]\n",
                "install_source",
                id="missing-required-field",
            ),
            pytest.param(
                "harnesses:\n  claude-code:\n    install_source: p\n"
                "    version: '1'\n    argv_prefix: [claude]\n    argv_suffix: []\n"
                "defaults:\n  version: '2'\n",
                "defaults",
                id="unknown-top-level-key",
            ),
            pytest.param(
                "harnesses:\n  grok:\n    install_method: curl-bash\n"
                "    install_source: not-a-url\n"
                "    version: '1'\n    argv_prefix: [grok]\n    argv_suffix: []\n",
                "http:// or https:// URL",
                id="downloaded-source-is-not-a-url",
            ),
            pytest.param(
                "harnesses:\n  grok:\n    install_method: pip\n"
                "    install_source: 'https://harness.invalid/grok.tar.gz'\n"
                "    version: '1'\n    argv_prefix: [grok]\n    argv_suffix: []\n",
                "not a bare package name",
                id="named-source-is-a-url",
            ),
            pytest.param("harnesses: {}\n", "non-empty", id="no-harness-declared"),
            pytest.param("- claude-code\n", "must be a YAML mapping", id="not-a-mapping"),
            pytest.param("harnesses: [\n", "not valid YAML", id="malformed-yaml"),
        ],
    )
    def test_malformed_registry_is_refused(self, tmp_path, document, expected):
        """A registry typo has to name the file and the offending key: it is an
        operator's config error, and silently dropping the entry would surface
        much later as an unknown-harness or missing-flag trial failure."""
        from tolokaforge_adapter_terminal_bench.harness import load_harness_registry

        path = tmp_path / "harnesses.yaml"
        path.write_text(document)
        with pytest.raises(ValueError, match=expected) as excinfo:
            load_harness_registry(path)
        assert str(path) in str(excinfo.value)

    def test_missing_file_is_refused(self, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import load_harness_registry

        missing = tmp_path / "absent.yaml"
        with pytest.raises(ValueError, match="does not exist"):
            load_harness_registry(missing)


class TestHarnessCommand:
    def test_claude_code_argv(self):
        """claude-code exports the model quartet, pipes the instruction via
        stdin, and drops ``--model`` because the env vars carry the model."""
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import harness_command

        command = harness_command("claude-code", "fix the bug", "anthropic/claude-sonnet-4-6")
        preamble, sep, cli = command.partition(" && printf ")
        assert sep, "claude-code pipes instruction via printf on stdin"
        # Preamble: five model exports, matching the reference env quartet + subagent var.
        exports = [p.strip() for p in preamble.split(" && ")]
        assert exports == [
            "export ANTHROPIC_MODEL=anthropic/claude-sonnet-4-6",
            "export ANTHROPIC_DEFAULT_SONNET_MODEL=anthropic/claude-sonnet-4-6",
            "export ANTHROPIC_DEFAULT_OPUS_MODEL=anthropic/claude-sonnet-4-6",
            "export ANTHROPIC_DEFAULT_HAIKU_MODEL=anthropic/claude-sonnet-4-6",
            "export CLAUDE_CODE_SUBAGENT_MODEL=anthropic/claude-sonnet-4-6",
        ]
        # printf part: instruction on stdin, no positional argv arg.
        printf_prefix, _, cli_only = ("printf " + cli).partition(" | ")
        assert shlex.split(printf_prefix) == ["printf", "%s", "fix the bug"]
        assert shlex.split(cli_only) == [
            "claude",
            "--verbose",
            "--output-format=stream-json",
            "--permission-mode=bypassPermissions",
            "--print",
        ]

    def test_codex_argv_shape(self):
        """codex chains a config.toml + auth.json write before the CLI. The
        CLI portion, after the final ``&&``, is what has to match the pinned
        shape — instruction stays on positional argv."""
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import harness_command

        _, _, cli = harness_command("codex", "do it", "openai/gpt-5-codex").rpartition(" && ")
        assert shlex.split(cli) == [
            "codex",
            "exec",
            "--model",
            "gpt-5-codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-c",
            "model_reasoning_effort=high",
            "do it",
        ]

    def test_gemini_argv_shape(self):
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import harness_command

        assert shlex.split(harness_command("gemini-cli", "do it", "google/gemini-2.5-flash")) == [
            "gemini",
            "--model",
            "gemini-2.5-flash",
            "--yolo",
            "--prompt",
            "do it",
        ]

    def test_codex_writes_config_toml_and_auth_json_before_the_cli(self):
        """codex reads ``openai_base_url`` from ``$CODEX_HOME/config.toml`` and
        the API key from ``$CODEX_HOME/auth.json``. The env vars they mirror
        both land the CLI at 401s without the files (config.toml drop routes
        to api.openai.com; missing auth.json earns "No cookie auth credentials
        found" from OpenRouter)."""
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        # codex still uses positional-argv instruction, so the CLI is chained
        # after " && " and everything before that is preamble + the CLI itself.
        preamble, sep, _ = harness_command("codex", "do it", "m").rpartition(" && ")
        assert sep, "codex must chain a preamble before the CLI"
        assert "config.toml" in preamble
        assert "openai_base_url" in preamble
        assert "auth.json" in preamble
        assert "OPENAI_API_KEY" in preamble

    def test_gemini_has_no_preamble_no_stdin(self):
        """A CLI without config_files, without env_model_vars, and with
        argv-channel instruction publishes the CLI command alone — no shell
        scaffolding for readers of the metadata to peel off."""
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        command = harness_command("gemini-cli", "go", "google/gemini-2.5-flash")
        assert "&&" not in command
        assert "printf" not in command
        assert command.startswith("gemini ")

    def test_instruction_is_one_shell_argument(self):
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import harness_command

        instruction = "don't $EXPAND `me`;\nsecond line"
        _, _, cli = harness_command("codex", instruction, "m").rpartition(" && ")
        argv = shlex.split(cli)
        assert argv[-1] == instruction

    def test_engine_loop_has_no_command(self):
        from tolokaforge_adapter_terminal_bench.harness import ENGINE_LOOP, harness_command

        with pytest.raises(ValueError, match="runs no CLI"):
            harness_command(ENGINE_LOOP, "anything", "m")

    def test_unknown_harness_names_accepted_set(self):
        from tolokaforge_adapter_terminal_bench.harness import validate_harness

        with pytest.raises(ValueError, match="claude-code"):
            validate_harness("bogus")

    def test_terminus_2_is_not_an_accepted_harness(self):
        """This repo installs no Terminus-2 scaffold, so no trial may claim it."""
        from tolokaforge_adapter_terminal_bench.harness import (
            accepted_harnesses,
            validate_harness,
        )

        assert "terminus-2" not in accepted_harnesses()
        with pytest.raises(ValueError, match="not supported"):
            validate_harness("terminus-2")


class TestHarnessConfigFiles:
    """CLIs configured by file: rendered from the declared variables only."""

    @staticmethod
    def _spec(**overrides):
        from tolokaforge_adapter_terminal_bench.harness import HarnessSpec

        return HarnessSpec(
            install_source="cli", version="1", argv_prefix=("cli",), argv_suffix=(), **overrides
        )

    def test_config_toml_renders_the_effective_base_url(self):
        """The endpoint the trial's container will carry, not the shipped
        default — a run config pointing the harness at its own gateway has to
        reach the file codex actually reads."""
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        command = harness_command(
            "codex",
            "do it",
            "m",
            provider_env={
                "OPENAI_BASE_URL": "https://gateway.invalid/v1",
                "OPENAI_API_KEY": "sk-not-in-the-command",
            },
        )
        assert 'openai_base_url = \\"https://gateway.invalid/v1\\"' in command

    def test_auth_json_names_the_key_env_var_and_never_its_value(self):
        """The command lands on ``TaskDescription.metadata`` and from there in
        the trial artifacts, so the credential reaches the file through the
        container's environment instead."""
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        command = harness_command(
            "codex",
            "do it",
            "m",
            provider_env={
                "OPENAI_BASE_URL": "https://gateway.invalid/v1",
                "OPENAI_API_KEY": "sk-not-in-the-command",
            },
        )
        assert "$OPENAI_API_KEY" in command
        assert "sk-not-in-the-command" not in command

    def test_rendered_files_land_where_the_cli_reads_them(self, tmp_path):
        """The assembled preamble is shell, so run it: the quoting, the
        ``$HOME``-rooted path and the credential expansion all have to survive
        a real shell, and none of that is visible in the string."""
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text("#!/bin/sh\n")
        (bin_dir / "codex").chmod(0o755)
        command = harness_command("codex", "do it", "openrouter/openai/gpt-5-mini")

        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "OPENAI_API_KEY": "sk-from-the-container",
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert (tmp_path / ".codex" / "config.toml").read_text() == (
            'openai_base_url = "https://openrouter.ai/api/v1"\n'
        )
        assert (tmp_path / ".codex" / "auth.json").read_text() == (
            '{"OPENAI_API_KEY": "sk-from-the-container"}\n'
        )

    def test_undeclared_variable_is_refused_at_construction(self):
        from tolokaforge_adapter_terminal_bench.harness import CONFIG_TEMPLATE_VARIABLES

        with pytest.raises(ValueError, match="undeclared variable"):
            self._spec(config_files={"/etc/cli.toml": "key = {{ api_key }}\n"})
        assert "api_key" not in CONFIG_TEMPLATE_VARIABLES

    def test_relative_path_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="is relative"):
            self._spec(config_files={"cli.toml": "key = {{ model }}\n"})

    def test_every_declared_variable_renders(self):
        """The whitelist is the contract an operator writes templates against."""
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        spec = self._spec(
            config_files={
                "$HOME/cli.toml": (
                    "model={{ model }} provider={{ provider }} "
                    "base_url={{ base_url }} key=${{ api_key_env }}\n"
                )
            },
        )
        command = harness_command(
            "cli",
            "go",
            "openrouter/openai/gpt-5-mini",
            registry={"cli": spec},
            provider_env={"OPENAI_BASE_URL": "https://x.invalid/v1", "OPENAI_API_KEY": "s"},
        )
        assert (
            "model=openai/gpt-5-mini provider=openrouter "
            "base_url=https://x.invalid/v1 key=$OPENAI_API_KEY" in command
        )

    @pytest.mark.parametrize(
        ("template", "provider_env"),
        [
            pytest.param(
                "url={{ base_url }}\n",
                {"OPENAI_BASE_URL": "https://a.invalid", "ANTHROPIC_BASE_URL": "b"},
                id="two-base-urls",
            ),
            pytest.param(
                "key=${{ api_key_env }}\n",
                {"OPENAI_API_KEY": "sk-a", "ANTHROPIC_API_KEY": "sk-b"},
                id="two-api-keys",
            ),
        ],
    )
    def test_ambiguous_provider_envelope_is_refused(self, template, provider_env):
        """Two endpoints — or two keys — leave no single answer for the template."""
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        spec = self._spec(config_files={"/etc/cli.toml": template})
        with pytest.raises(ValueError, match="several entries"):
            harness_command("cli", "go", "m", registry={"cli": spec}, provider_env=provider_env)


class TestModelFlagStyle:
    def test_space_style_is_two_argv_words(self):
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import harness_command

        argv = shlex.split(harness_command("gemini-cli", "go", "google/gemini-2.5-flash"))
        assert argv[1:3] == ["--model", "gemini-2.5-flash"]

    def test_equals_style_is_one_argv_word(self):
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import HarnessSpec, harness_command

        spec = HarnessSpec(
            install_source="opencode",
            version="1",
            argv_prefix=("opencode", "run"),
            argv_suffix=(),
            model_flag_style="equals",
        )
        command = harness_command("opencode", "go", "openrouter/openai/gpt-5", {"opencode": spec})
        assert shlex.split(command) == ["opencode", "run", "--model=openai/gpt-5", "go"]


class TestHarnessModelPrefix:
    """A vendor CLI reaches OpenRouter through ``*_BASE_URL``, not litellm."""

    def test_openrouter_prefix_stripped_for_a_vendor_cli(self):
        """Claude-code no longer emits ``--model`` on the CLI (env quartet
        carries the model), but the resolved model name must still land in
        the env quartet with the ``openrouter/`` prefix stripped."""
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        command = harness_command("claude-code", "go", "openrouter/anthropic/claude-sonnet-4-6")
        assert "export ANTHROPIC_MODEL=anthropic/claude-sonnet-4-6 " in command
        assert "openrouter/anthropic/claude-sonnet-4-6" not in command

    def test_codex_gets_the_bare_model_name(self):
        """codex refuses ``openai/gpt-5-mini`` as off-catalog and drops
        OPENAI_BASE_URL — the reference fix is the last-path-segment strip."""
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import harness_command

        argv = shlex.split(harness_command("codex", "go", "openrouter/openai/gpt-5-mini"))
        assert argv[argv.index("--model") + 1] == "gpt-5-mini"

    def test_gemini_cli_gets_the_bare_model_name(self):
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import harness_command

        argv = shlex.split(
            harness_command("gemini-cli", "go", "openrouter/google/gemini-2.5-flash")
        )
        assert argv[argv.index("--model") + 1] == "gemini-2.5-flash"

    def test_a_bare_model_name_is_untouched_for_claude_code(self):
        from tolokaforge_adapter_terminal_bench.harness import harness_model

        assert (
            harness_model("anthropic/claude-sonnet-4-6", "claude-code")
            == "anthropic/claude-sonnet-4-6"
        )

    def test_only_a_leading_prefix_is_stripped(self):
        from tolokaforge_adapter_terminal_bench.harness import harness_model

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
        assert main["image"] == "tbench-layered:local-claude-code-2.1.231"
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
        assert compose["services"]["main"]["image"] == "tbench-prebuilt:local-claude-code-2.1.231"

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
        from tolokaforge_adapter_terminal_bench.harness import HarnessSpec

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
        from tolokaforge_adapter_terminal_bench.harness import HARNESSES

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
        from tolokaforge_adapter_terminal_bench.harness import HARNESSES

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
        from tolokaforge_adapter_terminal_bench.harness import HARNESSES

        assert self._adapter(fixture_dir, tmp_path, None).harnesses == HARNESSES


class TestHarnessRegistryPluginDiscovery:
    """A pip-installed bundle contributes harnesses without an adapter release."""

    _TASKS_DIR = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    @pytest.fixture(autouse=True)
    def _isolated_discovery(self, monkeypatch):
        """Drop the per-group entry-point cache around every case.

        ``discover_entry_points`` caches its scan, so an injected plugin set
        would otherwise leak into the next case — including into the
        no-plugin-installed case, which asserts the opposite.
        """
        from tolokaforge.core import plugin_registry

        plugin_registry._clear_discovery_cache()
        yield
        plugin_registry._clear_discovery_cache()

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

    def test_nothing_installed_contributes_nothing(self):
        """The common case: no plugin, no change to what the adapter ships."""
        from tolokaforge_adapter_terminal_bench.harness import (
            discover_plugin_harness_registries,
        )

        discovered = discover_plugin_harness_registries()
        assert discovered.harnesses == {}
        assert discovered.bundles == ()

    def test_baseline_resolution_names_no_plugin_and_no_overlay(self):
        """A run with nothing installed and no overlay says so, rather than
        leaving a reader to infer it from a registry that happens to match."""
        from tolokaforge_adapter_terminal_bench.harness import (
            HARNESSES,
            resolve_effective_registry,
        )

        resolved = resolve_effective_registry()
        assert resolved.harnesses == HARNESSES
        assert resolved.plugin_bundles == ()
        assert resolved.overlay_file is None

    def test_installed_bundle_is_loaded_from_its_packaged_yaml(self, monkeypatch, plugin):
        from tolokaforge_adapter_terminal_bench.harness import (
            PluginBundle,
            discover_plugin_harness_registries,
        )

        self._install(
            monkeypatch,
            plugin(
                "acme_harnesses",
                "acme-tbench-harnesses",
                self._bundle("acme-cli", "4.5.6"),
                version="2.3.4",
            ),
        )
        discovered = discover_plugin_harness_registries()
        assert list(discovered.harnesses) == ["acme-cli"]
        assert discovered.harnesses["acme-cli"].version == "4.5.6"
        assert discovered.harnesses["acme-cli"].argv_prefix == ("acme-cli",)
        assert discovered.bundles == (
            PluginBundle(
                distribution="acme-tbench-harnesses",
                version="2.3.4",
                harnesses=("acme-cli",),
            ),
        )

    def test_bundles_are_ordered_by_distribution(self, monkeypatch, plugin):
        """Distribution order, not entry-point order: the two disagree here,
        and the recorded order must not depend on how a plugin names its
        package."""
        from tolokaforge_adapter_terminal_bench.harness import (
            discover_plugin_harness_registries,
        )

        self._install(
            monkeypatch,
            plugin("acme_harnesses", "zeta-harnesses", self._bundle("acme-cli", "4.5.6")),
            plugin("globex_harnesses", "alpha-harnesses", self._bundle("globex-cli", "7.8.9")),
        )
        discovered = discover_plugin_harness_registries()
        assert [bundle.distribution for bundle in discovered.bundles] == [
            "alpha-harnesses",
            "zeta-harnesses",
        ]
        assert [bundle.harnesses for bundle in discovered.bundles] == [
            ("globex-cli",),
            ("acme-cli",),
        ]

    def test_entry_point_without_a_distribution_reports_no_version(self, monkeypatch, plugin):
        """A programmatically registered entry point has no distribution to
        read a version off, and a fabricated one would be a lie."""
        from tolokaforge_adapter_terminal_bench.harness import (
            discover_plugin_harness_registries,
        )

        self._install(
            monkeypatch,
            plugin("acme_harnesses", None, self._bundle("acme-cli", "4.5.6")),
        )
        (bundle,) = discover_plugin_harness_registries().bundles
        assert bundle.distribution == "acme_harnesses"
        assert bundle.version is None

    def test_two_plugins_claiming_one_harness_name_are_refused(self, monkeypatch, plugin):
        """No safe pick: the two bundles disagree about what the name installs
        and how it is invoked, so install order must not decide it."""
        from tolokaforge_adapter_terminal_bench.harness import (
            discover_plugin_harness_registries,
        )

        self._install(
            monkeypatch,
            plugin("acme_harnesses", "acme-tbench-harnesses", self._bundle("shared-cli", "1.0.0")),
            plugin("globex_harnesses", "globex-harnesses", self._bundle("shared-cli", "2.0.0")),
        )
        with pytest.raises(ValueError) as excinfo:
            discover_plugin_harness_registries()
        message = str(excinfo.value)
        assert "shared-cli" in message
        assert "acme-tbench-harnesses" in message
        assert "globex-harnesses" in message

    def test_plugin_replaces_a_shipped_entry_whole(self, monkeypatch, plugin):
        """Whole-entry replacement, as at every other layer: the shipped
        ``codex`` fields the bundle omits are gone, not merged underneath."""
        from tolokaforge_adapter_terminal_bench.harness import (
            HARNESSES,
            resolve_effective_registry,
        )

        self._install(
            monkeypatch,
            plugin("acme_harnesses", "acme-tbench-harnesses", self._bundle("codex", "9.9.9")),
        )
        effective = resolve_effective_registry().harnesses
        assert effective["codex"].version == "9.9.9"
        assert effective["codex"].config_files == {}
        assert HARNESSES["codex"].config_files != {}
        assert effective["claude-code"] == HARNESSES["claude-code"]

    def test_operator_overlay_wins_over_a_plugin(self, monkeypatch, plugin, tmp_path):
        from tolokaforge_adapter_terminal_bench.harness import resolve_effective_registry

        self._install(
            monkeypatch,
            plugin("acme_harnesses", "acme-tbench-harnesses", self._bundle("acme-cli", "4.5.6")),
        )
        overlay = tmp_path / "harness_presets.yaml"
        overlay.write_text(self._bundle("acme-cli", "0.0.0-overlay"))
        resolved = resolve_effective_registry(str(overlay))
        assert resolved.harnesses["acme-cli"].version == "0.0.0-overlay"
        assert resolved.overlay_file == overlay.resolve()

    def test_relative_overlay_is_recorded_as_its_resolved_path(self, monkeypatch, tmp_path):
        """The recorded overlay must name one file whatever directory the run
        started in, so a relative argument is recorded resolved."""
        from tolokaforge_adapter_terminal_bench.harness import resolve_effective_registry

        overlay = tmp_path / "harness_presets.yaml"
        overlay.write_text(self._bundle("acme-cli", "4.5.6"))
        monkeypatch.chdir(tmp_path)
        recorded = resolve_effective_registry("harness_presets.yaml").overlay_file
        assert recorded is not None
        assert recorded.is_absolute()
        assert recorded == overlay.resolve()

    def test_disable_harness_plugins_bypasses_discovery(self, monkeypatch, tmp_path):
        """An audit run pins the registry to what the adapter ships, whatever
        else happens to be installed in the environment — so discovery must not
        run at all, not merely have its result discarded."""
        import importlib.metadata

        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter
        from tolokaforge_adapter_terminal_bench.harness import (
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
        assert td.metadata["agent_harness_version"] == "2.1.231"

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
