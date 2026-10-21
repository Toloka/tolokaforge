"""Assemble the construction params a run resolves its benchmark adapter with.

One function, called by every path that builds an adapter from a run config —
the orchestrator (``tolokaforge run``) and the library planning path alike — so
they resolve the same adapter from the same config.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tolokaforge.core.models import ProjectConfig, RunConfig

__all__ = ["assemble_adapter_params"]


def assemble_adapter_params(
    config: RunConfig,
    *,
    project: ProjectConfig | None = None,
) -> dict[str, Any]:
    """Build the params dict passed to ``get_adapter`` for *config*.

    Carries the harness adapter's own ``params``, the coding-harness selector on
    ``models.agent``, the task glob and project packs (with the
    ``TASK_PACKS_DIRS`` container override), the TypeSense search plane, and —
    when a *project* is given — its ``task_defaults`` and ``default_environment``.
    The adapter type itself is resolved by the caller.
    """
    adapter_config = config.evaluation.harness_adapter
    params: dict[str, Any] = dict(adapter_config.params) if adapter_config else {}

    # Coding-harness selector: canonical home is ``models.agent.harness``
    # (adapter-agnostic). ``models.agent.name`` doubles as the model the CLI
    # receives — the same field the engine loop reads.
    agent_model_config = config.models.get("agent") if config.models else None
    if agent_model_config is not None and agent_model_config.harness is not None:
        params.setdefault("agent_harness", agent_model_config.harness)
        params.setdefault("agent_model", agent_model_config.name)

    params["tasks_glob"] = config.evaluation.tasks_glob

    # ``evaluation.projects`` is the canonical field; ``TASK_PACKS_DIRS`` can
    # override config paths to container-visible mounts in Docker flows.
    task_packs = list(config.evaluation.projects)
    env_task_packs = os.environ.get("TASK_PACKS_DIRS", "").strip()
    if env_task_packs:
        task_packs = [part.strip() for part in env_task_packs.split(",") if part.strip()]
    params["task_packs"] = task_packs

    typesense_config = config.orchestrator.effective_typesense()
    if typesense_config is not None:
        params["typesense"] = typesense_config.model_dump()

    # Layer ``project.task_defaults`` under every task the adapter loads, and
    # forward the project's ``default_environment`` patch. ``exclude_defaults``
    # drops fields the project author didn't set.
    if project is not None:
        defaults = project.task_defaults.model_dump(exclude_defaults=True)
        if defaults:
            params["project_task_defaults"] = defaults
        if project.default_environment is not None:
            params["project_default_environment"] = project.default_environment

    return params
