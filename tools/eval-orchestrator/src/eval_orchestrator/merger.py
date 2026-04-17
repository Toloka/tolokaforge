"""Merge shard output directories into a single combined result.

Each shard produces an output directory like::

    output/shard-0/
      run_state.json
      aggregate.json
      trials/
        TASK-001/0/grade.yaml
        TASK-001/0/metrics.yaml
        TASK-001/0/trajectory.yaml
        TASK-002/0/grade.yaml
        ...

The merger:
  1. Copies all ``trials/`` subdirectories into a unified output.
  2. Merges ``run_state.json`` trial maps.
  3. Re-aggregates metrics from all per-trial ``metrics.yaml`` and ``grade.yaml``
     files to produce a combined ``aggregate.json`` and ``run_state.json``.
  4. Writes a Markdown summary suitable for ``$GITHUB_STEP_SUMMARY``.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml


def discover_shard_dirs(input_dirs: list[Path]) -> list[Path]:
    """Return only shard directories that actually contain output data.

    A valid shard dir is one that has a ``trials/`` subdirectory OR a
    ``run_state.json`` file.
    """
    valid = []
    for d in input_dirs:
        if not d.is_dir():
            continue
        if (d / "trials").is_dir() or (d / "run_state.json").exists():
            valid.append(d)
    return valid


def copy_trials(shard_dirs: list[Path], output_dir: Path) -> int:
    """Copy trial directories from all shards into the merged output.

    Returns:
        Total number of trial leaf directories copied.
    """
    merged_trials = output_dir / "trials"
    merged_trials.mkdir(parents=True, exist_ok=True)
    count = 0

    for shard_dir in shard_dirs:
        trials_dir = shard_dir / "trials"
        if not trials_dir.is_dir():
            continue

        for task_dir in sorted(trials_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            dest_task = merged_trials / task_dir.name
            dest_task.mkdir(parents=True, exist_ok=True)
            for trial_dir in sorted(task_dir.iterdir()):
                if not trial_dir.is_dir():
                    continue
                dest_trial = dest_task / trial_dir.name
                if dest_trial.exists():
                    shutil.rmtree(dest_trial)
                shutil.copytree(trial_dir, dest_trial)
                count += 1

    return count


def merge_run_states(shard_dirs: list[Path]) -> dict:
    """Merge run_state.json from all shards into a combined state dict."""
    merged_trials: dict = {}
    total_completed = 0
    total_failed = 0
    earliest_start = None
    latest_update = None

    for shard_dir in shard_dirs:
        state_path = shard_dir / "run_state.json"
        if not state_path.exists():
            continue

        with open(state_path) as f:
            state = json.load(f)

        # Merge trial entries
        for key, trial in state.get("trials", {}).items():
            merged_trials[key] = trial

        total_completed += state.get("completed_trials", 0)
        total_failed += state.get("failed_trials", 0)

        start_ts = state.get("start_ts")
        if start_ts:
            if earliest_start is None or start_ts < earliest_start:
                earliest_start = start_ts

        updated = state.get("last_updated")
        if updated:
            if latest_update is None or updated > latest_update:
                latest_update = updated

    now = datetime.now(tz=timezone.utc).isoformat()

    return {
        "run_id": "merged",
        "config_path": "merged",
        "output_dir": "output/merged",
        "start_ts": earliest_start or now,
        "last_updated": latest_update or now,
        "status": "completed",
        "total_trials": len(merged_trials),
        "completed_trials": total_completed,
        "failed_trials": total_failed,
        "trials": merged_trials,
    }


def _load_yaml_safe(path: Path) -> dict | None:
    """Load a YAML file, returning None on any error."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def aggregate_metrics(output_dir: Path) -> dict:
    """Compute aggregate metrics from all trial grade and metrics files.

    Walks ``output_dir/trials/{task_id}/{trial_idx}/`` and reads
    ``grade.yaml`` and ``metrics.yaml`` from each.

    Returns:
        Dictionary with aggregate statistics.
    """
    trials_dir = output_dir / "trials"
    if not trials_dir.is_dir():
        return {}

    total_trials = 0
    passed = 0
    total_cost = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    total_latency = 0.0
    total_turns = 0
    total_tool_calls = 0
    task_ids: set[str] = set()
    stuck_count = 0

    for task_dir in sorted(trials_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        task_ids.add(task_id)

        for trial_dir in sorted(task_dir.iterdir()):
            if not trial_dir.is_dir():
                continue

            total_trials += 1

            grade = _load_yaml_safe(trial_dir / "grade.yaml")
            if grade:
                if grade.get("binary_pass"):
                    passed += 1
                if grade.get("stuck"):
                    stuck_count += 1

            metrics = _load_yaml_safe(trial_dir / "metrics.yaml")
            if metrics:
                total_cost += metrics.get("cost_usd_est", 0) or 0
                total_tokens_in += metrics.get("tokens_input", 0) or 0
                total_tokens_out += metrics.get("tokens_output", 0) or 0
                total_latency += metrics.get("latency_total_s", 0) or 0
                total_turns += metrics.get("turns", 0) or 0
                total_tool_calls += metrics.get("tool_calls", 0) or 0

    if total_trials == 0:
        return {"total_trials": 0, "total_tasks": 0}

    return {
        "total_trials": total_trials,
        "total_tasks": len(task_ids),
        "success_rate_micro": round(passed / total_trials, 4),
        "passed": passed,
        "failed_attempts": total_trials - passed,
        "total_cost_usd": round(total_cost, 4),
        "avg_cost_per_task": round(total_cost / total_trials, 4),
        "avg_latency_s": round(total_latency / total_trials, 1),
        "avg_turns": round(total_turns / total_trials, 1),
        "avg_tool_calls": round(total_tool_calls / total_trials, 1),
        "stuck_rate": round(stuck_count / total_trials, 4),
        "total_tokens_input": total_tokens_in,
        "total_tokens_output": total_tokens_out,
    }


def write_summary_markdown(aggregate: dict, output_path: Path) -> str:
    """Write a Markdown summary table and return the content.

    Suitable for ``$GITHUB_STEP_SUMMARY``.
    """
    total = aggregate.get("total_trials", 0)
    lines = [
        "## Benchmark Results (merged)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Pass rate | {aggregate.get('success_rate_micro', 0):.1%} |",
        f"| Passed / Total trials | {aggregate.get('passed', 0)} / {total} |",
        f"| Total tasks | {aggregate.get('total_tasks', 0)} |",
        f"| Total cost | ${aggregate.get('total_cost_usd', 0):.2f} |",
        f"| Avg cost/task | ${aggregate.get('avg_cost_per_task', 0):.4f} |",
        f"| Avg latency | {aggregate.get('avg_latency_s', 0):.1f}s |",
        f"| Avg turns | {aggregate.get('avg_turns', 0):.1f} |",
        f"| Avg tool calls | {aggregate.get('avg_tool_calls', 0):.1f} |",
        f"| Stuck rate | {aggregate.get('stuck_rate', 0):.1%} |",
        f"| Tokens (in/out) | {aggregate.get('total_tokens_input', 0):,} / {aggregate.get('total_tokens_output', 0):,} |",
        "",
    ]
    content = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    return content


def merge(input_dirs: list[Path], output_dir: Path) -> dict:
    """Top-level merge entrypoint.

    1. Discover valid shard directories.
    2. Copy all trials into merged output.
    3. Merge run_state.json files.
    4. Re-aggregate metrics from trial files.
    5. Write combined aggregate.json, run_state.json, and summary.md.

    Returns:
        The aggregate metrics dict.
    """
    shard_dirs = discover_shard_dirs(input_dirs)

    if not shard_dirs:
        raise ValueError(
            f"No valid shard output directories found in: {[str(d) for d in input_dirs]}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy trials
    copy_trials(shard_dirs, output_dir)

    # 2. Merge run states
    merged_state = merge_run_states(shard_dirs)
    merged_state["output_dir"] = str(output_dir)
    with open(output_dir / "run_state.json", "w") as f:
        json.dump(merged_state, f, indent=2, default=str)

    # 3. Aggregate from copied trial files
    aggregate = aggregate_metrics(output_dir)
    with open(output_dir / "aggregate.json", "w") as f:
        json.dump(aggregate, f, indent=2)

    # 4. Summary
    write_summary_markdown(aggregate, output_dir / "summary.md")

    return aggregate
