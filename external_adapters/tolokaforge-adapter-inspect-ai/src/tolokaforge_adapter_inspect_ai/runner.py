"""Run Inspect tasks through the adapter and collect tolokaforge-typed results.

Uses the local subprocess bridge: for each discovered task, run ``inspect eval``
(Inspect executes its own solver + scorer against the model), then project the
``.eval`` log onto a tolokaforge ``Grade`` + ``Trajectory``. This is the runnable
path today; execution through the tolokaforge engine runner is a separate backend.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tolokaforge.core.models import Grade, Trajectory
from tolokaforge_adapter_inspect_ai import normalize
from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter
from tolokaforge_adapter_inspect_ai.bridge import run_inspect_eval


@dataclass(frozen=True)
class TaskRunResult:
    """Normalized outcome of running one Inspect task."""

    task_id: str
    grade: Grade
    trajectories: list[Trajectory]
    log_path: Path | None
    returncode: int


class InspectRunError(RuntimeError):
    """Raised when ``inspect eval`` produced no readable log for a task."""


def run_task(
    adapter: InspectAiAdapter,
    task_id: str,
    *,
    model: str,
    log_dir: str | Path,
    env: Mapping[str, str] | None = None,
    limit: int | None = None,
    timeout: float | None = None,
) -> TaskRunResult:
    """Run a single discovered task and normalize its ``.eval`` log."""
    info = adapter.inspect_task(task_id)
    result = run_inspect_eval(
        task_file=info.file,
        task_name=info.name,
        model=model,
        log_dir=log_dir,
        env=env,
        limit=limit,
        timeout=timeout,
    )
    if result.log_path is None:
        raise InspectRunError(
            f"`inspect eval` produced no .eval log for {task_id!r} "
            f"(returncode={result.returncode}).\n{result.stderr[-2000:]}"
        )

    from inspect_ai.log import read_eval_log

    log = read_eval_log(str(result.log_path))
    trajectories = [
        normalize.sample_trajectory(sample, task_id=task_id) for sample in (log.samples or [])
    ]
    return TaskRunResult(
        task_id=task_id,
        grade=normalize.run_grade(log),
        trajectories=trajectories,
        log_path=result.log_path,
        returncode=result.returncode,
    )


def run_pack(
    pack_dir: str | Path,
    *,
    model: str,
    output_dir: str | Path,
    env: Mapping[str, str] | None = None,
    task_ids: list[str] | None = None,
    tasks_glob: list[str] | str | None = None,
    limit: int | None = None,
    timeout: float | None = None,
) -> list[TaskRunResult]:
    """Run every discovered task in ``pack_dir`` and write results to ``output_dir``.

    Writes one ``<task_id>.json`` per task (grade + trajectories) plus a
    ``summary.json``, and returns the per-task results.
    """
    adapter = InspectAiAdapter(
        {"inspect_task_dir": str(pack_dir), "tasks_glob": tasks_glob, "task_ids": task_ids}
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = output_dir / "logs"

    results: list[TaskRunResult] = []
    for task_id in adapter.get_task_ids():
        result = run_task(
            adapter, task_id, model=model, log_dir=log_dir, env=env, limit=limit, timeout=timeout
        )
        _write_result(output_dir / f"{_safe_name(task_id)}.json", result)
        results.append(result)

    _write_summary(output_dir / "summary.json", results, model=model)
    return results


def _safe_name(task_id: str) -> str:
    return task_id.replace("/", "__")


def _write_result(path: Path, result: TaskRunResult) -> None:
    payload = {
        "task_id": result.task_id,
        "grade": result.grade.model_dump(mode="json"),
        "trajectories": [t.model_dump(mode="json") for t in result.trajectories],
        "log_path": str(result.log_path) if result.log_path else None,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))


def _write_summary(path: Path, results: list[TaskRunResult], *, model: str) -> None:
    payload = {
        "model": model,
        "task_count": len(results),
        "passed": sum(1 for r in results if r.grade.binary_pass),
        "tasks": [
            {"task_id": r.task_id, "binary_pass": r.grade.binary_pass, "score": r.grade.score}
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
