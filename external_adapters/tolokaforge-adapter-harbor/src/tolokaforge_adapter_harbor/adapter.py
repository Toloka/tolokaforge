"""Harbor (Terminal-Bench 2.0) adapter for tolokaforge.

Harbor tasks are Terminal-Bench 2.0 tasks — ``task.toml`` + ``environment/``
+ ``tests/test.sh`` writing ``/logs/verifier/reward.txt``. This adapter runs
each Harbor task on tolokaforge's own runner by reusing the
terminal-bench adapter's environment synthesis
(:mod:`tolokaforge_adapter_terminal_bench.compose_synthesis`, which already
emits the Harbor compose dialect) and grading its reward through
``test_execution``. It does not nest ``harbor run`` inside the container — the
trial runs on tolokaforge's substrate, and the pack's own verifier is the
authority on the reward.

The adapter is focused on the two ways a trial reaches that verifier: the
engine's own LLM turn loop (``engine-loop``), and delegation to a coding-harness
CLI baked into the task image (``DELEGATED``). Both read the same reward file,
so grading is ``test_execution`` on both branches.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

# Reuse the terminal-bench adapter's environment synthesis + task parsing:
# Harbor tasks are Terminal-Bench 2.0 tasks, so this is a deliberate shared
# surface across the two external adapter packages (approach b, no `harbor run`).
from tolokaforge_adapter_terminal_bench.compose_synthesis import (
    DEFAULT_SKILL_DELIVERY,
    PROJECT_PREFIX,
    MaterialisedEnvironment,
    installable_skills_dir,
    materialise_task_environment,
    skills_bundle_digest,
)
from tolokaforge_coding_harnesses.adapter_support import CodingHarnessAdapterMixin

from tolokaforge.adapters.base import (
    AdapterEnvironment,
    BaseAdapter,
    ComposeImageBuild,
    DockerStackRequirements,
)
from tolokaforge.core.execution_mode import ExecutionMode
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
    RunnerInitialStateConfig,
    RunnerUserSimulatorConfig,
    StackPatch,
    TaskDescription,
    ToolSchema,
)
from tolokaforge.secrets import expand_secret_refs, get_default
from tolokaforge_adapter_harbor.discovery import HarborTask, discover_harbor_tasks
from tolokaforge_coding_harnesses import (
    DEFAULT_PATH_RESOLVER,
    ENGINE_LOOP,
    HARNESS_USAGE_LOG_METADATA_KEY,
    MIDDLEWARE_USAGE_LOG_CONTAINER_PATH,
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

#: Adapter type string this adapter registers under. ``AdapterType`` is an open
#: set (``TaskDescription.adapter_type`` is a free ``str`` sourced from the
#: registry), so a plug-in adapter rides its own name with no engine edit.
ADAPTER_TYPE = "harbor"

#: One ``docker exec`` per agent command under the engine loop: the per-call
#: subprocess ceiling the runner-side bash wrapper enforces. Under a delegated
#: harness the whole trial is one exec, so the budget is the task's own agent
#: timeout instead (see :meth:`HarborAdapter._agent_tool_timeout_s`).
_AGENT_TOOL_TIMEOUT_S = 120.0

AGENT_TOOL_BASH = "bash"


def _resolve_provider_env(
    shipped: dict[str, str], declared: dict[str, str], agent_harness: str
) -> dict[str, str]:
    """Effective provider envelope: *shipped* defaults, *declared* winning.

    Mirrors the terminal-bench adapter. A run config that names no
    ``agent_provider_env`` inherits the harness's own
    :attr:`HarnessSpec.provider_env`; declaring one key overrides just that key
    and keeps the rest. Values go through
    :func:`~tolokaforge.secrets.expand_secret_refs`, so a run config names a
    credential as ``${secret:NAME}`` rather than carrying it literally. The
    empty-envelope early return keeps the engine-loop path from constructing a
    ``SecretManager`` at all.
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
                f"harbor adapter_params.agent_provider_env[{key!r}]"
                if key in declared
                else f"harbor harness {agent_harness!r} provider_env[{key!r}]"
            ),
        )
        for key, value in effective.items()
    }
    # Each value is written as one ``KEY=value`` line in the per-trial compose
    # ``.env``: a newline splits the line and a ``$`` starts a compose
    # interpolation, so either way the container would get a mangled credential
    # and the CLI would fail with a provider auth error far from the cause.
    # Refuse here, where the offending key can be named.
    unrepresentable = sorted(
        key for key, value in resolved.items() if any(char in value for char in ("\n", "\r", "$"))
    )
    if unrepresentable:
        raise ValueError(
            f"harbor adapter: provider env value(s) for {unrepresentable!r} contain a "
            "newline or a `$`; each value becomes one line of the per-trial compose "
            "`.env`, where a newline splits the line and a `$` starts an interpolation. "
            "Neither survives intact."
        )
    return resolved


class HarborAdapter(CodingHarnessAdapterMixin, BaseAdapter):
    """Adapter that runs Harbor (Terminal-Bench 2.0) tasks on tolokaforge's runner.

    Inheriting :class:`CodingHarnessAdapterMixin` opts the adapter into the
    orchestrator's ``models.agent.harness`` config gate and gives it the shared
    command-assembly, metadata, tool-schema and grading helpers — leaving this
    class to wire Harbor discovery and the reused environment synthesis.
    """

    requires_docker_cli_in_runner: ClassVar[bool] = True
    """Runner runs the docker CLI against the host daemon via the mounted socket
    — the bash tool ``docker exec``\\ s into the sibling task container."""

    supported_execution_modes: ClassVar[frozenset[ExecutionMode]] = frozenset(
        {ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED}
    )
    """Harbor runs the engine loop and can also delegate to a coding-harness CLI."""

    def preferred_grader_kind(self) -> str:
        """Grades via ``test_execution`` on both branches.

        The pack's verifier writes ``/logs/verifier/reward.txt`` whether the CLI
        or the engine loop drove the trial, so :meth:`to_task_description` emits
        ``test_execution`` unconditionally. This override keeps the declared kind
        in agreement with the emitted ``grading_method`` under the engine loop,
        where the mixin's default would otherwise report ``"composite"``.
        """
        return "test_execution"

    def __init__(
        self,
        params: dict[str, Any],
        *,
        path_resolver: PathResolver | None = None,
        skill_delivery: SkillDelivery | None = None,
    ):
        super().__init__(params)
        # Construction seams, not run-config keys: which filesystem the CLI lands
        # on and how a skills bundle gets there are properties of the runtime
        # driving this adapter, and ``get_adapter`` builds every adapter as
        # ``AdapterClass(params)``.
        self.path_resolver: PathResolver = (
            DEFAULT_PATH_RESOLVER if path_resolver is None else path_resolver
        )
        self.skill_delivery: SkillDelivery = (
            DEFAULT_SKILL_DELIVERY if skill_delivery is None else skill_delivery
        )
        first_pack = self.task_packs[0] if self.task_packs else None
        first_pack_str = str(first_pack) if first_pack else None
        self.harbor_tasks_dir = Path(params.get("harbor_tasks_dir") or first_pack_str or ".")
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
                f"harbor adapter: agent_harness {self.agent_harness!r} requires "
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
            else Path(tempfile.gettempdir()) / "tolokaforge-harbor"
        )

        self._tasks: dict[str, HarborTask] = {}
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
            self._tasks = discover_harbor_tasks(self.harbor_tasks_dir)

    def get_task_dir(self, task_id: str) -> Path:
        self._ensure_discovered()
        return self._tasks[task_id].task_dir

    def _environment(self, task_id: str) -> MaterialisedEnvironment:
        """Materialise this task's environment once and cache it.

        Both :meth:`get_task` and :meth:`to_task_description` route through here,
        so the two surfaces describe the exact same staging path and agent
        service — no divergence is possible.
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

        A harness-layered task contributes two entries, base before layer — the
        layer's Dockerfile is ``FROM`` the base image, and the orchestrator
        builds the list in order. Skipped under ``prebuild_images: false``, for
        callers pre-warming images themselves.
        """
        if not self.prebuild_images:
            return DockerStackRequirements()
        builds = []
        for task_id in self.get_task_ids():
            env = self._environment(task_id)
            if env.base_build_service is not None:
                builds.append(
                    ComposeImageBuild(
                        compose_file=env.compose_file,
                        service=env.base_build_service,
                        expected_image_ref=env.base_image,
                    )
                )
            builds.append(
                ComposeImageBuild(
                    compose_file=env.compose_file,
                    service=env.agent_service,
                    expected_image_ref=env.agent_image,
                )
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
            adapter_type=ADAPTER_TYPE,
            initial_user_message=meta.instruction if meta.instruction.strip() else None,
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(
                agent={"enabled": [AGENT_TOOL_BASH]},
                user={"enabled": []},
            ),
            grading="__adapter__",
            policies=self._agent_policies(task_id),
            environment_manifest=self._environment_patch(task_id),
            adapter_settings={
                "difficulty": meta.difficulty,
                "tags": meta.tags,
            },
        )

    def _agent_policies(self, task_id: str) -> dict[str, Any]:
        """The task's ``policies`` block.

        Under the engine loop the adapter supplies its own prompt so the agent
        is told what the tool is; under a delegated harness the engine runs no
        turn loop, so no prompt is composed and the key is left out.
        """
        if self.agent_harness == ENGINE_LOOP:
            return {"agent_system_prompt": self.get_system_prompt(task_id)}
        return {}

    def _environment_patch(self, task_id: str) -> EnvironmentPatch:
        """Two-stack composition plan (ADR-0044 § 5 ``MULTI_SCOPE``).

        The ``engine`` stack (run-scope) owns the runner + db-service — one
        substrate held live for every trial in the run. The ``task`` stack
        (trial-scope) owns the agent service and any task-authored siblings —
        materialised fresh per trial so state never leaks across trials. Only the
        engine stack sets ``runner_service`` (INV-12).
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

    def get_tools(self, task_id: str) -> list[Any]:
        return []

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Any]:
        return []

    # -- tools ----------------------------------------------------------------

    def _agent_tool_timeout_s(self, task_id: str) -> float:
        """Subprocess budget the runner-side wrapper enforces per call.

        Under the engine loop a call is one agent command, so the budget is the
        fixed per-command ceiling. Under a delegated harness the whole trial runs
        inside a single ``exec``, so the budget is the task's agent timeout.
        """
        if self.agent_harness == ENGINE_LOOP:
            return _AGENT_TOOL_TIMEOUT_S
        self._ensure_discovered()
        return self._tasks[task_id].agent_timeout_sec

    def agent_tool_schemas(self, task_id: str) -> list[ToolSchema]:
        """The single ``bash`` tool, as the runner reconstructs it.

        Carries a ``docker_compose_exec`` source whose ``service`` +
        ``compose_project_prefix`` resolve the per-trial container the runner
        execs into. The prefix is :data:`PROJECT_PREFIX` from the reused
        synthesis module, so it matches the container names that module stamps.
        """
        return [
            ToolSchema(
                **self.emit_harness_tool_schema(
                    service=self._environment(task_id).agent_service,
                    compose_project_prefix=PROJECT_PREFIX,
                    timeout_s=self._agent_tool_timeout_s(task_id),
                    toolset=ADAPTER_TYPE,
                )
            )
        ]

    # -- prompts --------------------------------------------------------------

    def get_system_prompt(self, task_id: str) -> str:
        """The agent's system prompt under the engine loop.

        Deliberately terse: a Harbor task carries its own instruction, and the
        prompt's job is to say what the tool is.
        """
        return (
            "You are an expert developer working inside a Linux container. "
            f"Use the {AGENT_TOOL_BASH} tool to execute commands. "
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
        manifest = resolve_environment_patch(None, self._environment_patch(task_id))

        return TaskDescription(
            task_id=task_id,
            name=task_id,
            category="terminal",
            description=meta.instruction[:500] if meta.instruction else task_id,
            adapter_type=ADAPTER_TYPE,
            system_prompt=self.get_system_prompt(task_id),
            environment_manifest=manifest,
            agent_tools=self.agent_tool_schemas(task_id),
            user_tools=[],
            initial_state=RunnerInitialStateConfig(),
            user_simulator=RunnerUserSimulatorConfig(mode="scripted"),
            grading=RunnerGradingConfig(
                # The task's own ``[verifier] timeout_sec`` in both directions.
                # A task is the authority on how long its own suite needs; a
                # suite killed by the clock reaches the grade as
                # ``script_exec_error`` rather than as an agent failure.
                **self.emit_test_execution_grading(meta.verifier_timeout_sec)
            ),
            metadata=self._metadata(meta),
        )

    def _metadata(self, meta: HarborTask) -> dict[str, Any]:
        """Adapter extras on the runner-side task projection.

        ``agent_harness_command`` is the whole of what the engine core needs to
        know about a delegated CLI: present means run this command once in place
        of the LLM turn loop, absent means run the loop. The remaining harness
        keys are recorded so replay can reconstruct which CLI + version + model
        produced the trial.
        """
        metadata: dict[str, Any] = {
            "difficulty": meta.difficulty,
            "tags": meta.tags,
            "agent_harness": self.agent_harness,
        }
        if meta.verifier_timeout_sec is not None:
            metadata["verifier_timeout_sec"] = meta.verifier_timeout_sec
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
            # Where the CLI edits inside the trial container — the runner reads
            # this back for state-checks grading. Harbor packs set ``WORKDIR
            # /app`` in their base image and the harness-install layer does not
            # override it. Only read for non-``test_execution`` grading; the
            # shipped Harbor path stays on ``test_execution`` and bypasses it.
            metadata["agent_visible_dir"] = "/app"
            if self.harness_spec.request_middleware is not None:
                metadata[HARNESS_USAGE_LOG_METADATA_KEY] = MIDDLEWARE_USAGE_LOG_CONTAINER_PATH
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
            reasons="Harbor grading must run via Runner GradeTrial RPC",
        )
