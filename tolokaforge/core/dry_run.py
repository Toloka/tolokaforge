"""Dry-run first-turn materialisation — no Docker, no HTTP, no run directory.

Produces a :class:`DryRunSample` per task: the exact system prompt / user
prompt / sanitized tool spec the agent's :class:`LLMClient` would see on
the first ``generate`` call, plus the resolved agent / judge / runtime
identifiers. The rendering layer consumes these; the CLI's dry-run
branch stitches them together.

Two helpers:

* :func:`load_tasks_for_dry_run` — build the adapter from a
  :class:`RunConfig` and enumerate every declared :class:`TaskConfig`.
  Deliberately skips the TypeSense preflight ``Orchestrator.load_tasks``
  performs.
* :func:`materialize_dry_run_sample` — assemble the first-turn payload
  for one task without instantiating :class:`LLMClient` or opening a
  socket.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tolokaforge.adapters import BaseAdapter, get_adapter
from tolokaforge.core.llm.presets import build_capabilities, resolve_effective_preset
from tolokaforge.core.system_prompt import build_system_prompt
from tolokaforge.tools.registry import sanitize_schema_properties

if TYPE_CHECKING:
    from tolokaforge.core.models import ModelConfig, ProjectConfig, RunConfig, TaskConfig
    from tolokaforge.runner.models import ToolSchema

__all__ = [
    "DryRunSample",
    "load_tasks_for_dry_run",
    "materialize_dry_run_sample",
    "tool_schema_to_openai_dict",
]


@dataclass(frozen=True)
class DryRunSample:
    """First-turn wire payload for a single ``(task_id, trial=0)``.

    Fields carry the exact strings / tool list the agent's LLM client
    would receive on the first ``generate`` call. Rendered by the CLI
    display layer; not serialised to disk.
    """

    task_id: str
    trial_index: int
    system_prompt: str
    user_prompt_text: str
    user_prompt_is_literal: bool
    tool_spec: list[dict[str, Any]]
    agent_model_line: str
    judge_model_line: str
    runtime_line: str


def tool_schema_to_openai_dict(tool_schema: ToolSchema) -> dict[str, Any]:
    """Convert a :class:`ToolSchema` to the OpenAI ``function`` wire shape.

    Matches the transformation :meth:`InProcessConductor._setup_trial`
    performs on ``runtime_backend.register_trial(...)["tool_schemas"]``.
    Property-name sanitisation runs here so downstream parity assertions
    against the runner's wire payload hold byte-for-byte.
    """
    return {
        "type": "function",
        "function": {
            "name": tool_schema.name,
            "description": tool_schema.description,
            "parameters": sanitize_schema_properties(copy.deepcopy(tool_schema.parameters)),
        },
    }


def _placeholder_user_prompt(task: TaskConfig) -> str:
    """Placeholder line for tasks with an LLM-driven first user message.

    Names the simulator mode, persona, and a truncated backstory so the
    operator sees what the user simulator would generate at runtime
    without firing the simulator's LLM call.
    """
    sim = task.user_simulator
    backstory = sim.backstory or ""
    truncated = backstory[:120]
    suffix = "…" if len(backstory) > 120 else ""
    return (
        f"<generated at runtime by user simulator — mode={sim.mode}, "
        f"persona={sim.persona}, backstory={truncated}{suffix}>"
    )


def _resolve_user_prompt(task: TaskConfig) -> tuple[str, bool]:
    literal = task.initial_user_message
    if literal is not None and literal.strip():
        return literal, True
    return _placeholder_user_prompt(task), False


def _model_line(model: ModelConfig) -> str:
    preset = resolve_effective_preset(model.name, model.provider)
    return f"{model.provider}/{model.name} · preset: {preset}"


def _build_dry_run_adapter_params(
    run_config: RunConfig,
    project: ProjectConfig | None,
) -> tuple[str | None, dict[str, Any]]:
    """Return ``(adapter_type, params)`` for adapter construction.

    Mirrors :meth:`Orchestrator._create_adapter` — same param assembly,
    same env-override for ``TASK_PACKS_DIRS``, same project-defaults
    forwarding — minus every Docker / TypeSense side effect. The
    typesense config *is* forwarded when declared so adapters that
    embed it in :class:`TaskDescription` render the config verbatim
    (unresolved port / api_key) for the operator to inspect.
    """
    adapter_config = run_config.evaluation.harness_adapter
    if adapter_config:
        adapter_type: str | None = adapter_config.type
        params: dict[str, Any] = dict(adapter_config.params)
    else:
        adapter_type = None
        params = {}

    params["tasks_glob"] = run_config.evaluation.tasks_glob
    task_packs = list(run_config.evaluation.projects)
    env_task_packs = os.environ.get("TASK_PACKS_DIRS", "").strip()
    if env_task_packs:
        task_packs = [part.strip() for part in env_task_packs.split(",") if part.strip()]
    params["task_packs"] = task_packs

    typesense_config = run_config.orchestrator.typesense
    if typesense_config and typesense_config.enabled:
        params["typesense"] = typesense_config.model_dump()

    if project is not None:
        defaults = project.task_defaults.model_dump(exclude_defaults=True)
        if defaults:
            params["project_task_defaults"] = defaults
        if project.default_environment is not None:
            params["project_default_environment"] = project.default_environment

    return adapter_type, params


def load_tasks_for_dry_run(
    *,
    run_config: RunConfig,
    project: ProjectConfig | None = None,
) -> tuple[BaseAdapter, list[TaskConfig]]:
    """Instantiate the adapter and load every declared task.

    Skips the TypeSense preflight :meth:`Orchestrator.load_tasks` runs
    (dry-run must not start Docker containers). Constructs the adapter
    via the shared :func:`get_adapter` factory using the same parameter
    assembly the orchestrator uses at run start. Failed task loads
    propagate as exceptions — surfacing config errors here is the
    point of ``--dry-run``.
    """
    adapter_type, params = _build_dry_run_adapter_params(run_config, project)
    adapter = get_adapter(adapter_type, params)

    tasks: list[TaskConfig] = []
    for task_id in adapter.get_task_ids():
        tasks.append(adapter.get_task(task_id))
    return adapter, tasks


def _sanitized_tool_spec(
    *,
    adapter: BaseAdapter,
    task_id: str,
    agent_config: ModelConfig,
) -> list[dict[str, Any]]:
    """Materialise the OpenAI tool list the LLM would receive on turn 1.

    Runs the same three-step pipeline the production path uses:

    1. ``adapter.to_task_description(task_id).agent_tools`` — the
       orchestrator-side authoring source for tool schemas.
    2. Convert each :class:`ToolSchema` to the OpenAI ``function`` shape
       and apply property-name sanitisation (matches
       :meth:`InProcessConductor._setup_trial`).
    3. Apply the capability schema sanitizer resolved from the agent
       model's preset — the same pass :meth:`LLMClient.generate` runs at
       wire time.
    """
    task_description = adapter.to_task_description(task_id)
    raw_tool_schemas = list(task_description.agent_tools)
    openai_shape = [tool_schema_to_openai_dict(ts) for ts in raw_tool_schemas]
    capabilities = build_capabilities(
        agent_config.name,
        agent_config.provider,
        overrides=agent_config.capabilities,
    )
    return list(capabilities.schema_sanitizer.sanitize(openai_shape))


def materialize_dry_run_sample(
    *,
    task: TaskConfig,
    adapter: BaseAdapter,
    agent_config: ModelConfig,
    judge_config: ModelConfig | None,
    runtime_choice: str,
) -> DryRunSample:
    """Produce a :class:`DryRunSample` for one task.

    Assembles the same three inputs :meth:`ToolCallingLoop._generate`
    would pass on the first turn — system prompt, first user message,
    sanitized tool spec — plus the resolved model / judge / runtime
    identifiers used in the rendered summary. Pure Python + local file
    reads. No LLM client is constructed; no socket is opened.
    """
    task_dir = adapter.get_task_dir(task.task_id)
    system_prompt = build_system_prompt(task=task, task_dir=task_dir, adapter=adapter)
    user_prompt_text, user_prompt_is_literal = _resolve_user_prompt(task)
    tool_spec = _sanitized_tool_spec(
        adapter=adapter, task_id=task.task_id, agent_config=agent_config
    )
    judge_line = _model_line(judge_config) if judge_config is not None else "(none)"
    return DryRunSample(
        task_id=task.task_id,
        trial_index=0,
        system_prompt=system_prompt,
        user_prompt_text=user_prompt_text,
        user_prompt_is_literal=user_prompt_is_literal,
        tool_spec=tool_spec,
        agent_model_line=_model_line(agent_config),
        judge_model_line=judge_line,
        runtime_line=runtime_choice,
    )
