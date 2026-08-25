"""Terminal-bench adapter for tolokaforge.

Emits an :class:`~tolokaforge.runner.models.EnvironmentPatch` on every
``TaskConfig`` and the resolved :class:`~tolokaforge.runner.models.EnvironmentManifest`
on every ``TaskDescription``. The synthesised compose file lives in a
staging directory materialised by
:mod:`tolokaforge_adapter_terminal_bench.compose_synthesis`; the
orchestrator's per-trial runtime brings the stack up and the runner-side
bash tool only ``docker exec``s into the already-running agent container.

Carries no coding-harness mode state: a run driven by a vendor CLI
(``models.agent.coding_harness``) is applied by the orchestrator's
selected :class:`~tolokaforge.core.agent_driver.AgentDriver` around this
adapter's plain output — see :meth:`TerminalBenchAdapter.stage_task`,
which hands the driver the per-task compose staging root it layers a
CLI install onto.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from tolokaforge.adapters.base import (
    AdapterEnvironment,
    BaseAdapter,
    ComposeImageBuild,
    DockerStackRequirements,
)
from tolokaforge.core.agent_driver import StagedTask
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
    AdapterType,
    EnvironmentPatch,
    InvocationStyle,
    NetworkPolicy,
    RunnerGradingConfig,
    RunnerInitialStateConfig,
    RunnerUserSimulatorConfig,
    StackPatch,
    TaskDescription,
    ToolSchema,
    ToolSource,
)
from tolokaforge_adapter_terminal_bench.compose_synthesis import (
    HARNESS_BASE_SERVICE_SUFFIX,
    PROJECT_PREFIX,
    MaterialisedEnvironment,
    agent_image_tag,
    materialise_task_environment,
)
from tolokaforge_adapter_terminal_bench.task_parser import (
    TerminalBenchTask,
    discover_tasks,
)

_AGENT_TOOL_TIMEOUT_S = 120.0

_REMOVED_PARAMS: dict[str, str] = {
    "runner_task_dir": (
        "task files are staged under `staging_root` (default: a "
        "`tolokaforge-tbench` directory under the system temp dir); "
        "the runner reads them through the synthesised compose file's "
        "relative bind mounts, not through a runner-side path"
    ),
    "logs_host_root": (
        "per-trial log directories are created inside the staging dir "
        "(`_logs/verifier`, `_logs/agent`) and bind-mounted into the "
        "agent service via relative volumes; no host-daemon path is required"
    ),
}


class TerminalBenchAdapter(BaseAdapter):
    """Adapter that runs terminal-bench tasks through
    :class:`~tolokaforge.core.per_trial_runtime.PerTrialRuntimeBackend`.

    Carries no coding-harness mode state: a run driven by a vendor CLI is
    applied by the orchestrator's selected
    :class:`~tolokaforge.core.agent_driver.AgentDriver` around this
    adapter's plain output — see :meth:`stage_task`.
    """

    def __init__(self, params: dict[str, Any]):
        for removed, replacement in _REMOVED_PARAMS.items():
            if removed in params:
                raise ValueError(
                    f"terminal-bench adapter: param {removed!r} was removed — {replacement}."
                )
        super().__init__(params)
        first_pack = self.task_packs[0] if self.task_packs else None
        first_pack_str = str(first_pack) if first_pack else None

        self.terminal_bench_dir = Path(params.get("terminal_bench_dir") or first_pack_str or ".")
        self.image_registry: str | None = params.get("image_registry")
        self.image_tag: str = params.get("image_tag", "local")
        self.task_id_filter: list[str] | None = params.get("task_ids")
        self.network_policy = NetworkPolicy(
            params.get("network_policy", NetworkPolicy.FULL_INTERNET.value)
        )
        self.prebuild_images: bool = params.get("prebuild_images", True)
        staging_root = params.get("staging_root")
        self.staging_root: Path = (
            Path(staging_root).expanduser().resolve()
            if staging_root
            else Path(tempfile.gettempdir()) / "tolokaforge-tbench"
        )

        self._tasks: dict[str, TerminalBenchTask] = {}
        self._environments: dict[str, MaterialisedEnvironment] = {}

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

    def _environment(self, task_id: str) -> MaterialisedEnvironment:
        """Materialise this task's environment once and cache it.

        Both :meth:`get_task` and :meth:`to_task_description` route through
        here, so the two surfaces describe the exact same staging path and
        agent service — no divergence is possible.
        """
        cached = self._environments.get(task_id)
        if cached is not None:
            return cached
        self._ensure_discovered()
        env = materialise_task_environment(
            self._tasks[task_id],
            staging_root=self.staging_root,
            image_registry=self.image_registry,
            image_tag=self.image_tag,
        )
        self._environments[task_id] = env
        return env

    # -- Docker stack requirements -------------------------------------------

    def docker_stack_requirements(self) -> DockerStackRequirements:
        """Declare the per-task agent image the orchestrator builds once per run.

        A coding-harness driver declares its own layered-image build via its
        ``apply_container_layers`` seam; this only ever declares the pack's
        own build.

        Skipped under ``prebuild_images: false``, for callers pre-warming
        images themselves.
        """
        if not self.prebuild_images:
            return DockerStackRequirements()
        builds = []
        for task_id in self.get_task_ids():
            env = self._environment(task_id)
            builds.append(
                ComposeImageBuild(compose_file=env.compose_file, service=env.agent_service)
            )
        return DockerStackRequirements(image_builds=builds)

    def stage_task(self, task_id: str) -> StagedTask:
        """Hand a coding-harness driver the per-task staging root it layers onto.

        Terminal-bench tasks always ship a compose file, so this never
        returns ``None`` — every task stages.
        """
        self._ensure_discovered()
        env = self._environment(task_id)
        return StagedTask(
            task_id=task_id,
            staging_dir=env.staging_dir,
            compose_file=env.compose_file,
            agent_service=env.agent_service,
            base_image=agent_image_tag(task_id, self.image_registry, self.image_tag),
            base_build_service=f"{env.agent_service}{HARNESS_BASE_SERVICE_SUFFIX}",
            compose_project_prefix=PROJECT_PREFIX,
        )

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
            initial_user_message=meta.instruction if meta.instruction.strip() else None,
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(
                agent={"enabled": ["bash"]},
                user={"enabled": []},
            ),
            grading="__adapter__",
            system_prompt="__adapter__",
            environment_manifest=self._environment_patch(task_id),
            adapter_settings={
                "difficulty": meta.difficulty,
                "tags": meta.tags,
            },
        )

    def _environment_patch(self, task_id: str) -> EnvironmentPatch:
        env = self._environment(task_id)
        return EnvironmentPatch(
            stack=StackPatch(compose_file=env.compose_file, runner_service="runner"),
            network_policy=self.network_policy,
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
        env = self._environment(task_id)
        manifest = resolve_environment_patch(None, self._environment_patch(task_id))

        return TaskDescription(
            task_id=task_id,
            name=task_id,
            category="terminal",
            description=meta.instruction[:500] if meta.instruction else task_id,
            adapter_type=AdapterType.TERMINAL_BENCH,
            system_prompt=self.get_system_prompt(task_id),
            environment_manifest=manifest,
            agent_tools=[
                ToolSchema(
                    name="bash",
                    description="Execute a bash command inside the task container",
                    parameters={
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Shell command to run"}
                        },
                        "required": ["command"],
                    },
                    category="compute",
                    timeout_s=_AGENT_TOOL_TIMEOUT_S,
                    source=ToolSource(
                        toolset="terminal_bench",
                        module_path="",
                        class_name="bash",
                        invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                        extra={
                            "service": env.agent_service,
                            "compose_project_prefix": PROJECT_PREFIX,
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
                grading_method="test_execution",
            ),
            metadata=self._metadata(meta),
        )

    def _metadata(self, meta: TerminalBenchTask) -> dict[str, Any]:
        """Adapter extras on the runner-side task projection.

        ``initial_user_message`` and ``agent_visible_dir`` are mode-agnostic:
        a run driven by a vendor coding-harness CLI reads them back through
        the orchestrator's selected
        :class:`~tolokaforge.core.agent_driver.AgentDriver` to build the
        per-task command and locate the CLI's edits, respectively. The engine
        loop ignores both.
        """
        return {
            "difficulty": meta.difficulty,
            "tags": meta.tags,
            "verifier_timeout_sec": meta.verifier_timeout_sec,
            "initial_user_message": meta.instruction or "",
            # Terminal-bench packs conventionally set ``WORKDIR /app`` in
            # their base image; the harness-install layer does not override
            # it.
            "agent_visible_dir": "/app",
        }

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
