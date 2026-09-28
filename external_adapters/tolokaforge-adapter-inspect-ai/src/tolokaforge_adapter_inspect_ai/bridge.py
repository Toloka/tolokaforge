"""Local (non-Docker) delegation path: run ``inspect eval`` and collect its log.

tolokaforge shells out to ``inspect eval``; Inspect executes the task on its own
runtime (solvers, scorers, sandbox) and writes a ``.eval`` log, which
:mod:`tolokaforge_adapter_inspect_ai.normalize` reads back into tolokaforge types.
Model routing is the caller's responsibility: pass an ``env`` mapping (e.g. an
OpenAI-compatible base URL + key for a LiteLLM/OpenRouter gateway). This module
never reads secrets from the process environment itself.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class InspectRunResult:
    """Outcome of one ``inspect eval`` subprocess."""

    log_path: Path | None
    returncode: int
    stdout: str
    stderr: str


def build_eval_command(
    *,
    task_file: str | Path,
    task_name: str,
    model: str,
    log_dir: str | Path,
    sample_id: str | None = None,
    limit: int | None = None,
    extra_args: Sequence[str] | None = None,
    executable: str = "inspect",
) -> list[str]:
    """Assemble the ``inspect eval`` argv for one task."""
    cmd = [
        executable,
        "eval",
        f"{task_file}@{task_name}",
        "--model",
        model,
        "--log-dir",
        str(log_dir),
        "--log-format",
        "eval",
    ]
    if sample_id is not None:
        cmd += ["--sample-id", str(sample_id)]
    if limit is not None:
        cmd += ["--limit", str(limit)]
    if extra_args:
        cmd += list(extra_args)
    return cmd


def run_inspect_eval(
    *,
    task_file: str | Path,
    task_name: str,
    model: str,
    log_dir: str | Path,
    sample_id: str | None = None,
    limit: int | None = None,
    env: Mapping[str, str] | None = None,
    extra_args: Sequence[str] | None = None,
    executable: str = "inspect",
    timeout: float | None = None,
) -> InspectRunResult:
    """Run one Inspect task and return the path to the ``.eval`` log it produced."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    before = set(log_dir.glob("*.eval"))

    cmd = build_eval_command(
        task_file=task_file,
        task_name=task_name,
        model=model,
        log_dir=log_dir,
        sample_id=sample_id,
        limit=limit,
        extra_args=extra_args,
        executable=executable,
    )
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=dict(env) if env is not None else None,
        timeout=timeout,
        check=False,
    )

    produced = sorted(set(log_dir.glob("*.eval")) - before, key=lambda p: p.stat().st_mtime)
    log_path = produced[-1] if produced else None
    return InspectRunResult(
        log_path=log_path,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
