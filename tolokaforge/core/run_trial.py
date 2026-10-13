"""Top-level single-trial library entry point (ADR-0022 § Surface 2).

A thin convenience over :func:`tolokaforge.core.trial_node.run_trial_from_inputs`:
it resolves the task's wire description through its adapter, packs the call
into a :class:`~tolokaforge.core.trial.TrialNodeInputs`, and hands it to the
standalone trial entry, which composes the adapter, LLM client, runtime
backend, conductor and trial grader and returns a typed
:class:`~tolokaforge.core.trial.TrialResult` — without the caller reconstructing
the orchestrator or its batch lifecycle (queue, run-state, worker pool, budget,
resume). The runtime backend, grader, and conductor are resolved by registered
name through the entry-point registries in :mod:`tolokaforge.core.plugin_registry`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tolokaforge.core.models import GraderConfig, ModelConfig, RateLimitProbeConfig, TaskConfig
from tolokaforge.core.shared_stack_runtime import _build_env_endpoints
from tolokaforge.core.trial import (
    DEFAULT_TOOL_TIMEOUT_S,
    TrialNodeInputs,
    TrialResult,
    TrialSpec,
)
from tolokaforge.core.trial_node import (
    build_single_task_adapter,
    compute_task_pack_hash,
    run_trial_from_inputs,
)
from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB

_RUN_ID = "run_trial"


class _RunTrialModels(BaseModel):
    """Coercion gate for the ``models`` argument.

    A required ``agent`` and an ``extra="forbid"`` policy make both a missing
    ``agent`` key and any malformed / unexpected role value raise a Pydantic
    ``ValidationError`` (the ADR-named error type) before any registry or
    backend work.
    """

    model_config = {"extra": "forbid"}

    agent: ModelConfig
    user: ModelConfig | None = None
    judge: ModelConfig | None = None


def run_trial(
    *,
    task: TaskConfig,
    models: dict[str, ModelConfig | dict[str, Any]],
    runtime: str = "auto",
    grader: str = "runner_rpc",
    conductor: str = "in_process",
    output_dir: Path | str | None = None,
    trial_index: int = 0,
    rate_limit_probe: RateLimitProbeConfig | None = None,
) -> TrialResult:
    """Run one trial in-process and return its :class:`TrialResult`.

    Args:
        task: The task to run. When obtained through the adapter/loader it
            carries its source directory (``TaskConfig.source_dir``) so file
            assets resolve without a filesystem glob; a hand-built config
            resolves assets against the current directory and fails loud the
            moment a missing file is required.
        models: Role → model map. ``agent`` is required; ``user`` and
            ``judge`` are optional. Values may be :class:`ModelConfig`
            instances or plain dicts.
        runtime: Registered runtime-backend name, or ``"auto"`` (default) to
            pick ``"per_trial"`` when the task's manifest requires per-trial
            isolation, else ``"shared"``.
        grader: Registered trial-grader name.
        conductor: Registered conductor name.
        output_dir: When ``None`` (default) no artifacts touch disk; otherwise
            artifacts are written under this directory.
        trial_index: Trial index within the task; forms the ``"{task_id}:{n}"``
            trial id.
        rate_limit_probe: Rate-limit probe mode for the agent and simulator
            clients. ``None`` (default) runs the standard bounded-exponential
            retry path. Enabling it raises the composed run config's episode
            budget to twice the probe's per-*turn* 429 wall ceiling (a probe
            absorbs 429s by sleeping, and episode wall-time counts that sleep),
            which is what keeps the budget invariant true by construction for
            this composed-config surface. ``conductor`` must name an
            implementation that supports the mode.

    Returns:
        The trial's :class:`TrialResult`.

    Raises:
        pydantic.ValidationError: ``models`` is missing ``agent`` or malformed,
            or the composed run config is invalid.
        ValueError: ``rate_limit_probe`` is enabled with budgets that cannot
            fit inside the episode budget, or with a ``conductor`` that does not
            support the mode.
        UnknownImplementationError: ``runtime`` / ``grader`` / ``conductor``
            names no registered implementation.
        ProvisionError: The substrate failed to provision (propagated, not
            swallowed — the single-trial contract diverges from the batch
            executor's synthesise-and-continue behaviour).
        NotImplementedError: The task's adapter is not node-runnable (see
            :meth:`~tolokaforge.adapters.BaseAdapter.register_preloaded_task`).
    """
    resolved_models = _RunTrialModels.model_validate(models)

    adapter = build_single_task_adapter(task)
    task_desc = adapter.describe_task(task.task_id)

    runner_address = os.environ.get("EXECUTOR_ADDRESS", "executor:50051")
    pack_dir = task.source_dir

    spec = TrialSpec(
        trial_id=f"{task.task_id}:{trial_index}",
        run_id=_RUN_ID,
        task=task_desc,
        agent_model_config=resolved_models.agent,
        user_model_config=resolved_models.user,
        judge_model_config=resolved_models.judge,
        max_turns=task.max_turns,
        default_tool_timeout_s=DEFAULT_TOOL_TIMEOUT_S,
        env_endpoints=_build_env_endpoints(runner_address),
    )
    return run_trial_from_inputs(
        TrialNodeInputs(
            task=task,
            trial_spec=spec,
            grader_config=GraderConfig(name=grader),
            grpc_max_message_mb=DEFAULT_MAX_MESSAGE_MB,
            runtime=runtime,
            conductor=conductor,
            task_pack_dir=pack_dir,
            task_pack_hash=compute_task_pack_hash(pack_dir) if pack_dir is not None else None,
            output_dir=Path(output_dir) if output_dir is not None else None,
            rate_limit_probe=rate_limit_probe,
        )
    )
