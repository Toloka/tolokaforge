"""Batch recording: generate demo videos from all trajectories in a run directory.

Discovers ``trials/*/0/trajectory.yaml`` files under the given run directory and
records each one in parallel using the single-trajectory recorder.
"""

from __future__ import annotations

import concurrent.futures
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class JobResult:
    task_id: str
    ok: bool
    output: Path
    error: str = ""


def find_latest_run(prefix: str = "mobile_run_") -> Path | None:
    results = Path("results")
    if not results.exists():
        return None
    runs = sorted([p for p in results.iterdir() if p.is_dir() and p.name.startswith(prefix)])
    return runs[-1] if runs else None


def collect_jobs(run_dir: Path) -> list[tuple[str, Path]]:
    jobs: list[tuple[str, Path]] = []
    for trajectory in sorted(run_dir.glob("trials/*/0/trajectory.yaml")):
        task_id = trajectory.parts[-3]
        jobs.append((task_id, trajectory))
    return jobs


def run_one(
    task_id: str,
    trajectory: Path,
    output_dir: Path,
    frames_root: Path,
    keep_frames: bool = False,
    frames_only: bool = False,
) -> JobResult:
    frames_dir = frames_root / task_id
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    output = output_dir / f"{task_id}.mp4"
    cmd = [
        "uv",
        "run",
        "demo-recorder",
        "record",
        "--trajectory",
        str(trajectory),
        "--frames-dir",
        str(frames_dir),
        "--output",
        str(output),
    ]
    if frames_only:
        cmd.append("--frames-only")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as exc:
        return JobResult(task_id=task_id, ok=False, output=output, error=str(exc))
    finally:
        # Best-effort cleanup to avoid large temp growth when frames are not needed.
        if frames_dir.exists() and not keep_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return JobResult(task_id=task_id, ok=False, output=output, error=err)

    return JobResult(task_id=task_id, ok=True, output=output)


def record_all(
    run_dir: Path | None = None,
    output_dir: Path = Path("demos"),
    workers: int = 8,
    prefix: str = "mobile_run_",
    frames_root: Path = Path("scratchpad") / "demo_frames",
    keep_frames: bool = False,
    frames_only: bool = False,
) -> int:
    """Batch-record demos for every trajectory found under *run_dir*.

    Returns an exit code: 0 on full success, 1 if no runs/trajectories found,
    2 if some tasks failed.
    """
    resolved_run_dir = run_dir or find_latest_run(prefix)
    if not resolved_run_dir or not resolved_run_dir.exists():
        print("No run directory found. Pass --run-dir explicitly.")
        return 1

    jobs = collect_jobs(resolved_run_dir)
    if not jobs:
        print(f"No trajectories found under {resolved_run_dir}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    frames_root.mkdir(parents=True, exist_ok=True)

    print(f"Run dir: {resolved_run_dir}")
    print(f"Tasks: {len(jobs)}")
    print(f"Output dir: {output_dir}")
    print(f"Frames root: {frames_root}")
    print(f"Workers: {workers}")

    results: list[JobResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {
            ex.submit(
                run_one,
                task_id,
                trajectory,
                output_dir,
                frames_root,
                keep_frames,
                frames_only,
            ): task_id
            for task_id, trajectory in jobs
        }
        total = len(futs)
        for done, fut in enumerate(concurrent.futures.as_completed(futs), start=1):
            res = fut.result()
            results.append(res)
            status = "OK" if res.ok else "FAIL"
            print(f"[{done}/{total}] {status} {res.task_id}")
            if not res.ok and res.error:
                print(f"  error: {res.error[:400]}")

    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    print(f"Completed: {len(ok)} ok, {len(bad)} failed")
    if bad:
        print("Failed tasks:")
        for r in sorted(bad, key=lambda x: x.task_id):
            print(f"- {r.task_id}")
        return 2

    return 0
