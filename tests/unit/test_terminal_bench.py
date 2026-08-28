"""Unit tests for terminal-bench adapter and Docker Compose exec wrapper."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

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
        fake = MagicMock()
        fake.communicate.return_value = ("hello world\n", "")
        fake.returncode = 0
        with patch("subprocess.Popen", return_value=fake) as mock_popen:
            result = wrapper._exec_sync("echo hello world", 30.0)
            assert result == "hello world\n"
            argv = mock_popen.call_args.args[0]
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
        fake = MagicMock()
        fake.communicate.return_value = ("partial", "error msg")
        fake.returncode = 1
        with patch("subprocess.Popen", return_value=fake):
            result = wrapper._exec_sync("bad cmd", 30.0)
            assert "partial" in result
            assert "[exit code: 1]" in result
            assert "error msg" in result

    def test_exec_before_start_fails_loud(self, wrapper):
        with pytest.raises(ToolExecutionError, match="container name unresolved"):
            wrapper._exec_sync("echo hi", 30.0)

    def test_exec_sync_timeout_preserves_partial_stdout_bytes_payload(self, wrapper):
        """A slow agent that runs past the tool's own budget must still have
        its already-emitted stdout surface in the returned string. The prior
        ``subprocess.run(capture_output=True, timeout=…)`` shape discarded
        stdout on TimeoutExpired, turning a legitimately-running-but-slow CLI
        into an opaque "nothing happened" that made native-pack coding-harness
        smokes unobservable.

        The payload is explicitly ``bytes`` here — ``TimeoutExpired.stdout``
        carries the raw child buffer even when ``Popen`` was opened with
        ``text=True``. A prior version of this path did ``str + bytes`` and
        raised ``TypeError: can't concat str to bytes``, which the runner
        then logged as the CLI's output. The decode step is what this test
        pins."""
        wrapper.start(ToolLifecycleContext(trial_id="task-1:0"))
        fake = MagicMock()
        fake.communicate.side_effect = [
            subprocess.TimeoutExpired(
                cmd=["docker", "exec"],
                timeout=1.0,
                output=b"partial cli stdout\n",
                stderr=b"partial cli stderr\n",
            ),
            (b"", b""),
        ]
        fake.returncode = -9  # after .kill()
        with patch("subprocess.Popen", return_value=fake):
            result = wrapper._exec_sync("slow-cli --print", 1.0)
        assert "partial cli stdout" in result
        assert "timed out after 1.0s" in result
        assert "partial cli stderr" in result
        assert "can't concat str to bytes" not in result
        fake.kill.assert_called_once()

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


class TestComposeSynthesisReservesTheHarnessBaseName:
    """A coding-harness driver later injects a build-only ``<agent>-base``
    service (see :class:`~tolokaforge.core.drivers.coding_harness.CodingHarnessDriver`);
    a task pack cannot declare a service that name would collide with,
    whether or not this run ends up driven by a harness."""

    def test_task_declaring_the_base_service_name_always_raises(self, tmp_path):
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


class TestHarnessSkillsBundleDeclaration:
    """``task.yaml``'s ``harness_skills_dir`` is validated at discovery time.

    The parity policy refuses the out-of-tree host's ``~/.claude/skills``
    smuggling because a reward that depends on the eval machine's home
    directory is not a reproducible reward: the bundle must be declared by
    the pack and contained inside it. Consuming the declaration (copying the
    bundle into a coding-harness CLI's image layer) is
    :class:`~tolokaforge.core.drivers.coding_harness.CodingHarnessDriver`
    scope, not this adapter's — the field is parsed and validated here, not
    installed.
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


class TestHarnessSpecSkillsDirTargetValidation:
    """``HarnessSpec.skills_dir_target``'s own validation — a registry-level
    concern independent of any adapter. Consuming a validated target to
    deliver a task's skills bundle is
    :class:`~tolokaforge.core.drivers.coding_harness.CodingHarnessDriver`
    scope; no driver implements that yet (tracked separately)."""

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
    """The adapter always declares exactly one build — its own pack image.

    A coding-harness driver's layered-image build is declared by the driver
    (via ``apply_container_layers``) and merged in by the orchestrator; the
    adapter has no harness-mode branch to add a second entry here."""

    def test_declares_one_build(self, tmp_path):
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


class TestStageTaskAndCodingHarnessDriverIntegration:
    """``TerminalBenchAdapter.stage_task`` hands
    :class:`~tolokaforge.core.drivers.coding_harness.CodingHarnessDriver` a
    plain per-task staging root; the driver layers the CLI install, the
    ``bash`` tool schema, ``test_execution`` grading, and the four-key
    metadata handshake onto it. The adapter itself carries no coding-harness
    state.
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

    @staticmethod
    def _driver():
        from tolokaforge.core.drivers.coding_harness import CodingHarnessDriver, HarnessSelection

        return CodingHarnessDriver(
            HarnessSelection(
                agent_harness="claude-code",
                agent_model="openrouter/anthropic/claude-sonnet-4-6",
            )
        )

    def test_stage_task_reports_the_pack_image_and_base_service_name(self, adapter):
        staged = adapter.stage_task("echo-hello")
        assert staged.agent_service == "main"
        assert staged.base_build_service == "main-base"
        assert staged.base_image == "tbench-echo-hello:local"
        assert staged.compose_project_prefix == "tbench_"
        assert staged.compose_file.exists()

    def test_decorated_description_carries_the_bash_tool_and_test_execution_grading(self, adapter):
        staged = adapter.stage_task("echo-hello")
        base = adapter.to_task_description("echo-hello")
        td = self._driver().decorate_task_description(base, staged=staged)

        assert len(td.agent_tools) == 1
        tool = td.agent_tools[0]
        assert tool.name == "bash"
        assert tool.source.invocation_style == "docker_compose_exec"
        assert tool.source.extra == {"service": "main", "compose_project_prefix": "tbench_"}
        assert td.grading.grading_method == "test_execution"

    def test_decorated_metadata_carries_the_real_instruction(self, adapter):
        staged = adapter.stage_task("echo-hello")
        base = adapter.to_task_description("echo-hello")
        td = self._driver().decorate_task_description(base, staged=staged)

        for key in (
            "agent_harness",
            "agent_harness_version",
            "agent_harness_model",
            "agent_harness_command",
        ):
            assert key in td.metadata, f"missing metadata key {key!r}"
        instruction = adapter.get_task("echo-hello").initial_user_message
        assert instruction in td.metadata["agent_harness_command"]
        assert "TOLOKAFORGE_HARNESS_INSTRUCTION" not in td.metadata["agent_harness_command"]
        # Terminal-bench's own WORKDIR convention survives the driver's default.
        assert td.metadata["agent_visible_dir"] == "/app"

    def test_apply_container_layers_declares_base_then_layer(self, adapter):
        staged = adapter.stage_task("echo-hello")
        layers = self._driver().apply_container_layers(staged=staged)
        assert [b.service for b in layers.stack_requirements] == ["main-base", "main"]
        for build in layers.stack_requirements:
            assert build.compose_file == staged.compose_file
