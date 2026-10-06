"""Harbor-harness adapter for tolokaforge.

Runs Terminal-Bench 2.0 task packs under ``tolokaforge run`` by delegating
execution to the real Harbor harness inside a trial container — the same
"delegate to the harness's own loop" shape as the inspect_ai adapter. For each
task the adapter synthesises a two-stack compose plan whose agent container has
``harbor`` + the Docker CLI + the task pack baked in, sets the trial's "agent"
step to a single ``harbor run`` command (Harbor runs its own Terminus 2 agent and
verifier), and grades via ``test_execution`` from the reward a generated
``tests/test.sh`` extracts from Harbor's native ``result.json``.

Discovery reads the TB2 on-disk shape (``task.toml`` + ``environment/`` +
``tests/``) through the terminal-bench task parser, so the ``[harbor]`` extra
depends on ``tolokaforge-adapter-terminal-bench`` — never on the ``harbor`` pip
distribution, which is installed only inside the agent image.

Model credentials reach the container through ``tolokaforge.secrets``: values in
``agent_provider_env`` are resolved from ``${secret:NAME}`` refs at load time and
passed as per-trial compose inputs — never read from ``os.environ`` here.
"""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask, discover_tasks

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
    StackPatch,
    TaskDescription,
    ToolSchema,
    ToolSource,
)
from tolokaforge.secrets import expand_secret_refs, get_default
from tolokaforge_adapter_harbor import compose_synthesis as cs

_ADAPTER_TYPE = "harbor"
_DEFAULT_HARBOR_VERSION = "0.23.0"
# Harbor requires Python >=3.12; a standard slim base covers it (no 3.13 needed).
_DEFAULT_BASE_IMAGE = "python:3.12-slim-bookworm"
_DEFAULT_AGENT = "terminus-2"
_ORACLE_AGENT = "oracle"
_DEFAULT_SANDBOX_BACKEND = "docker"
_DEFAULT_AGENT_TIMEOUT_S = 1800.0

# Terminus resolves its model through LiteLLM, which routes an ``openrouter/``
# slug to its OpenRouter handler (reading ``OPENROUTER_API_KEY``); the prefix is
# therefore KEPT. Vendor coding CLIs (claude-code/codex/gemini-cli) are the
# mirror image — they want the prefix stripped and reach OpenRouter through
# per-CLI ``*_BASE_URL`` + token env — and are not wired in v1, so the adapter
# supports only the terminus family plus the keyless ``oracle`` and fails loud
# otherwise rather than emitting an invocation that would silently mis-auth.
_TERMINUS_AGENTS = frozenset({"terminus", "terminus-1", "terminus-2"})
_SUPPORTED_AGENTS = _TERMINUS_AGENTS | {_ORACLE_AGENT}

# Terminus installs tooling (tmux, asciinema) per trial; Harbor's default 360s
# agent-setup budget is routinely too tight for a real run, so the multiplier
# defaults high. Oracle runs no agent setup, so it never receives the flag.
_DEFAULT_AGENT_SETUP_TIMEOUT_MULTIPLIER = 10.0

# One scalar reward per task -> the same test_execution runner dispatch
# terminal-bench and inspect_ai use.
_TEST_EXECUTION_GRADING: dict[str, Any] = {
    "combine_method": "weighted",
    "weights": {"custom_checks": 1.0},
    "pass_threshold": 0.5,
    "grading_method": "test_execution",
}

_SYSTEM_PROMPT = (
    "Executed by the Harbor harness; Harbor's own Terminus 2 agent drives the interaction."
)


class HarborAdapter(BaseAdapter):
    """Runs TB2 tasks by delegating execution to the Harbor harness in a container."""

    requires_docker_cli_in_runner: ClassVar[bool] = True
    """The runner shells out to ``docker exec`` to drive ``harbor run`` in the
    sibling task container, so the runner stack carries the host Docker socket.
    (The agent container carries a second socket mount of its own, for the
    Docker-out-of-Docker ``harbor run`` performs — see :mod:`.compose_synthesis`.)"""

    supported_execution_modes: ClassVar[frozenset[ExecutionMode]] = frozenset(
        {ExecutionMode.DELEGATED}
    )
    """Delegated-only: a trial's agent step is a single ``harbor run`` command the
    adapter emits, never the engine's own turn loop. The run selects the adapter
    with ``models.agent.harness: harbor``."""

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        first_pack = str(self.task_packs[0]) if self.task_packs else None
        self.pack_dir = Path(
            params.get("harbor_tasks_dir") or first_pack or self.base_dir
        ).resolve()
        self.task_id_filter: list[str] | None = _as_list(params.get("task_ids"))
        self.agent: str = params.get("agent") or _DEFAULT_AGENT
        if self.agent not in _SUPPORTED_AGENTS:
            raise ValueError(
                f"harbor adapter: unsupported agent {self.agent!r}. v1 supports the "
                f"terminus family ({', '.join(sorted(_TERMINUS_AGENTS))}) and the keyless "
                "'oracle'. Vendor coding CLIs (claude-code, codex, gemini-cli) need per-CLI "
                "OpenRouter auth that is not wired here yet."
            )
        self.agent_model: str = params.get("agent_model") or ""
        self.sandbox_backend: str = params.get("sandbox_backend") or _DEFAULT_SANDBOX_BACKEND
        self.agent_setup_timeout_multiplier: float = float(
            params.get("agent_setup_timeout_multiplier") or _DEFAULT_AGENT_SETUP_TIMEOUT_MULTIPLIER
        )
        self.harbor_version: str = params.get("harbor_version") or _DEFAULT_HARBOR_VERSION
        self.base_image: str = params.get("base_image") or _DEFAULT_BASE_IMAGE
        self.agent_kwargs: dict[str, str] = _as_str_map(params.get("agent_kwargs") or {})
        self.agent_timeout_override: float | None = (
            float(params["agent_timeout_s"]) if params.get("agent_timeout_s") else None
        )
        self.network_policy = NetworkPolicy(
            params.get("network_policy", NetworkPolicy.FULL_INTERNET.value)
        )
        staging_root = params.get("staging_root")
        self.staging_root = (
            Path(staging_root).expanduser().resolve()
            if staging_root
            else Path(tempfile.gettempdir()) / "tolokaforge-harbor"
        )
        _assert_host_visible_staging_root(self.staging_root)
        self.agent_provider_env = _resolve_provider_env(params.get("agent_provider_env") or {})

        self._tasks: dict[str, TerminalBenchTask] = {}
        self._environments: dict[str, cs.MaterialisedEnvironment] = {}

    # -- discovery -----------------------------------------------------------

    def _ensure_discovered(self) -> None:
        if not self._tasks:
            self._tasks = discover_tasks(self.pack_dir)

    def get_task_ids(self) -> list[str]:
        self._ensure_discovered()
        ids = list(self._tasks)
        if self.task_id_filter:
            ids = [tid for tid in ids if tid in self.task_id_filter]
        return ids

    def get_task_dir(self, task_id: str) -> Path:
        self._ensure_discovered()
        return self._tasks[task_id].task_dir

    def _agent_timeout_s(self, task: TerminalBenchTask) -> float:
        """Per-trial agent budget: the param override if set, else the task's own
        ``[agent].timeout_sec`` from ``task.toml``."""
        if self.agent_timeout_override is not None:
            return self.agent_timeout_override
        return float(task.agent_timeout_sec or _DEFAULT_AGENT_TIMEOUT_S)

    def _environment(self, task_id: str) -> cs.MaterialisedEnvironment:
        cached = self._environments.get(task_id)
        if cached is not None:
            return cached
        env = cs.materialise_task_environment(
            self._tasks[task_id].task_dir,
            task_id,
            staging_root=self.staging_root,
            harbor_version=self.harbor_version,
            base_image=self.base_image,
            provider_env_keys=sorted(self.agent_provider_env),
        )
        self._environments[task_id] = env
        return env

    # -- translation ---------------------------------------------------------

    def get_task(self, task_id: str) -> TaskConfig:
        self._ensure_discovered()
        return TaskConfig(
            task_id=task_id,
            name=task_id,
            category="harbor",
            description=f"Harbor task {task_id}",
            adapter_type=_ADAPTER_TYPE,
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(agent={"enabled": ["bash"]}, user={"enabled": []}),
            grading="__adapter__",
            policies={"agent_system_prompt": self.get_system_prompt(task_id)},
            environment_manifest=self._environment_patch(task_id),
            adapter_settings={"harbor_agent": self.agent, "harbor_model": self.agent_model},
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
                    compose_file=env.engine_compose_file,
                    stack_scope="run",
                    runner_service="runner",
                ),
                "task": StackPatch(
                    compose_file=env.compose_file, stack_scope="trial", inputs=inputs
                ),
            },
            network_policy=self.network_policy,
        )

    def _harbor_command(self, jobs_dir: str) -> str:
        """Assemble the single ``harbor run`` invocation the delegated agent runs.

        ``--jobs-dir`` targets *jobs_dir*, the environment's identity-mounted job
        directory (host path == container path), so Harbor's sandbox can
        bind-mount it via the host daemon; Harbor writes this trial under
        ``<jobs_dir>/<job-name>/``. ``--job-name`` is unique per trial — it
        expands the engine's per-trial slug inside the container
        (:data:`~tolokaforge_adapter_harbor.compose_synthesis.HARBOR_JOB_NAME_SHELL`),
        so concurrent trials of one task (``repeats>1`` / ``workers>1``) cannot
        collide on one job directory, and the verifier reads that exact path.

        ``-m`` is omitted for the keyless ``oracle`` agent (it runs the task's
        reference solution); the terminus family requires a ``provider/model``,
        and its ``openrouter/`` prefix is kept verbatim so LiteLLM routes to
        OpenRouter (reading ``OPENROUTER_API_KEY`` from ``agent_provider_env``).
        ``--agent-setup-timeout-multiplier`` is added for the terminus family
        (oracle runs no agent setup) so per-trial tooling installs fit the budget.

        No secret rides the argv: the provider key reaches the container as a
        compose env input, referenced here only by Harbor's own agent, never
        interpolated into this command string.
        """
        if self.agent != _ORACLE_AGENT and not self.agent_model:
            raise ValueError(
                "harbor adapter: `agent_model` is required to run a task with the "
                f"{self.agent!r} agent (set models.agent.name to a 'provider/model' "
                "string, e.g. 'openrouter/anthropic/claude-sonnet-4.6', with "
                "models.agent.harness: harbor). Use agent='oracle' for a keyless "
                "reference run."
            )
        parts = [
            "harbor run",
            f"-p {cs.CONTAINER_TASK_DIR}",
            f"-a {shlex.quote(self.agent)}",
        ]
        if self.agent != _ORACLE_AGENT:
            parts.append(f"-m {shlex.quote(self.agent_model)}")
        parts.append(f"-e {shlex.quote(self.sandbox_backend)}")
        parts.append(f"--jobs-dir {shlex.quote(jobs_dir)}")
        # Unquoted on purpose: the ``"$TOLOKAFORGE_TRIAL_SLUG"`` inside expands
        # in the container's shell to this trial's unique slug.
        parts.append(f"--job-name {cs.HARBOR_JOB_NAME_SHELL}")
        if self.agent != _ORACLE_AGENT:
            parts.append(
                f"--agent-setup-timeout-multiplier {self.agent_setup_timeout_multiplier:g}"
            )
        parts.append("-k 1 -y")
        for key, value in self.agent_kwargs.items():
            parts.append(f"--ak {shlex.quote(f'{key}={value}')}")
        return " ".join(parts)

    def to_task_description(self, task_id: str) -> TaskDescription:
        self._ensure_discovered()
        task = self._tasks[task_id]
        env = self._environment(task_id)
        command = self._harbor_command(str(env.harbor_jobs_dir))
        manifest = resolve_environment_patch(None, self._environment_patch(task_id))
        exec_tool = ToolSchema(
            name="bash",
            description="Execute a bash command inside the harbor task container",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            category="compute",
            timeout_s=self._agent_timeout_s(task),
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
            name=task_id,
            category="harbor",
            description=f"Harbor task {task_id}",
            adapter_type=_ADAPTER_TYPE,
            system_prompt=self.get_system_prompt(task_id),
            agent_tools=[exec_tool],
            grading=RunnerGradingConfig(**_TEST_EXECUTION_GRADING),
            environment_manifest=manifest,
            metadata={
                "agent_harness_command": command,
                "agent_harness": "harbor",
                "harbor_agent": self.agent,
                "harbor_model": self.agent_model,
                "harbor_version": self.harbor_version,
            },
        )

    # -- environment (Harbor owns the real environment) ----------------------

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
            reasons="harbor grading runs via the Runner GradeTrial RPC (test_execution)",
        )


def _assert_host_visible_staging_root(staging_root: Path) -> None:
    """Refuse a ``staging_root`` Harbor's nested sandbox could not see.

    Harbor's sandbox bind-mounts the job directory via the **host** daemon, so
    the staging root must resolve to a real host path. When the adapter runs
    inside a container (the orchestrator image), a path that is not on a bind
    mount from the host is invisible to the host daemon — Harbor's sandbox then
    silently sees no results and every trial reads as ungradeable. Fail loud
    here instead. On a bare-host run (no container) the path is always
    host-visible, so the check is a no-op.
    """
    if not Path("/.dockerenv").exists():
        return
    try:
        mounts = Path("/proc/mounts").read_text()
    except OSError:
        return  # cannot introspect mounts; do not block the run
    mountpoints = {fields[1] for line in mounts.splitlines() if len(fields := line.split()) >= 2}
    if any(str(ancestor) in mountpoints for ancestor in (staging_root, *staging_root.parents)):
        return
    raise ValueError(
        f"harbor adapter: staging_root {staging_root} is not on a host-visible bind "
        "mount, but this process is containerised. Harbor's sandbox bind-mounts the "
        "job directory through the host daemon, so a container-only staging_root makes "
        "it see no results (every trial reads as ungradeable). Point staging_root at a "
        "directory bind-mounted from the host (e.g. under the mounted output dir)."
    )


def _resolve_provider_env(raw: dict[str, str]) -> dict[str, str]:
    if not raw:
        return {}
    secrets = get_default()
    return {
        key: expand_secret_refs(value, secrets, where=f"harbor agent_provider_env[{key}]")
        for key, value in raw.items()
    }


def _as_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _as_str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"harbor adapter: expected a mapping of agent kwargs; got {value!r}.")
    return {str(k): str(v) for k, v in value.items()}
