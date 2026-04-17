"""Terminal-bench adapter for tolokaforge.

Maps terminal-bench task directories (docker-compose.yaml + task.yaml/task.toml)
to the tolokaforge adapter interface.  All Docker Compose lifecycle management
is delegated to the Runner via ``DockerComposeExecToolWrapper``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    GradingCombineConfig,
    GradingConfig,
    InitialStateConfig,
    TaskConfig,
    ToolsConfig,
    Trajectory,
    UserSimulatorConfig,
)
from tolokaforge.runner.models import (
    AdapterType,
    GradingConfig as RunnerGradingConfig,
    InvocationStyle,
    TaskDescription,
    ToolSchema,
    ToolSource,
)
from tolokaforge.runner.models import (
    InitialStateConfig as RunnerInitialStateConfig,
    UserSimulatorConfig as RunnerUserSimulatorConfig,
)

from tolokaforge_adapter_terminal_bench.compose_env import (
    bundle_task_artifacts,
    resolve_tbench_env_vars,
)
from tolokaforge_adapter_terminal_bench.task_parser import (
    TerminalBenchTask,
    discover_tasks,
)


class TerminalBenchAdapter(BaseAdapter):
    """Adapter that runs terminal-bench tasks inside Docker Compose stacks."""

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self.terminal_bench_dir = Path(params.get("terminal_bench_dir", "."))
        self.image_registry: str | None = params.get("image_registry")
        self.task_id_filter: list[str] | None = params.get("task_ids")
        # Path where Runner container sees tasks (if different from host path)
        self.runner_task_dir: str | None = params.get("runner_task_dir")
        self._tasks: dict[str, TerminalBenchTask] = {}

    # -- discovery ------------------------------------------------------------

    def get_task_ids(self) -> list[str]:
        self._ensure_discovered()
        ids = list(self._tasks.keys())
        if self.task_id_filter:
            ids = [tid for tid in ids if tid in self.task_id_filter]
        return ids

    def _ensure_discovered(self) -> None:
        if not self._tasks:
            self._tasks = discover_tasks(self.terminal_bench_dir)

    def get_task_dir(self, task_id: str) -> Path:
        self._ensure_discovered()
        return self._tasks[task_id].task_dir

    # -- task loading ---------------------------------------------------------

    def get_task(self, task_id: str) -> TaskConfig:
        self._ensure_discovered()
        meta = self._tasks[task_id]
        return TaskConfig(
            task_id=task_id,
            name=task_id,
            category="terminal",
            description=meta.instruction[:500] if meta.instruction else task_id,
            adapter_type="terminal_bench",
            initial_user_message=meta.instruction,
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(
                agent={"enabled": ["bash"]},
                user={"enabled": []},
            ),
            user_simulator=UserSimulatorConfig(mode="scripted", scripted_flow=[]),
            grading="__adapter__",
            system_prompt="__adapter__",
            adapter_settings={
                "compose_file": str(meta.compose_file),
                "task_dir": str(meta.task_dir),
                "difficulty": meta.difficulty,
                "tags": meta.tags,
            },
        )

    # -- environment ----------------------------------------------------------

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        return AdapterEnvironment(
            data={},
            tools=[],
            wiki="",
            rules=[],
            task_dir=self._tasks[task_id].task_dir,
        )

    # -- tools ----------------------------------------------------------------

    def get_tools(self, task_id: str) -> list[Any]:
        return []

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Any]:
        return []

    # -- prompts --------------------------------------------------------------

    def get_system_prompt(self, task_id: str) -> str:
        return (
            "You are an expert developer working inside a Linux container. "
            "Use the bash tool to execute commands. "
            "Fix the issues described in the user message."
        )

    # -- grading config -------------------------------------------------------

    def get_grading_config(self, task_id: str) -> GradingConfig:
        return GradingConfig(
            combine=GradingCombineConfig(
                method="weighted",
                weights={"custom_checks": 1.0},
                pass_threshold=0.5,
            ),
        )

    # -- Docker runtime -------------------------------------------------------

    def to_task_description(self, task_id: str) -> TaskDescription:
        self._ensure_discovered()
        meta = self._tasks[task_id]
        env_vars = resolve_tbench_env_vars(meta, self.image_registry)

        # Decide task_dir strategy
        if self.image_registry:
            artifacts = bundle_task_artifacts(meta)
            task_dir_value = "__artifacts__"
        else:
            artifacts = {}
            # Use runner_task_dir if set (for Docker: runner sees a different mount path)
            if self.runner_task_dir:
                task_dir_value = f"{self.runner_task_dir}/{meta.task_id}"
            else:
                task_dir_value = str(meta.task_dir)

        return TaskDescription(
            task_id=task_id,
            name=task_id,
            category="terminal",
            description=meta.instruction[:500] if meta.instruction else task_id,
            adapter_type=AdapterType.TERMINAL_BENCH,
            system_prompt=self.get_system_prompt(task_id),
            agent_tools=[
                ToolSchema(
                    name="bash",
                    description="Execute a bash command inside the task container",
                    parameters={
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Shell command to run",
                            }
                        },
                        "required": ["command"],
                    },
                    category="compute",
                    timeout_s=120.0,
                    source=ToolSource(
                        toolset="terminal_bench",
                        module_path="",
                        class_name="bash",
                        invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                        extra={
                            "compose_file": "docker-compose.yaml",
                            "task_dir": task_dir_value,
                            "service": "main",
                            "env_vars": env_vars,
                        },
                    ),
                )
            ],
            user_tools=[],
            initial_state=RunnerInitialStateConfig(),
            user_simulator=RunnerUserSimulatorConfig(mode="scripted"),
            grading=RunnerGradingConfig(
                combine_method="weighted",
                weights={"custom_checks": 1.0},
                pass_threshold=0.5,
            ),
            tool_artifacts=artifacts,
            metadata={
                "difficulty": meta.difficulty,
                "tags": meta.tags,
                "verifier_timeout_sec": meta.verifier_timeout_sec,
            },
        )

    # -- lifecycle helpers ----------------------------------------------------

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
        # Not called in Docker runtime — grading happens in Runner via GradeTrial RPC.
        return Grade(
            binary_pass=False,
            score=0.0,
            components=GradeComponents(),
            reasons="Terminal-bench grading must run via Runner GradeTrial RPC",
        )
