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
from typing import Any, ClassVar

from tolokaforge.adapters.base import (
    AdapterEnvironment,
    BaseAdapter,
    ComposeImageBuild,
    DockerStackRequirements,
)
from tolokaforge.core.agent_prompt_contract import CONTRACTS
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
from tolokaforge.tools.builtin import registry as builtin_registry
from tolokaforge.tools.builtin.submit import SUBMIT_TOOL_NAME
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
from tolokaforge_coding_harnesses.adapter_support import CodingHarnessAdapterMixin

_AGENT_TOOL_TIMEOUT_S = 120.0

AGENT_TOOL_BASH = "bash"
"""One ``docker exec`` per call: no cwd, environment or shell state survives it."""

AGENT_TOOL_BASH_SESSION = "bash_session"
"""One held ``docker exec`` bash session for the trial: state survives every call."""

AGENT_TOOL_BASH_BATCH = "bash_batch"
"""An array of commands per call, each its own ``docker exec``: no state survives."""

AGENT_TOOLS: tuple[str, ...] = (
    AGENT_TOOL_BASH,
    AGENT_TOOL_BASH_SESSION,
    AGENT_TOOL_BASH_BATCH,
)
"""Values ``adapter_params.agent_tool`` accepts, default first."""

_INTERACTION_MODES: frozenset[str] = frozenset({"conversational", "agent_only"})
"""Values ``adapter_params.interaction_mode`` accepts.

Mirrors ``TaskConfig.interaction_mode`` rather than re-deriving it: a mode the
engine grows reaches a run here by being added to this set.
"""

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


def _looks_like_a_bare_name(selector: str) -> bool:
    """Whether *selector* names a shipped contract rather than a file.

    Mirrors :func:`~tolokaforge.core.agent_prompt_contract.resolve_agent_prompt_contract`,
    which tries the registry first and falls back to a path relative to the
    task's own directory.
    """
    return "/" not in selector and not selector.endswith((".md", ".txt"))


class TerminalBenchAdapter(CodingHarnessAdapterMixin, BaseAdapter):
    """Adapter that runs terminal-bench tasks through
    :class:`~tolokaforge.core.per_trial_runtime.PerTrialRuntimeBackend`.

    Inheriting :class:`CodingHarnessAdapterMixin` opts this adapter into the
    orchestrator's ``models.agent.harness`` config gate (via the mixin's
    ``supports_coding_harness = True`` flag) and gives it the shared helpers
    — leaving only the terminal-bench-specific compose synthesis in this
    adapter.
    """

    requires_docker_cli_in_runner: ClassVar[bool] = True
    """Runner runs docker CLI + compose plugin against the host daemon via the mounted socket."""

    def preferred_grader_kind(self) -> str:
        """Grades via ``test_execution`` on both branches.

        The pack's verifier writes ``/logs/verifier/reward.txt`` regardless of
        whether the CLI or the engine loop drove the trial, so
        :meth:`to_task_description` calls
        :meth:`~tolokaforge_coding_harnesses.adapter_support.CodingHarnessAdapterMixin.emit_test_execution_grading`
        unconditionally. The mixin's harness-aware default would report
        ``"composite"`` under :data:`~tolokaforge_coding_harnesses.ENGINE_LOOP`;
        this override keeps the two answers in agreement on both branches."""
        return "test_execution"

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
        # Read once at construction: a missing file is a config error and should
        # surface before any container is built, not once per task lookup.
        prompt_file = params.get("agent_system_prompt_file")
        self._agent_system_prompt: str | None = None
        if prompt_file:
            prompt_path = Path(prompt_file)
            if not prompt_path.is_file():
                raise ValueError(
                    f"terminal-bench adapter: agent_system_prompt_file "
                    f"{prompt_file!r} does not exist (resolved to "
                    f"{prompt_path.resolve()})"
                )
            self._agent_system_prompt = prompt_path.read_text()
            if not self._agent_system_prompt.strip():
                raise ValueError(
                    f"terminal-bench adapter: agent_system_prompt_file "
                    f"{prompt_file!r} is empty — omit the key to use the default"
                )
        # Names a reply contract the engine resolves, rather than a prompt this
        # adapter writes. Set, the adapter supplies no prompt of its own and the
        # engine composes one; unset, the adapter's own terminal-bench prompt
        # applies, which is what keeps scores comparable with the benchmark. An
        # explicit ``agent_system_prompt_file`` wins over both, for byte-exact
        # replay. A model preset's ``default_agent_prompt_contract`` therefore
        # does not reach a terminal-bench task either way — changing that would
        # change what every task in the corpus is scored against.
        self._agent_prompt_contract: str | None = params.get("agent_prompt_contract")
        if self._agent_prompt_contract and _looks_like_a_bare_name(self._agent_prompt_contract):
            # Checked here rather than at the first trial's prompt build, which
            # happens after the run has provisioned images. Only bare names:
            # a path-shaped selector resolves against each task's own directory,
            # which this object does not know yet.
            if self._agent_prompt_contract not in CONTRACTS:
                known = ", ".join(sorted(CONTRACTS))
                raise ValueError(
                    f"terminal-bench adapter: unknown agent_prompt_contract "
                    f"{self._agent_prompt_contract!r} — shipped contracts are {known}, "
                    f"or give a path to a contract file beside the tasks"
                )
        if self._agent_prompt_contract and self._agent_system_prompt is not None:
            raise ValueError(
                "terminal-bench adapter: set agent_system_prompt_file or "
                "agent_prompt_contract, not both — the first supplies the whole "
                "prompt verbatim and the second asks the engine to compose one"
            )
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
        self.agent_tool: str = params.get("agent_tool", AGENT_TOOL_BASH)
        if self.agent_tool not in AGENT_TOOLS:
            raise ValueError(
                f"terminal-bench adapter: agent_tool {self.agent_tool!r} is not one of "
                f"{list(AGENT_TOOLS)!r}."
            )
        if self.agent_tool != AGENT_TOOL_BASH and self.agent_harness != ENGINE_LOOP:
            raise ValueError(
                f"terminal-bench adapter: agent_tool {self.agent_tool!r} requires "
                f"agent_harness {ENGINE_LOOP!r} — under a coding-harness CLI the engine "
                "runs no turn loop and the whole trial is one tool call, so neither a "
                "session-lifetime shell nor a per-turn command array has anything to act on."
            )
        self.agent_completion_tool: bool = bool(params.get("agent_completion_tool", False))
        if self.agent_completion_tool and self.agent_harness != ENGINE_LOOP:
            raise ValueError(
                f"terminal-bench adapter: agent_completion_tool requires agent_harness "
                f"{ENGINE_LOOP!r} — a coding-harness CLI ends its own trial when the "
                "process exits, so there is no turn loop for a completion signal to end."
            )
        self.interaction_mode: str = str(params.get("interaction_mode", "conversational"))
        if self.interaction_mode not in _INTERACTION_MODES:
            raise ValueError(
                f"terminal-bench adapter: interaction_mode "
                f"{self.interaction_mode!r} is not one of "
                f"{', '.join(sorted(_INTERACTION_MODES))}."
            )
        if self.interaction_mode == "agent_only" and not (
            self._agent_prompt_contract or self._agent_system_prompt
        ):
            raise ValueError(
                "terminal-bench adapter: interaction_mode 'agent_only' needs a prompt "
                "that tells the agent how the episode ends — set agent_prompt_contract "
                "(or agent_system_prompt_file). Under this mode a turn carrying no tool "
                "call ends the trial at whatever index it happens on, turn 1 included, "
                "graded against an untouched container; the adapter's own default prompt "
                "says nothing about that, so a model that opens with a plan scores zero "
                "and the bundle looks like a completed trial."
            )
        if self.interaction_mode == "agent_only" and self.agent_harness != ENGINE_LOOP:
            raise ValueError(
                f"terminal-bench adapter: interaction_mode 'agent_only' requires "
                f"agent_harness {ENGINE_LOOP!r} — under a coding-harness CLI the "
                "engine runs no turn loop, so there is no user turn to suppress."
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
            adapter_type="terminal_bench",
            initial_user_message=meta.instruction if meta.instruction.strip() else None,
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(
                agent=self.agent_tool_block(task_id),
                user={"enabled": []},
            ),
            grading="__adapter__",
            policies=self._agent_policies(task_id),
            agent_prompt_contract=self._agent_prompt_contract,
            interaction_mode=self.interaction_mode,  # type: ignore[arg-type]
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

    def _agent_tool_timeout_s(self, task_id: str) -> float:
        """Subprocess budget the runner-side wrapper enforces per call.

        Under the engine loop a call is one agent command, so the budget is the
        fixed per-command ceiling. Under a coding-harness CLI the whole trial
        runs inside a single ``exec``, so the budget is the trial's agent
        timeout.
        """
        if self.agent_harness == ENGINE_LOOP:
            return _AGENT_TOOL_TIMEOUT_S
        self._ensure_discovered()
        return self._tasks[task_id].agent_timeout_sec

    def _persistent_shell_tool_config(self, task_id: str) -> dict[str, Any]:
        """``tool_config`` the persistent shell's compose backend reads.

        ``service`` selects that backend over the local subprocess one, and
        ``compose_project_prefix`` is what resolves the per-trial container
        name — the same two values the one-shot tool carries on
        ``ToolSource.extra``, so both tools exec into the same container.
        """
        return {
            "service": self._environment(task_id).agent_service,
            "compose_project_prefix": PROJECT_PREFIX,
            "timeout_s": self._agent_tool_timeout_s(task_id),
        }

    def agent_tool_block(self, task_id: str) -> dict[str, Any]:
        """The ``tools.agent`` block naming this run's agent tools.

        Paired with :meth:`agent_tool_schemas` — the names enabled here are the
        names of the schemas emitted there, and the persistent shell's per-tool
        kwargs are the same dict its schema carries as ``tool_config``.

        The shell is always enabled. ``agent_completion_tool`` adds the
        completion tool beside it, which is the whole of what that param does.
        """
        if self.agent_tool == AGENT_TOOL_BASH:
            block: dict[str, Any] = {"enabled": [AGENT_TOOL_BASH]}
        elif self.agent_tool == AGENT_TOOL_BASH_BATCH:
            block = {"enabled": [AGENT_TOOL_BASH_BATCH]}
        else:
            block = {
                "enabled": [AGENT_TOOL_BASH_SESSION],
                AGENT_TOOL_BASH_SESSION: self._persistent_shell_tool_config(task_id),
            }
        if self.agent_completion_tool:
            block["enabled"] = [*block["enabled"], SUBMIT_TOOL_NAME]
        return block

    def agent_tool_schemas(self, task_id: str) -> list[ToolSchema]:
        """Every agent tool this run offers, shell first.

        One entry unless ``agent_completion_tool`` is set, which appends the
        completion tool. Nothing downstream of the engine loop requires a
        single agent tool: the ``!= 1`` guard that does live in the conductor
        governs the coding-harness branch, which this param refuses to combine
        with.
        """
        schemas = [self.agent_shell_tool_schema(task_id)]
        if self.agent_completion_tool:
            schemas.append(self.agent_completion_tool_schema())
        return schemas

    def agent_completion_tool_schema(self) -> ToolSchema:
        """The completion tool, as the runner reconstructs it.

        Sourceless like ``bash_session``: the runner's factory resolves the name
        through the builtin registry. Its advertised parameters and description
        come from the registered class, so the only thing the model is ever told
        about this tool is what the tool itself declares — no prompt carries it.
        """
        tool = builtin_registry.get_class(SUBMIT_TOOL_NAME)()
        function = tool.get_schema()["function"]
        return ToolSchema(
            name=SUBMIT_TOOL_NAME,
            description=function["description"],
            parameters=function["parameters"],
            category="compute",
            timeout_s=tool.policy.timeout_s,
        )

    def agent_shell_tool_schema(self, task_id: str) -> ToolSchema:
        """This run's shell tool, as the runner reconstructs it.

        ``bash`` carries a ``docker_compose_exec`` :class:`ToolSource`, which
        the runner's factory routes to its compose-exec wrapper. ``bash_session``
        carries no source at all: the factory dispatches a sourceless tool by
        name through the builtin registry, where the name resolves to the
        persistent shell. Its advertised parameters come from the registered
        tool class rather than a copy, so the schema the model sees cannot
        drift from the one the wrapper implements.
        """
        if self.agent_tool == AGENT_TOOL_BASH_BATCH:
            return ToolSchema(
                **self.emit_harness_batch_tool_schema(
                    service=self._environment(task_id).agent_service,
                    compose_project_prefix=PROJECT_PREFIX,
                    timeout_s=self._agent_tool_timeout_s(task_id),
                    toolset="terminal_bench",
                )
            )
        if self.agent_tool == AGENT_TOOL_BASH:
            return ToolSchema(
                **self.emit_harness_tool_schema(
                    service=self._environment(task_id).agent_service,
                    compose_project_prefix=PROJECT_PREFIX,
                    # The runner-side compose-exec wrapper reads its subprocess
                    # timeout off this field, so under harness mode it has to
                    # carry the whole trial's agent budget: the CLI runs to
                    # completion inside a single exec.
                    timeout_s=self._agent_tool_timeout_s(task_id),
                    toolset="terminal_bench",
                )
            )
        tool_config = self._persistent_shell_tool_config(task_id)
        function = builtin_registry.get_class(AGENT_TOOL_BASH_SESSION)(**tool_config).get_schema()[
            "function"
        ]
        return ToolSchema(
            name=AGENT_TOOL_BASH_SESSION,
            description=function["description"],
            parameters=function["parameters"],
            category="compute",
            timeout_s=tool_config["timeout_s"],
            tool_config=tool_config,
        )

    # -- prompts --------------------------------------------------------------

    def get_system_prompt(self, task_id: str) -> str:
        """The agent's system prompt, or the contents of ``agent_system_prompt_file``.

        The default is deliberately terse: a terminal-bench task carries its own
        instruction, and the prompt's job is to say what the tool is.

        How much a run needs beyond that is a property of the model, not of the
        pack. A model that narrates its reasoning and signs off when it is done
        needs nothing here; one that emits a bare tool call every turn has no
        channel to think in and no way to say it has finished, and the same
        default leaves it circling. The override exists so that difference can
        be measured and carried per run rather than compiled in.
        """
        if self._agent_system_prompt is not None:
            return self._agent_system_prompt
        return (
            "You are an expert developer working inside a Linux container. "
            f"Use the {self.agent_tool} tool to execute commands. "
            "Fix the issues described in the user message."
        )

    def _agent_policies(self, task_id: str) -> dict[str, Any]:
        """The task's ``policies`` block, carrying a prompt only when one was asked for.

        ``build_system_prompt`` returns ``policies["agent_system_prompt"]``
        before it considers anything else, so writing the key decides the
        prompt outright. It is written for a run that supplied a prompt file,
        and for a run that named no contract — the latter is how a
        terminal-bench task keeps the prompt the benchmark scores it against.
        Only an explicit ``agent_prompt_contract`` leaves the key out and lets
        the engine compose one.
        """
        if self._agent_system_prompt is not None:
            return {"agent_system_prompt": self._agent_system_prompt}
        if self._agent_prompt_contract is not None:
            return {}
        return {"agent_system_prompt": self.get_system_prompt(task_id)}

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
            adapter_type=AdapterType.TERMINAL_BENCH,
            system_prompt=self.get_system_prompt(task_id),
            environment_manifest=manifest,
            agent_tools=self.agent_tool_schemas(task_id),
            user_tools=[],
            initial_state=RunnerInitialStateConfig(),
            user_simulator=RunnerUserSimulatorConfig(mode="scripted"),
            grading=RunnerGradingConfig(
                # The task's own ``[verifier] timeout_sec``, in both directions.
                # 613 of the 974 delivered tasks ask for more than the grading
                # kind's 300s default and 360 ask for less — commonly 180s. A
                # task is the authority on how long its own suite needs, and
                # substituting a longer budget would score it under a rule its
                # author did not write. A suite killed by the clock is not
                # silent: it reaches the grade as ``script_exec_error``, so
                # ``grade.yaml`` says the verifier ran out of time rather than
                # that the agent failed.
                **self.emit_test_execution_grading(meta.verifier_timeout_sec)
            ),
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

        :data:`HARNESS_USAGE_LOG_METADATA_KEY` appears only for a harness that
        declares request middleware. That middleware is the proxy every one of
        the CLI's provider requests passes through, and the only thing that
        writes the usage records the key points at — for a harness that boots
        no proxy the key would name a file nothing ever creates. The value is
        the path *inside the trial container*: the synthesised compose mounts
        the log directory from the per-trial context copy, which the stack
        deletes at teardown, so the engine reads the records out of the running
        container instead of off the host.
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
            # Where the CLI edits inside the trial container — the runner
            # reads this back for state-checks grading. Terminal-bench packs
            # conventionally set ``WORKDIR /app`` in their base image and the
            # harness-install layer does not override it. Only read when a
            # trial requests non-``test_execution`` grading; the shipped
            # terminal-bench harness path stays on ``test_execution`` and
            # bypasses the read.
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
            reasons="Terminal-bench grading must run via Runner GradeTrial RPC",
        )
