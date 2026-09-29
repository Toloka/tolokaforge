"""Inspect AI adapter for tolokaforge.

Runs Inspect AI tasks under ``tolokaforge run`` by delegating execution to
``inspect_ai`` inside a trial container — the same shape as the terminal-bench
adapter. For each task the adapter synthesises a two-stack compose plan whose
agent container has ``inspect_ai`` + the task pack baked in, sets the trial's
"agent" step to a single ``inspect eval`` command (Inspect runs its own solver +
scorer), and grades via ``test_execution`` from the reward a generated
``tests/test.sh`` extracts from the ``.eval`` log.

Model credentials reach the container through ``tolokaforge.secrets``: values in
``agent_provider_env`` are resolved from ``${secret:NAME}`` refs at load time and
passed as per-trial compose inputs — never read from ``os.environ`` here.
"""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from tolokaforge.adapters.base import (
    AdapterEnvironment,
    BaseAdapter,
    ComposeImageBuild,
    DockerStackRequirements,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    GradingCombineConfig,
    GradingConfig,
    InitialStateConfig,
    TaskConfig,
    ToolsConfig,
    Trajectory,
)
from tolokaforge.core.project_loader import resolve as resolve_environment_patch
from tolokaforge.runner.models import (
    EnvironmentPatch,
    NetworkPolicy,
    RunnerGradingConfig,
    StackPatch,
    TaskDescription,
    ToolSchema,
    ToolSource,
)
from tolokaforge.secrets import expand_secret_refs, get_default
from tolokaforge_adapter_inspect_ai import compose_synthesis as cs
from tolokaforge_adapter_inspect_ai.inspect_loader import InspectTaskInfo, discover_inspect_tasks

_ADAPTER_TYPE = "inspect_ai"
_DEFAULT_INSPECT_VERSION = "0.3.271"
_DEFAULT_BASE_IMAGE = "python:3.12-slim-bookworm"
_DEFAULT_AGENT_TIMEOUT_S = 1800.0

# One scalar reward per task -> the same test_execution runner dispatch terminal-bench uses.
_TEST_EXECUTION_GRADING: dict[str, Any] = {
    "combine_method": "weighted",
    "weights": {"custom_checks": 1.0},
    "pass_threshold": 0.5,
    "grading_method": "test_execution",
}

_SYSTEM_PROMPT = "Executed by the Inspect AI runtime; the task's own solver drives the interaction."


class InspectAiAdapter(BaseAdapter):
    """Runs Inspect AI tasks by delegating execution to ``inspect_ai`` in a container."""

    requires_docker_cli_in_runner: ClassVar[bool] = True
    # The trial's "agent" step is one `inspect eval` command, not the engine LLM
    # loop — the run selects it with `models.agent.harness: inspect_ai`, which the
    # orchestrator lifts into `params.agent_model` (the model Inspect runs).
    supports_coding_harness: ClassVar[bool] = True

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        first_pack = str(self.task_packs[0]) if self.task_packs else None
        self.pack_dir = Path(
            params.get("inspect_task_dir") or first_pack or self.base_dir
        ).resolve()
        self.tasks_glob: list[str] | None = _as_list(params.get("tasks_glob"))
        self.task_id_filter: list[str] | None = _as_list(params.get("task_ids"))
        self.agent_model: str = params.get("agent_model") or ""
        self.inspect_version: str = params.get("inspect_version") or _DEFAULT_INSPECT_VERSION
        self.base_image: str = params.get("base_image") or _DEFAULT_BASE_IMAGE
        self.agent_timeout_s: float = float(
            params.get("agent_timeout_s") or _DEFAULT_AGENT_TIMEOUT_S
        )
        self.network_policy = NetworkPolicy(
            params.get("network_policy", NetworkPolicy.FULL_INTERNET.value)
        )
        staging_root = params.get("staging_root")
        self.staging_root = (
            Path(staging_root).expanduser().resolve()
            if staging_root
            else Path(tempfile.gettempdir()) / "tolokaforge-inspect"
        )
        self.agent_provider_env = _resolve_provider_env(params.get("agent_provider_env") or {})

        self._tasks: dict[str, InspectTaskInfo] = {}
        self._environments: dict[str, cs.MaterialisedEnvironment] = {}

    # -- discovery -----------------------------------------------------------

    def _ensure_discovered(self) -> None:
        if not self._tasks:
            self._tasks = discover_inspect_tasks(self.pack_dir, self.tasks_glob)

    def get_task_ids(self) -> list[str]:
        self._ensure_discovered()
        ids = list(self._tasks)
        if self.task_id_filter:
            ids = [tid for tid in ids if tid in self.task_id_filter]
        return ids

    def get_task_dir(self, task_id: str) -> Path:
        self._ensure_discovered()
        return self._tasks[task_id].task_dir

    def inspect_task(self, task_id: str) -> InspectTaskInfo:
        self._ensure_discovered()
        return self._tasks[task_id]

    def _environment(self, task_id: str) -> cs.MaterialisedEnvironment:
        cached = self._environments.get(task_id)
        if cached is not None:
            return cached
        env = cs.materialise_task_environment(
            self.pack_dir,
            task_id,
            staging_root=self.staging_root,
            inspect_version=self.inspect_version,
            base_image=self.base_image,
            provider_env_keys=sorted(self.agent_provider_env),
        )
        self._environments[task_id] = env
        return env

    # -- translation ---------------------------------------------------------

    def get_task(self, task_id: str) -> TaskConfig:
        self._ensure_discovered()
        info = self._tasks[task_id]
        return TaskConfig(
            task_id=task_id,
            name=info.name,
            category="inspect",
            description=f"Inspect AI task {info.name}",
            adapter_type=_ADAPTER_TYPE,
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(agent={"enabled": ["bash"]}, user={"enabled": []}),
            grading="__adapter__",
            policies={"agent_system_prompt": self.get_system_prompt(task_id)},
            environment_manifest=self._environment_patch(task_id),
            adapter_settings={"inspect_file": str(info.file), "inspect_task": info.name},
        )

    def get_system_prompt(self, task_id: str) -> str:
        return _SYSTEM_PROMPT

    def get_grading_config(self, task_id: str) -> GradingConfig:
        return GradingConfig(
            combine=GradingCombineConfig(
                method="weighted", weights={"custom_checks": 1.0}, pass_threshold=0.5
            ),
        )

    def preferred_grader_kind(self) -> str:
        return "test_execution"

    def docker_stack_requirements(self) -> DockerStackRequirements:
        builds = [
            ComposeImageBuild(
                compose_file=(env := self._environment(tid)).compose_file,
                service=env.agent_service,
                expected_image_ref=env.agent_image,
            )
            for tid in self.get_task_ids()
        ]
        return DockerStackRequirements(image_builds=builds)

    def _environment_patch(self, task_id: str) -> EnvironmentPatch:
        env = self._environment(task_id)
        inputs = {cs.provider_input(k): v for k, v in self.agent_provider_env.items()}
        return EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=env.engine_compose_file, stack_scope="run", runner_service="runner"
                ),
                "task": StackPatch(
                    compose_file=env.compose_file, stack_scope="trial", inputs=inputs
                ),
            },
            network_policy=self.network_policy,
        )

    def _eval_command(self, info: InspectTaskInfo, env: cs.MaterialisedEnvironment) -> str:
        rel = info.file.relative_to(self.pack_dir).as_posix()
        address = f"{cs.CONTAINER_TASK_PACK}/{rel}@{info.name}"
        return (
            f"mkdir -p {cs.INSPECT_LOG_DIR} && "
            f"inspect eval {shlex.quote(address)} "
            f"--model {shlex.quote(self.agent_model)} "
            f"--log-dir {cs.INSPECT_LOG_DIR} --log-format eval"
        )

    def to_task_description(self, task_id: str) -> TaskDescription:
        self._ensure_discovered()
        if not self.agent_model:
            raise ValueError(
                "inspect_ai adapter: `agent_model` is required to run a task "
                "(set evaluation.harness_adapter.params.agent_model to an inspect model, "
                "e.g. 'mockllm/model' or 'openai/gpt-4o')."
            )
        info = self._tasks[task_id]
        env = self._environment(task_id)
        manifest = resolve_environment_patch(None, self._environment_patch(task_id))
        exec_tool = ToolSchema(
            name="bash",
            description="Execute a bash command inside the inspect task container",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            category="compute",
            timeout_s=self.agent_timeout_s,
            source=ToolSource(
                toolset=_ADAPTER_TYPE,
                module_path="",
                class_name="bash",
                invocation_style="docker_compose_exec",
                extra={"service": env.agent_service, "compose_project_prefix": cs.PROJECT_PREFIX},
            ),
        )
        return TaskDescription(
            task_id=task_id,
            name=info.name,
            category="inspect",
            description=f"Inspect AI task {info.name}",
            adapter_type=_ADAPTER_TYPE,
            system_prompt=self.get_system_prompt(task_id),
            agent_tools=[exec_tool],
            grading=RunnerGradingConfig(**_TEST_EXECUTION_GRADING),
            environment_manifest=manifest,
            metadata={
                "agent_harness_command": self._eval_command(info, env),
                "inspect_task": info.name,
                "inspect_model": self.agent_model,
            },
        )

    # -- environment (Inspect owns the real environment) ---------------------

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        self._ensure_discovered()
        return AdapterEnvironment(
            data={}, tools=[], wiki="", rules=[], task_dir=self._tasks[task_id].task_dir
        )

    def get_tools(self, task_id: str) -> list[Any]:
        return []

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Any]:
        return []

    def reset_environment(self, env: AdapterEnvironment) -> None:
        pass

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        return None

    def grade(
        self,
        task_id: str,
        trajectory: Trajectory,
        final_state: dict[str, Any],
        env: AdapterEnvironment,
    ) -> Grade:
        return Grade(
            binary_pass=False,
            score=0.0,
            components=GradeComponents(),
            reasons="inspect_ai grading runs via the Runner GradeTrial RPC (test_execution)",
        )


def _resolve_provider_env(raw: dict[str, str]) -> dict[str, str]:
    if not raw:
        return {}
    secrets = get_default()
    return {
        key: expand_secret_refs(value, secrets, where=f"inspect_ai agent_provider_env[{key}]")
        for key, value in raw.items()
    }


def _as_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)
