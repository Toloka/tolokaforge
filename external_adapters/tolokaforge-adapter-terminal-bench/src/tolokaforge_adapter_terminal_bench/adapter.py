"""Terminal-bench adapter for tolokaforge.

Emits an :class:`~tolokaforge.runner.models.EnvironmentPatch` on every
``TaskConfig`` and the resolved :class:`~tolokaforge.runner.models.EnvironmentManifest`
on every ``TaskDescription``. The synthesised compose file lives in a
staging directory materialised by
:mod:`tolokaforge_adapter_terminal_bench.compose_synthesis`; the
orchestrator's per-trial runtime brings the stack up and the runner-side
bash tool only ``docker exec``s into the already-running agent container.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tolokaforge_coding_harnesses.adapter_support import CodingHarnessAdapterMixin

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
    AdapterType,
    EnvironmentPatch,
    NetworkPolicy,
    RunnerGradingConfig,
    RunnerInitialStateConfig,
    RunnerUserSimulatorConfig,
    StackPatch,
    TaskDescription,
    ToolSchema,
)
from tolokaforge.secrets import expand_secret_refs, get_default
from tolokaforge_adapter_terminal_bench.compose_synthesis import (
    DEFAULT_SKILL_DELIVERY,
    PROJECT_PREFIX,
    MaterialisedEnvironment,
    installable_skills_dir,
    materialise_task_environment,
    skills_bundle_digest,
)
from tolokaforge_adapter_terminal_bench.task_parser import (
    TerminalBenchTask,
    discover_tasks,
)
from tolokaforge_coding_harnesses import (
    DEFAULT_PATH_RESOLVER,
    ENGINE_LOOP,
    HarnessSpec,
    PathResolver,
    ResolvedHarnessRegistry,
    SkillDelivery,
    compute_harness_fingerprint,
    provider_env_input,
    resolve_effective_registry,
    validate_harness,
    validate_provider_env_keys,
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


def _resolve_provider_env(
    shipped: dict[str, str], declared: dict[str, str], agent_harness: str
) -> dict[str, str]:
    """Effective provider envelope: *shipped* defaults, *declared* winning.

    A run config that names no ``agent_provider_env`` gets the harness's own
    :attr:`HarnessSpec.provider_env` — the CLI reaches its provider without the
    operator re-deriving an envelope the harness already knows. Declaring one
    key (a different endpoint, a different vault name) keeps the rest of the
    shipped envelope rather than replacing it.

    Values go through :func:`~tolokaforge.secrets.expand_secret_refs`, so a run
    config names a credential as ``${secret:NAME}`` instead of carrying it
    literally. The empty-envelope early return is what keeps the engine loop's
    canonical and ``--dry-run`` paths from constructing a ``SecretManager`` at
    all — ``get_default()`` would otherwise lazily build one to resolve nothing.
    """
    effective = shipped | declared
    if not effective:
        return {}
    validate_provider_env_keys(effective)
    secrets = get_default()
    resolved = {
        key: expand_secret_refs(
            value,
            secrets,
            where=(
                f"terminal-bench adapter_params.agent_provider_env[{key!r}]"
                if key in declared
                else f"terminal-bench harness {agent_harness!r} provider_env[{key!r}]"
            ),
        )
        for key, value in effective.items()
    }
    # Each value is written as one ``KEY=value`` line in the per-trial compose
    # ``.env``. A newline splits the line and turns the remainder into a
    # variable of its own; a ``$`` starts a compose interpolation and the value
    # is truncated there. Either way the container gets a mangled credential
    # and the CLI fails with a provider auth error many layers from the cause,
    # so refuse here, where the offending key can be named.
    unrepresentable = sorted(
        key for key, value in resolved.items() if any(char in value for char in ("\n", "\r", "$"))
    )
    if unrepresentable:
        raise ValueError(
            f"terminal-bench adapter: provider env value(s) for {unrepresentable!r} "
            "contain a newline or a `$`; each value becomes one line of the per-trial "
            "compose `.env`, where a newline splits the line and a `$` starts an "
            "interpolation. Neither survives intact."
        )
    return resolved


class TerminalBenchAdapter(CodingHarnessAdapterMixin, BaseAdapter):
    """Adapter that runs terminal-bench tasks through
    :class:`~tolokaforge.core.per_trial_runtime.PerTrialRuntimeBackend`.

    Inheriting :class:`CodingHarnessAdapterMixin` opts this adapter into the
    orchestrator's ``models.agent.harness`` config gate (via the mixin's
    ``supports_coding_harness = True`` flag) and gives it the shared helpers
    for command assembly, metadata emission, tool-schema payload, and
    ``test_execution`` grading — leaving only the terminal-bench-specific
    compose synthesis in this adapter.
    """

    def __init__(
        self,
        params: dict[str, Any],
        *,
        path_resolver: PathResolver | None = None,
        skill_delivery: SkillDelivery | None = None,
    ):
        for removed, replacement in _REMOVED_PARAMS.items():
            if removed in params:
                raise ValueError(
                    f"terminal-bench adapter: param {removed!r} was removed — {replacement}."
                )
        super().__init__(params)
        # Construction seams, not run-config keys: which filesystem the CLI
        # lands on and how a skills bundle gets there are properties of the
        # runtime driving this adapter, and `get_adapter` builds every adapter
        # as `AdapterClass(params)`.
        self.path_resolver: PathResolver = (
            DEFAULT_PATH_RESOLVER if path_resolver is None else path_resolver
        )
        self.skill_delivery: SkillDelivery = (
            DEFAULT_SKILL_DELIVERY if skill_delivery is None else skill_delivery
        )
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
        self._resolved_registry: ResolvedHarnessRegistry = resolve_effective_registry(
            params.get("harness_presets_file"),
            discover_plugins=not params.get("disable_harness_plugins", False),
        )
        self.harnesses: Mapping[str, HarnessSpec] = self._resolved_registry.harnesses
        self.agent_harness: str = validate_harness(
            params.get("agent_harness", ENGINE_LOOP), self.harnesses
        )
        # Empty under the engine loop, which never reads it: the run config's
        # model reaches litellm through the engine's own LLM layer there.
        self.agent_model: str = params.get("agent_model") or ""
        if self.agent_harness != ENGINE_LOOP and not self.agent_model:
            raise ValueError(
                f"terminal-bench adapter: agent_harness {self.agent_harness!r} requires "
                "`agent_model` — the CLI selects its own default otherwise, so the run "
                "config's model would not be the one measured."
            )
        self.agent_provider_env: dict[str, str] = _resolve_provider_env(
            self.harness_spec.provider_env if self.harness_spec else {},
            params.get("agent_provider_env") or {},
            self.agent_harness,
        )
        staging_root = params.get("staging_root")
        self.staging_root: Path = (
            Path(staging_root).expanduser().resolve()
            if staging_root
            else Path(tempfile.gettempdir()) / "tolokaforge-tbench"
        )

        self._tasks: dict[str, TerminalBenchTask] = {}
        self._environments: dict[str, MaterialisedEnvironment] = {}

    @property
    def harness_spec(self) -> HarnessSpec | None:
        """This run's harness spec. ``None`` under the engine loop, which runs no CLI."""
        return self.harnesses.get(self.agent_harness)

    def fingerprint(self) -> dict[str, Any]:
        """The harness registry this run resolved, under a ``harness`` namespace."""
        return {
            "harness": compute_harness_fingerprint(
                self._resolved_registry, self.agent_harness
            ).model_dump(mode="json")
        }

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
            agent_harness=self.agent_harness,
            harness_registry=self.harnesses,
            provider_env_keys=sorted(self.agent_provider_env),
            path_resolver=self.path_resolver,
            skill_delivery=self.skill_delivery,
        )
        self._environments[task_id] = env
        return env

    # -- Docker stack requirements -------------------------------------------

    def docker_stack_requirements(self) -> DockerStackRequirements:
        """Declare the per-task agent images the orchestrator builds once per run.

        A harness-layered task contributes two entries, base before layer —
        the layer's Dockerfile is ``FROM`` the base image, and the orchestrator
        builds the list in order.

        Skipped under ``prebuild_images: false``, for callers pre-warming
        images themselves.
        """
        if not self.prebuild_images:
            return DockerStackRequirements()
        builds = []
        for task_id in self.get_task_ids():
            env = self._environment(task_id)
            if env.base_build_service is not None:
                builds.append(
                    ComposeImageBuild(compose_file=env.compose_file, service=env.base_build_service)
                )
            builds.append(
                ComposeImageBuild(compose_file=env.compose_file, service=env.agent_service)
            )
        return DockerStackRequirements(image_builds=builds)

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
        """Two-stack composition plan (ADR-0044 § 5 ``MULTI_SCOPE``).

        The ``engine`` stack (run-scope) owns the runner + db-service — one
        substrate held live for every trial in the run. The ``task`` stack
        (trial-scope) owns the agent service and any task-authored siblings
        — materialised fresh per trial so state never leaks across trials.
        Only the engine stack sets ``runner_service`` (INV-12).
        """
        env = self._environment(task_id)
        return EnvironmentPatch(
            stacks={
                "engine": StackPatch(
                    compose_file=env.engine_compose_file,
                    stack_scope="run",
                    runner_service="runner",
                ),
                "task": StackPatch(
                    compose_file=env.compose_file,
                    stack_scope="trial",
                    inputs={
                        provider_env_input(key): value
                        for key, value in self.agent_provider_env.items()
                    },
                ),
            },
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
                    **self.emit_harness_tool_schema(
                        service=env.agent_service,
                        compose_project_prefix=PROJECT_PREFIX,
                        # The runner-side compose-exec wrapper reads its subprocess
                        # timeout off this field, so under harness mode it has to
                        # carry the whole trial's agent budget: the CLI runs to
                        # completion inside a single exec.
                        timeout_s=(
                            _AGENT_TOOL_TIMEOUT_S
                            if self.agent_harness == ENGINE_LOOP
                            else meta.agent_timeout_sec
                        ),
                        toolset="terminal_bench",
                    )
                )
            ],
            user_tools=[],
            initial_state=RunnerInitialStateConfig(),
            user_simulator=RunnerUserSimulatorConfig(mode="scripted"),
            grading=RunnerGradingConfig(**self.emit_test_execution_grading()),
            metadata=self._metadata(meta),
        )

    def _metadata(self, meta: TerminalBenchTask) -> dict[str, Any]:
        """Adapter extras on the runner-side task projection.

        ``agent_harness_command`` is the whole of what the engine core needs to
        know about a coding-harness CLI: present means run this command once in
        place of the LLM turn loop, absent means run the loop. The CLI's name
        and argv stay inside this adapter.

        ``harness_skills_bundle_sha`` appears only when a skills bundle reached
        the image — the task shipped one and the harness had somewhere to put
        it. Absent therefore reads as "this agent had no skills", which a
        bundle-shaped placeholder value could not say.
        """
        metadata: dict[str, Any] = {
            "difficulty": meta.difficulty,
            "tags": meta.tags,
            "verifier_timeout_sec": meta.verifier_timeout_sec,
            "agent_harness": self.agent_harness,
        }
        if self.harness_spec is not None:
            command = self.build_harness_command(
                self.agent_harness,
                self.harness_spec,
                meta.instruction,
                self.agent_model,
                self.agent_provider_env,
                path_resolver=self.path_resolver,
            )
            metadata.update(
                self.emit_harness_metadata(
                    self.agent_harness, self.harness_spec, command, self.agent_model
                )
            )
            # Where the CLI edits inside the trial container — the runner
            # reads this back for state-checks grading. Terminal-bench packs
            # conventionally set ``WORKDIR /app`` in their base image and the
            # harness-install layer does not override it. Only read when a
            # trial requests non-``test_execution`` grading; the shipped
            # terminal-bench harness path stays on ``test_execution`` and
            # bypasses the read.
            metadata["agent_visible_dir"] = "/app"
            skills_dir = installable_skills_dir(meta, self.harness_spec)
            if skills_dir is not None:
                metadata["harness_skills_bundle_sha"] = skills_bundle_digest(
                    meta.task_dir, skills_dir
                )
        return metadata

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
