"""Analyze Tolokaforge run artifacts programmatically.

Example:
    python examples/analyze_results/analyze_run.py --run-dir results/my_run
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from tolokaforge import (
    Trajectory,
    attribute_failure,
    calculate_aggregate_metrics,
    calculate_task_metrics,
    compute_pass_at_k,
    summarize_failure_attributions,
)


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_trajectory_from_trial_dir(trial_dir: Path) -> Trajectory | None:
    trajectory_path = trial_dir / "trajectory.yaml"
    if not trajectory_path.exists():
        return None

    payload = _read_yaml(trajectory_path)

    metrics_path = trial_dir / "metrics.yaml"
    if metrics_path.exists():
        payload["metrics"] = _read_yaml(metrics_path)

    grade_path = trial_dir / "grade.yaml"
    if grade_path.exists():
        payload["grade"] = _read_yaml(grade_path)

    env_path = trial_dir / "env.yaml"
    if env_path.exists():
        payload["final_env_state"] = _read_yaml(env_path)

    return Trajectory.model_validate(payload)


def collect_trajectories(run_dir: Path) -> list[Trajectory]:
    trajectories: list[Trajectory] = []
    trials_root = run_dir / "trials"
    if not trials_root.exists():
        return trajectories

    for trajectory_path in sorted(trials_root.glob("*/*/trajectory.yaml")):
        trial_dir = trajectory_path.parent
        trajectory = load_trajectory_from_trial_dir(trial_dir)
        if trajectory is not None:
            trajectories.append(trajectory)

    return trajectories


def is_failed(trajectory: Trajectory) -> bool:
    if trajectory.grade is None:
        return True
    if not trajectory.grade.binary_pass:
        return True
    return trajectory.status.value in {"failed", "timeout", "error"}


def build_report(trajectories: list[Trajectory]) -> dict[str, Any]:
    by_task: dict[str, list[Trajectory]] = defaultdict(list)
    for trajectory in trajectories:
        by_task[trajectory.task_id].append(trajectory)

    per_task_metrics: list[dict[str, Any]] = []
    for task_id, task_trajectories in sorted(by_task.items()):
        metrics = calculate_task_metrics(task_trajectories)
        metrics["task_id"] = task_id
        per_task_metrics.append(metrics)

    aggregate = calculate_aggregate_metrics(per_task_metrics, weighted=True)

    total = len(trajectories)
    passed = sum(
        1 for trajectory in trajectories if trajectory.grade and trajectory.grade.binary_pass
    )
    aggregate["run_pass_rate"] = (passed / total) if total else 0.0
    aggregate["run_pass_at_1"] = compute_pass_at_k(n=total, c=passed, k=1) if total else 0.0

    failure_attributions = [
        attribute_failure(trajectory) for trajectory in trajectories if is_failed(trajectory)
    ]
    failure_summary = summarize_failure_attributions(failure_attributions)

    return {
        "summary": {
            "total_trajectories": total,
            "total_tasks": len(by_task),
            "passing_trajectories": passed,
            "failing_trajectories": total - passed,
        },
        "aggregate_metrics": aggregate,
        "per_task_metrics": per_task_metrics,
        "failure_attribution": {
            "summary": failure_summary,
            "failures": failure_attributions,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Tolokaforge run artifacts.")
    parser.add_argument("--run-dir", required=True, help="Path to run output directory")
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output JSON path (default: <run-dir>/analysis_summary.json)",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

    trajectories = collect_trajectories(run_dir)
    if not trajectories:
        raise RuntimeError(f"No trajectory artifacts found under: {run_dir / 'trials'}")

    report = build_report(trajectories)

    output_path = Path(args.output).resolve() if args.output else run_dir / "analysis_summary.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    summary = report["summary"]
    aggregate = report["aggregate_metrics"]
    failure_summary = report["failure_attribution"]["summary"]

    print(f"Analysis complete for: {run_dir}")
    print(
        "Trajectories: {total} (pass={passed}, fail={failed})".format(
            total=summary["total_trajectories"],
            passed=summary["passing_trajectories"],
            failed=summary["failing_trajectories"],
        )
    )
    print(
        "Success rate: {rate:.3f} | pass@1: {pass1:.3f} | total_cost_usd: ${cost:.4f}".format(
            rate=aggregate.get("run_pass_rate", 0.0),
            pass1=aggregate.get("run_pass_at_1", 0.0),
            cost=aggregate.get("total_cost_usd", 0.0),
        )
    )
    coverage = failure_summary.get("deterministic_attribution_coverage")
    if coverage is None:
        print("Failure attribution coverage (deterministic): N/A (no failures)")
    else:
        print(f"Failure attribution coverage (deterministic): {coverage:.3f}")
    print(f"Report written to: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
