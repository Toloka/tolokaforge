"""Tests for eval_orchestrator.merger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from eval_orchestrator.merger import (
    aggregate_metrics,
    copy_trials,
    discover_shard_dirs,
    merge,
    merge_run_states,
    write_summary_markdown,
)

pytestmark = pytest.mark.unit


def _make_trial(
    trial_dir: Path,
    binary_pass: bool = True,
    cost: float = 0.10,
    tokens_in: int = 500,
    tokens_out: int = 200,
    latency: float = 5.0,
    turns: int = 8,
    tool_calls: int = 3,
    stuck: bool = False,
) -> None:
    """Write grade.yaml and metrics.yaml into a trial directory."""
    trial_dir.mkdir(parents=True, exist_ok=True)

    grade = {"binary_pass": binary_pass, "score": 1.0 if binary_pass else 0.0}
    if stuck:
        grade["stuck"] = True
    (trial_dir / "grade.yaml").write_text(yaml.dump(grade))

    metrics = {
        "cost_usd_est": cost,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "latency_total_s": latency,
        "turns": turns,
        "tool_calls": tool_calls,
    }
    (trial_dir / "metrics.yaml").write_text(yaml.dump(metrics))

    # Minimal trajectory
    (trial_dir / "trajectory.yaml").write_text(yaml.dump({"steps": []}))


def _make_run_state(
    shard_dir: Path,
    trials: dict,
    completed: int = 0,
    failed: int = 0,
) -> None:
    """Write a run_state.json into a shard directory."""
    state = {
        "run_id": "test",
        "config_path": "test.yaml",
        "output_dir": str(shard_dir),
        "start_ts": "2025-01-01T00:00:00+00:00",
        "last_updated": "2025-01-01T01:00:00+00:00",
        "status": "completed",
        "total_trials": len(trials),
        "completed_trials": completed,
        "failed_trials": failed,
        "trials": trials,
    }
    (shard_dir / "run_state.json").write_text(json.dumps(state))


@pytest.fixture()
def two_shards(tmp_path: Path) -> tuple[Path, Path]:
    """Create two shard output directories with sample trials."""
    shard_0 = tmp_path / "shard-0"
    _make_trial(shard_0 / "trials" / "TASK-001" / "0", binary_pass=True, cost=0.10)
    _make_trial(shard_0 / "trials" / "TASK-002" / "0", binary_pass=False, cost=0.20)
    _make_run_state(
        shard_0,
        {
            "TASK-001:0": {
                "task_id": "TASK-001",
                "trial_index": 0,
                "status": "completed",
                "binary_pass": True,
                "score": 1.0,
            },
            "TASK-002:0": {
                "task_id": "TASK-002",
                "trial_index": 0,
                "status": "completed",
                "binary_pass": False,
                "score": 0.0,
            },
        },
        completed=2,
        failed=0,
    )

    shard_1 = tmp_path / "shard-1"
    _make_trial(shard_1 / "trials" / "TASK-003" / "0", binary_pass=True, cost=0.15)
    _make_trial(
        shard_1 / "trials" / "TASK-004" / "0",
        binary_pass=True,
        cost=0.05,
        stuck=True,
    )
    _make_run_state(
        shard_1,
        {
            "TASK-003:0": {
                "task_id": "TASK-003",
                "trial_index": 0,
                "status": "completed",
                "binary_pass": True,
                "score": 1.0,
            },
            "TASK-004:0": {
                "task_id": "TASK-004",
                "trial_index": 0,
                "status": "completed",
                "binary_pass": True,
                "score": 1.0,
            },
        },
        completed=2,
        failed=0,
    )

    return shard_0, shard_1


# ---------- discover_shard_dirs ----------


def test_discover_filters_invalid(tmp_path: Path) -> None:
    valid = tmp_path / "good"
    (valid / "trials").mkdir(parents=True)

    invalid = tmp_path / "bad"
    invalid.mkdir()

    nonexistent = tmp_path / "missing"

    dirs = discover_shard_dirs([valid, invalid, nonexistent])
    # "bad" has no trials/ or run_state.json => excluded
    assert dirs == [valid]


def test_discover_accepts_run_state_only(tmp_path: Path) -> None:
    d = tmp_path / "has_state"
    d.mkdir()
    (d / "run_state.json").write_text("{}")
    assert discover_shard_dirs([d]) == [d]


# ---------- copy_trials ----------


def test_copy_trials(two_shards: tuple[Path, Path], tmp_path: Path) -> None:
    shard_0, shard_1 = two_shards
    output = tmp_path / "merged"

    count = copy_trials([shard_0, shard_1], output)

    assert count == 4
    assert (output / "trials" / "TASK-001" / "0" / "grade.yaml").exists()
    assert (output / "trials" / "TASK-003" / "0" / "metrics.yaml").exists()


# ---------- merge_run_states ----------


def test_merge_run_states(two_shards: tuple[Path, Path]) -> None:
    shard_0, shard_1 = two_shards
    merged = merge_run_states([shard_0, shard_1])

    assert merged["total_trials"] == 4
    assert merged["completed_trials"] == 4
    assert merged["failed_trials"] == 0
    assert "TASK-001:0" in merged["trials"]
    assert "TASK-004:0" in merged["trials"]


# ---------- aggregate_metrics ----------


def test_aggregate_metrics(two_shards: tuple[Path, Path], tmp_path: Path) -> None:
    shard_0, shard_1 = two_shards
    output = tmp_path / "merged"
    copy_trials([shard_0, shard_1], output)

    agg = aggregate_metrics(output)

    assert agg["total_trials"] == 4
    assert agg["total_tasks"] == 4
    assert agg["passed"] == 3  # TASK-002 failed
    assert agg["success_rate_micro"] == pytest.approx(0.75, abs=0.01)
    assert agg["total_cost_usd"] == pytest.approx(0.50, abs=0.01)
    assert agg["stuck_rate"] == pytest.approx(0.25, abs=0.01)  # TASK-004 stuck


def test_aggregate_empty_output(tmp_path: Path) -> None:
    agg = aggregate_metrics(tmp_path)
    assert agg == {}


# ---------- write_summary_markdown ----------


def test_summary_markdown(tmp_path: Path) -> None:
    agg = {
        "total_trials": 10,
        "total_tasks": 5,
        "success_rate_micro": 0.80,
        "passed": 8,
        "total_cost_usd": 1.23,
        "avg_cost_per_task": 0.123,
        "avg_latency_s": 4.5,
        "avg_turns": 7.2,
        "avg_tool_calls": 3.1,
        "stuck_rate": 0.10,
        "total_tokens_input": 10000,
        "total_tokens_output": 5000,
    }
    md = write_summary_markdown(agg, tmp_path / "summary.md")

    assert "80.0%" in md
    assert "8 / 10" in md
    assert "$1.23" in md
    assert (tmp_path / "summary.md").exists()


# ---------- merge (integration) ----------


def test_merge_end_to_end(two_shards: tuple[Path, Path], tmp_path: Path) -> None:
    shard_0, shard_1 = two_shards
    output = tmp_path / "merged"

    agg = merge(input_dirs=[shard_0, shard_1], output_dir=output)

    # Aggregate
    assert agg["total_trials"] == 4
    assert agg["passed"] == 3

    # Files written
    assert (output / "run_state.json").exists()
    assert (output / "aggregate.json").exists()
    assert (output / "summary.md").exists()

    # Verify aggregate.json matches
    loaded = json.loads((output / "aggregate.json").read_text())
    assert loaded["total_trials"] == 4

    # Verify run_state.json
    state = json.loads((output / "run_state.json").read_text())
    assert len(state["trials"]) == 4


def test_merge_raises_on_no_valid_dirs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No valid shard"):
        merge(input_dirs=[tmp_path / "nonexistent"], output_dir=tmp_path / "out")
