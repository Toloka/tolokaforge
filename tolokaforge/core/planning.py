"""Library planning + aggregation surface: drive a run as composable steps.

Three functions let an external orchestration layer run a batch itself and
reproduce the report a single ``tolokaforge run`` writes:

* :func:`plan_trials` — a ``RunConfig`` (plus optional preloaded tasks) → the
  flat list of :class:`TrialNodeInputs`, one per ``(task, repeat)``. Each is a
  self-contained, serializable description of one trial; no adapter, conductor
  or runtime backend is resolved here — they travel as data.
* :func:`tolokaforge.core.trial_node.run_trial_from_inputs` — one
  :class:`TrialNodeInputs` → one :class:`TrialResult`. The caller runs these
  itself, on whatever backend it owns.
* :func:`aggregate_results` — the produced results (or a run directory) →
  the same four aggregate artifacts a full run writes.

The expansion math (:func:`expand_trials`) is the pure core the orchestrator's
own pending-trial build also calls.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from tolokaforge.core.models import ProjectConfig, RunConfig, TaskConfig, Trajectory
    from tolokaforge.core.output.report import RunReport
    from tolokaforge.core.trial import TrialNodeInputs, TrialResult

logger = logging.getLogger(__name__)


def expand_trials(
    tasks: Sequence[TaskConfig],
    repeats: int,
    *,
    shuffle: bool = False,
) -> list[tuple[str, int]]:
    """Expand ``tasks × repeats`` into ``(task_id, trial_index)`` pairs.

    The pure cartesian product in ``(task, trial_index)`` order. ``shuffle``
    randomizes the order in place (diagnostic only — it does not eliminate
    state leakage between trials). Resume filtering and any display-index
    bookkeeping stay with the caller.
    """
    pairs = [(task.task_id, trial_idx) for task in tasks for trial_idx in range(repeats)]
    if shuffle:
        random.shuffle(pairs)
    return pairs


def plan_trials(
    run_config: RunConfig,
    tasks: Sequence[TaskConfig] | None = None,
    *,
    runtime: str = "auto",
    grader: str | None = None,
    conductor: str = "in_process",
    run_id: str | None = None,
    project: ProjectConfig | None = None,
) -> list[TrialNodeInputs]:
    """Plan a run's trials as a flat list of serializable :class:`TrialNodeInputs`.

    Args:
        run_config: The run configuration. Supplies the role → model map
            (``models.agent`` required; ``user`` / ``judge`` optional), the
            repeat count and shuffle flag (``orchestrator``), the run-level
            grader block (``grader``), and the node run settings
            (:meth:`NodeRunSettings.from_config`).
        tasks: Preloaded tasks to plan. ``None`` loads them from *run_config*
            through its declared adapter — preloading gives exact control over
            the task set.
        runtime: Registered runtime-backend name carried as data, or ``"auto"``
            (default) for the node to pick per-trial isolation from the task.
        grader: Registered trial-grader name. ``None`` (default) carries the
            run config's own ``grader`` block, which defers to the adapter's
            default grader when it too is unset.
        conductor: Registered conductor name carried as data.
        run_id: The run identifier stamped onto every trial. ``None`` derives a
            stable id from the run config's output directory.
        project: The run's :class:`ProjectConfig`, when one applies. Its
            ``task_defaults`` and ``default_environment`` layer into the adapter
            params exactly as ``tolokaforge run`` applies them, so each planned
            trial describes its task identically to a single-box run.

    Returns:
        One :class:`TrialNodeInputs` per ``(task, repeat)``, each ready to hand
        to :func:`run_trial_from_inputs`.

    Raises:
        pydantic.ValidationError: ``models`` has no ``agent`` entry.
    """
    import os

    from tolokaforge.core.adapter_params import assemble_adapter_params
    from tolokaforge.core.models import ComputeConfig, GraderConfig
    from tolokaforge.core.shared_stack_runtime import _build_env_endpoints
    from tolokaforge.core.trial import (
        DEFAULT_TOOL_TIMEOUT_S,
        NodeRunSettings,
        TrialNodeInputs,
        TrialSpec,
    )
    from tolokaforge.core.trial_node import build_single_task_adapter, compute_task_pack_hash

    task_list = list(tasks) if tasks is not None else _load_tasks_from_config(run_config, project)
    adapter_params = assemble_adapter_params(run_config, project=project)

    agent_config = run_config.models.get("agent")
    if agent_config is None:
        raise ValueError(
            "run_config.models has no 'agent' entry; plan_trials needs an agent model "
            "to build each trial's spec."
        )
    user_config = run_config.models.get("user")
    judge_config = run_config.models.get("judge")

    grader_config = GraderConfig(name=grader) if grader is not None else run_config.grader
    run_settings = NodeRunSettings.from_config(run_config)
    # Mirror the orchestrator's resolution: an unset ``compute`` block falls back
    # to its default message bound.
    grpc_max_message_mb = (run_config.compute or ComputeConfig()).grpc_max_message_mb
    probe = run_config.orchestrator.rate_limit_probe
    rate_limit_probe = probe if probe.enabled else None
    resolved_run_id = run_id or _resolve_run_id(run_config)

    runner_address = os.environ.get("EXECUTOR_ADDRESS", "executor:50051")
    env_endpoints = _build_env_endpoints(runner_address)

    task_by_id = {task.task_id: task for task in task_list}
    description_by_id: dict[str, Any] = {}
    pack_hash_by_dir: dict[Path, str] = {}

    planned: list[TrialNodeInputs] = []
    for task_id, trial_idx in expand_trials(
        task_list, run_config.orchestrator.repeats, shuffle=run_config.orchestrator.shuffle_trials
    ):
        task = task_by_id[task_id]
        if task_id not in description_by_id:
            description_by_id[task_id] = build_single_task_adapter(
                task, adapter_params=adapter_params
            ).describe_task(task_id)
        task_desc = description_by_id[task_id]

        pack_dir = task.source_dir
        pack_hash: str | None = None
        if pack_dir is not None:
            if pack_dir not in pack_hash_by_dir:
                pack_hash_by_dir[pack_dir] = compute_task_pack_hash(pack_dir)
            pack_hash = pack_hash_by_dir[pack_dir]

        spec = TrialSpec(
            trial_id=f"{task_id}:{trial_idx}",
            run_id=resolved_run_id,
            task=task_desc,
            agent_model_config=agent_config,
            user_model_config=user_config,
            judge_model_config=judge_config,
            max_turns=task.max_turns,
            default_tool_timeout_s=DEFAULT_TOOL_TIMEOUT_S,
            env_endpoints=env_endpoints,
        )
        planned.append(
            TrialNodeInputs(
                task=task,
                trial_spec=spec,
                grader_config=grader_config,
                grpc_max_message_mb=grpc_max_message_mb,
                runtime=runtime,
                conductor=conductor,
                task_pack_dir=pack_dir,
                task_pack_hash=pack_hash,
                output_dir=None,
                rate_limit_probe=rate_limit_probe,
                run_settings=run_settings,
            )
        )
    return planned


def aggregate_results(
    results: Iterable[TrialResult | Trajectory] | Path | str,
    *,
    run_config: RunConfig | None = None,
    tasks: Sequence[TaskConfig] | None = None,
    output_dir: Path | str | None = None,
) -> RunReport:
    """Fold independently produced trial results into a run's aggregate report.

    Args:
        results: The trials to aggregate. Either an iterable of
            :class:`TrialResult` / :class:`Trajectory`, or a run directory
            (``Path`` / ``str``) whose ``trials/<task_id>/<trial_index>/`` bundles
            are read back. ``trial_index`` is renumbered ``0..N-1`` per task, so
            independently produced single-trial results (all ``trial_index=0``)
            count as the repeats they are.
        run_config: Run config to source per-task metadata from (benchmark type,
            complexity, tags, expected failure modes), loading tasks through its
            adapter. Optional.
        tasks: Preloaded tasks to source per-task metadata from; takes precedence
            over loading from *run_config*.
        output_dir: When given, the four aggregate JSONs are written here through
            the same :class:`FileAggregateWriter` a full run uses.

    Returns:
        The :class:`RunReport` — the reproducible core of the report a full
        ``tolokaforge run`` writes.
    """
    from pathlib import Path

    from tolokaforge.core.output.aggregates import FileAggregateWriter
    from tolokaforge.core.output.report import build_run_report

    trajectories = _renumber_trial_index(_normalize_to_trajectories(results))
    tasks_metadata = _collect_task_metadata(trajectories, run_config, tasks, results)

    report = build_run_report(trajectories, tasks_metadata)

    if output_dir is not None:
        FileAggregateWriter().write_run_aggregates(
            Path(output_dir),
            report.per_task_metrics,
            report.aggregate,
            report.metadata_slices,
            report.failure_attribution,
        )
    return report


def _load_tasks_from_config(
    run_config: RunConfig,
    project: ProjectConfig | None = None,
) -> list[TaskConfig]:
    """Load the run's tasks through its declared adapter.

    Resolves the same adapter params ``tolokaforge run`` does — adapter type and
    params, task glob, project packs, the coding-harness selector on
    ``models.agent``, TypeSense, and (when a *project* is given) its
    ``task_defaults`` and ``default_environment`` — via
    :func:`assemble_adapter_params`.
    """
    from tolokaforge.adapters import get_adapter
    from tolokaforge.core.adapter_params import assemble_adapter_params

    adapter_config = run_config.evaluation.harness_adapter
    adapter_type = adapter_config.type if adapter_config is not None else "native"
    params = assemble_adapter_params(run_config, project=project)

    adapter = get_adapter(adapter_type, params)
    return [adapter.get_task(task_id) for task_id in adapter.get_task_ids()]


def _resolve_run_id(run_config: RunConfig) -> str:
    """A stable run id from the config's output directory basename."""
    from pathlib import Path

    return Path(run_config.evaluation.output_dir).name or "library_run"


def _normalize_to_trajectories(results: Any) -> list[Any]:
    """Coerce *results* to a flat list of :class:`Trajectory`.

    Accepts a run directory (read back through
    :func:`read_report_trajectory`), or an iterable of :class:`TrialResult`
    (unwrapped to ``.trajectory``) / :class:`Trajectory`.
    """
    from pathlib import Path

    from tolokaforge.core.models import Trajectory

    if isinstance(results, (str, Path)):
        return _read_run_dir_trajectories(Path(results))

    trajectories: list[Any] = []
    for item in results:
        trajectory = getattr(item, "trajectory", item)
        if not isinstance(trajectory, Trajectory):
            raise TypeError(
                "aggregate_results accepts TrialResult, Trajectory, or a run directory; "
                f"got {type(item).__name__}"
            )
        trajectories.append(trajectory)
    return trajectories


def _read_run_dir_trajectories(run_dir: Path) -> list[Any]:
    """Read every ``trials/<task_id>/<trial_index>/`` bundle under *run_dir*."""
    from tolokaforge.core.output.artifacts import read_report_trajectory

    trials_root = run_dir / "trials"
    if not trials_root.is_dir():
        raise FileNotFoundError(
            f"{run_dir} has no trials/ directory; aggregate_results reads a run's trial bundles."
        )
    trajectories = []
    for task_dir in sorted(p for p in trials_root.iterdir() if p.is_dir()):
        for trial_dir in sorted(p for p in task_dir.iterdir() if p.is_dir()):
            trajectories.append(read_report_trajectory(trial_dir))
    return trajectories


def _renumber_trial_index(trajectories: list[Any]) -> list[Any]:
    """Renumber ``trial_index`` ``0..N-1`` per ``task_id``, preserving order.

    Independently produced single-trial results all carry ``trial_index=0``;
    renumbering keeps them distinct so the per-task sample counts the repeats.
    """
    next_index: dict[str, int] = {}
    renumbered = []
    for trajectory in trajectories:
        index = next_index.get(trajectory.task_id, 0)
        next_index[trajectory.task_id] = index + 1
        renumbered.append(
            trajectory
            if trajectory.trial_index == index
            else trajectory.model_copy(update={"trial_index": index})
        )
    return renumbered


def _collect_task_metadata(
    trajectories: list[Any],
    run_config: RunConfig | None,
    tasks: Sequence[TaskConfig] | None,
    results: Any,
) -> dict[str, Any]:
    """Build the ``task_id`` → metadata map the report's slices group by.

    Precedence: explicit *tasks*, then *run_config* (loading its tasks), then a
    run directory's per-trial ``task.yaml`` snapshots. A task with no metadata
    source is folded unclassified.
    """
    from pathlib import Path

    from tolokaforge.core.output.report import TaskReportMetadata

    task_configs: list[Any] = []
    if tasks is not None:
        task_configs = list(tasks)
    elif run_config is not None:
        task_configs = _load_tasks_from_config(run_config)

    metadata: dict[str, Any] = {
        task.task_id: TaskReportMetadata(
            benchmark_type=task.category,
            complexity=task.metadata.complexity,
            expected_failure_modes=tuple(task.metadata.expected_failure_modes),
            tags=tuple(task.metadata.tags),
        )
        for task in task_configs
    }
    if metadata:
        return metadata

    if isinstance(results, (str, Path)):
        return _task_metadata_from_snapshots(Path(results), trajectories)
    return {}


def _task_metadata_from_snapshots(run_dir: Path, trajectories: list[Any]) -> dict[str, Any]:
    """Read per-task metadata from each trial's ``task.yaml`` snapshot, best effort."""
    import yaml

    from tolokaforge.core.output.report import TaskReportMetadata

    metadata: dict[str, Any] = {}
    for trajectory in trajectories:
        task_id = trajectory.task_id
        if task_id in metadata:
            continue
        snapshot_path = run_dir / "trials" / task_id / str(trajectory.trial_index) / "task.yaml"
        if not snapshot_path.exists():
            continue
        try:
            with snapshot_path.open(encoding="utf-8") as stream:
                snapshot = yaml.safe_load(stream) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.debug("skipping unreadable task snapshot %s: %s", snapshot_path, exc)
            continue
        task_metadata = snapshot.get("metadata") or {}
        metadata[task_id] = TaskReportMetadata(
            benchmark_type=snapshot.get("category"),
            complexity=task_metadata.get("complexity"),
            expected_failure_modes=tuple(task_metadata.get("expected_failure_modes") or ()),
            tags=tuple(task_metadata.get("tags") or ()),
        )
    return metadata


__all__ = [
    "aggregate_results",
    "expand_trials",
    "plan_trials",
]
