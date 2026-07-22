"""End-user shape of a `tolokaforge run-trial` driver — model + task sweep.

Runs both bundled `tool_use` tasks (`tool_use_public_example_01` and
`tool_use_public_example_02`) against two agent models through the
`tolokaforge run-trial` subprocess CLI. Each trial is an isolated subprocess —
a crash in one leaves the others untouched. Errors are dispatched by
`error_type` rather than caught as tracebacks. At the end, a small
comparison table lands on stdout: per-task, per-model score, cost, and
wall-clock.

The pattern this demonstrates: taking `run-trial` beyond the "hello world"
one-line-of-JSON case into the shape of a real driver — one that has to
loop, aggregate, and handle both the happy path and typed failure paths.
Compare with `drive_run_trial.py`, which is the minimal version.

Needs an LLM key in `.env` (like every real run) and a live runner
(`make docker-up`). Then, from the repo root::

    uv run python examples/run-trial/drive_run_trial_sweep.py

Set `TOLOKAFORGE_SWEEP_MODELS` to a comma-separated list to override the
model choices, e.g. ``TOLOKAFORGE_SWEEP_MODELS=openai/gpt-4o`` to run one
model only.
"""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.secrets import init_default

_LLM_KEYS = [
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEYS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
]

_TASK_ROOT = Path(__file__).resolve().parents[1] / "native/tool_use/dataset/tasks/tool_use"

_TASK_YAMLS = [
    _TASK_ROOT / "tool_use_public_example_01" / "task.yaml",
    _TASK_ROOT / "tool_use_public_example_02" / "task.yaml",
]

_DEFAULT_MODELS = [
    "anthropic/claude-sonnet-4.6",
    "openai/gpt-4o",
]


@dataclass
class TrialOutcome:
    task_id: str
    model: str
    ok: bool
    score: float | None
    cost_usd: float | None
    latency_s: float
    detail: str


def _models_from_env() -> list[str]:
    override = os.environ.get("TOLOKAFORGE_SWEEP_MODELS")
    if not override:
        return _DEFAULT_MODELS
    return [name.strip() for name in override.split(",") if name.strip()]


def _run_single_trial(task_yaml: Path, model: str) -> TrialOutcome:
    task, task_dir = load_task_yaml(task_yaml)

    start = {
        "v": 1,
        "type": "start",
        "task": task.model_dump(mode="json"),
        "models": {
            "agent": {
                "provider": "openrouter",
                "name": model,
                "temperature": 0.0,
            },
        },
        "runtime": "shared",
        "conductor": "in_process",
    }

    began = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "tolokaforge.cli.main", "run-trial"],
        input=json.dumps(start) + "\n",
        cwd=str(task_dir),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - began

    message = _parse_single_message(proc.stdout)
    if message is None:
        return TrialOutcome(
            task_id=task.task_id,
            model=model,
            ok=False,
            score=None,
            cost_usd=None,
            latency_s=elapsed,
            detail=f"no wire output (exit {proc.returncode}); stderr tail: {proc.stderr[-200:].strip()}",
        )

    if message["type"] == "error":
        return TrialOutcome(
            task_id=task.task_id,
            model=model,
            ok=False,
            score=None,
            cost_usd=None,
            latency_s=elapsed,
            detail=f"error [{message['error_type']}]: {message['message']}",
        )

    trajectory = message["result"]["trajectory"]
    metrics = message["result"].get("metrics") or {}
    grade = trajectory.get("grade") or {}
    return TrialOutcome(
        task_id=task.task_id,
        model=model,
        ok=True,
        score=_safe_score(grade),
        cost_usd=_safe_float(metrics.get("cost_usd")),
        latency_s=elapsed,
        detail="ok",
    )


def _parse_single_message(stdout: str) -> dict[str, Any] | None:
    line = next((line for line in stdout.splitlines() if line.strip()), None)
    if line is None:
        return None
    return json.loads(line)


def _safe_score(grade: dict[str, Any]) -> float | None:
    score = grade.get("score")
    return _safe_float(score)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_table(rows: list[TrialOutcome]) -> str:
    header = f"{'task':<34}  {'model':<32}  {'score':>6}  {'cost':>8}  {'latency':>8}  status"
    sep = "-" * len(header)
    lines = [header, sep]
    for row in rows:
        score = f"{row.score:.2f}" if row.score is not None else "  -  "
        cost = f"${row.cost_usd:.4f}" if row.cost_usd is not None else "   -    "
        latency = f"{row.latency_s:>6.1f}s"
        status = "ok" if row.ok else row.detail
        lines.append(
            f"{row.task_id:<34}  {row.model:<32}  {score:>6}  {cost:>8}  {latency:>8}  {status}"
        )
    return "\n".join(lines)


def _summarise(rows: list[TrialOutcome]) -> str:
    by_model: dict[str, list[TrialOutcome]] = {}
    for row in rows:
        by_model.setdefault(row.model, []).append(row)

    lines = ["", "per-model averages:"]
    for model, model_rows in by_model.items():
        oks = [r for r in model_rows if r.ok and r.score is not None]
        if not oks:
            lines.append(f"  {model:<32}  no successful trials")
            continue
        avg_score = sum(r.score or 0.0 for r in oks) / len(oks)
        total_cost = sum((r.cost_usd or 0.0) for r in oks)
        total_latency = sum(r.latency_s for r in oks)
        lines.append(
            f"  {model:<32}  score {avg_score:.2f}  cost ${total_cost:.4f}  "
            f"total latency {total_latency:.1f}s  ({len(oks)}/{len(model_rows)} ok)"
        )
    return "\n".join(lines)


def main() -> None:
    init_default().export_to_environ(_LLM_KEYS)

    models = _models_from_env()
    print(
        f"sweeping {len(_TASK_YAMLS)} tasks x {len(models)} models = {len(_TASK_YAMLS) * len(models)} trials"
    )

    outcomes: list[TrialOutcome] = []
    for task_yaml in _TASK_YAMLS:
        for model in models:
            print(f"  running {task_yaml.parent.name} x {model} ...", flush=True)
            outcomes.append(_run_single_trial(task_yaml, model))

    print()
    print(_format_table(outcomes))
    print(_summarise(outcomes))

    if any(not row.ok for row in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
