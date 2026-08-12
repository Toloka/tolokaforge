"""Unit tests for terminal-bench adapter and Docker Compose exec wrapper."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

    def test_every_accepted_harness_has_a_dispatch_branch(self):
        from tolokaforge_adapter_terminal_bench.harness import (
            ACCEPTED_HARNESSES,
            INSTALL_SCRIPT,
        )

        text = INSTALL_SCRIPT.read_text()
        for name in ACCEPTED_HARNESSES:
            assert f"\n    {name})" in text, f"no case branch for {name!r}"

    def test_no_op_harness_exits_clean_without_network(self):
        from tolokaforge_adapter_terminal_bench.harness import (
            INSTALL_SCRIPT,
            NO_OP_HARNESS,
        )

        proc = subprocess.run(
            ["sh", str(INSTALL_SCRIPT), NO_OP_HARNESS],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr

    def test_unknown_harness_aborts_naming_accepted_set(self):
        from tolokaforge_adapter_terminal_bench.harness import (
            ACCEPTED_HARNESSES,
            INSTALL_SCRIPT,
        )

        proc = subprocess.run(
            ["sh", str(INSTALL_SCRIPT), "not-a-harness"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0
        for name in ACCEPTED_HARNESSES:
            assert name in proc.stderr

    def test_missing_argument_aborts(self):
        from tolokaforge_adapter_terminal_bench.harness import INSTALL_SCRIPT

        proc = subprocess.run(
            ["sh", str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0
        assert "unknown harness" in proc.stderr


class TestHarnessCommand:
    def test_claude_code_argv(self):
        from tolokaforge_adapter_terminal_bench.harness import harness_command

        assert harness_command("claude-code", "fix the bug") == "claude --print 'fix the bug'"

    def test_instruction_is_one_shell_argument(self):
        import shlex

        from tolokaforge_adapter_terminal_bench.harness import harness_command

        instruction = "don't $EXPAND `me`;\nsecond line"
        argv = shlex.split(harness_command("codex", instruction))
        assert argv == ["codex", "exec", instruction]

    def test_no_op_harness_has_no_command(self):
        from tolokaforge_adapter_terminal_bench.harness import (
            NO_OP_HARNESS,
            harness_command,
        )

        with pytest.raises(ValueError, match="runs no CLI"):
            harness_command(NO_OP_HARNESS, "anything")

    def test_unknown_harness_names_accepted_set(self):
        from tolokaforge_adapter_terminal_bench.harness import validate_harness

        with pytest.raises(ValueError, match="claude-code"):
            validate_harness("bogus")


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
        assert main["image"] == "tbench-layered:local-claude-code"
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
        assert "install-harness.sh gemini-cli" in dockerfile
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
        assert compose["services"]["main"]["image"] == "tbench-prebuilt:local-claude-code"

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
        assert compose["services"]["main"]["image"] == "reg.example/tbench/registry:v1-codex"
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

    def test_task_declaring_base_service_name_raises(self, tmp_path):
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
            materialise_task_environment(meta, staging_root=tmp_path / "staging")

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

    @pytest.fixture
    def env_backed_secrets(self, monkeypatch):
        """Install an ``os.environ``-only ``SecretManager`` as the process default.

        Patches the module global rather than calling ``init_default_from`` so
        the singleton is restored when the test ends — a leaked manager would
        make every later test's secret reads depend on test ordering.
        """
        from tolokaforge.secrets import SecretManager
        from tolokaforge.secrets.providers import EnvProvider

        monkeypatch.setattr(
            "tolokaforge.secrets.manager._default_manager", SecretManager([EnvProvider()])
        )

    def test_secret_reference_is_expanded(
        self, fixture_dir, tmp_path, monkeypatch, env_backed_secrets
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-secret-manager")
        adapter = self._adapter(
            fixture_dir,
            tmp_path,
            agent_provider_env={"ANTHROPIC_API_KEY": "${secret:ANTHROPIC_API_KEY}"},
        )
        assert adapter.agent_provider_env == {"ANTHROPIC_API_KEY": "sk-from-secret-manager"}

    def test_unresolvable_secret_reference_fails_loud(
        self, fixture_dir, tmp_path, monkeypatch, env_backed_secrets
    ):
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

    def test_no_provider_env_leaves_stack_inputs_empty(self, fixture_dir, tmp_path):
        adapter = self._adapter(fixture_dir, tmp_path)
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


class TestHarnessTaskDescriptionMetadata:
    """What the engine core reads to decide a trial runs a harness CLI."""

    @pytest.fixture
    def fixture_dir(self) -> Path:
        return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"

    def test_harness_command_carries_the_instruction(self, fixture_dir, tmp_path):
        import shlex

        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {
                "terminal_bench_dir": str(fixture_dir),
                "staging_root": str(tmp_path),
                "agent_harness": "claude-code",
            }
        )
        td = adapter.to_task_description("echo-hello")
        assert td.metadata["agent_harness"] == "claude-code"
        argv = shlex.split(td.metadata["agent_harness_command"])
        assert argv[:2] == ["claude", "--print"]
        assert argv[2] == adapter.get_task("echo-hello").initial_user_message

    def test_default_harness_publishes_no_command(self, fixture_dir, tmp_path):
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        adapter = TerminalBenchAdapter(
            {"terminal_bench_dir": str(fixture_dir), "staging_root": str(tmp_path)}
        )
        td = adapter.to_task_description("echo-hello")
        assert td.metadata["agent_harness"] == "terminus-2"
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
