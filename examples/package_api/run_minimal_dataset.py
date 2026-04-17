"""Programmatic Tolokaforge example using a minimal external dataset."""

from __future__ import annotations

import argparse
from pathlib import Path

from tolokaforge import Orchestrator, RunConfig


def build_config(dataset_root: Path, output_dir: Path, provider: str, model_name: str) -> RunConfig:
    return RunConfig(
        models={
            "agent": {
                "provider": provider,
                "name": model_name,
                "temperature": 0.0,
            },
            "user": {
                "provider": provider,
                "name": model_name,
                "temperature": 0.0,
            },
        },
        orchestrator={
            "workers": 1,
            "repeats": 1,
            "runtime": "docker",
            "max_turns": 8,
            "max_attempt_retries": 0,
            "queue_backend": "sqlite",
        },
        evaluation={
            "task_packs": [str(dataset_root)],
            "tasks_glob": "**/task.yaml",
            "output_dir": str(output_dir),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).resolve().parent / "minimal_dataset"),
        help="Path to external dataset root",
    )
    parser.add_argument(
        "--provider",
        default="mock",
        help="LiteLLM provider name (for example: mock, openrouter, openai, anthropic)",
    )
    parser.add_argument(
        "--model",
        default="mock-agent",
        help="Model name passed to LiteLLM (default: mock-agent for offline use)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional output directory (default: results/package_api_<timestamp>)",
    )
    args = parser.parse_args()

    dataset_root = Path(args.dataset).resolve()
    if not dataset_root.exists():
        raise FileNotFoundError(f"Dataset path does not exist: {dataset_root}")

    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else Path("results/package_api").resolve()
    )

    config = build_config(
        dataset_root=dataset_root,
        output_dir=output_dir,
        provider=args.provider,
        model_name=args.model,
    )
    orchestrator = Orchestrator(config)
    orchestrator.load_tasks()
    if not orchestrator.tasks:
        raise RuntimeError("No tasks discovered in provided dataset.")

    orchestrator.run()
    if not orchestrator.tasks:
        raise RuntimeError("No tasks ran.")

    actual_output_dir = (
        orchestrator.state_manager.output_dir
        if getattr(orchestrator, "state_manager", None)
        else output_dir
    )
    results = getattr(orchestrator, "results", None)
    failed_trials = [
        t for t in (results or []) if not (getattr(t, "grade", None) and t.grade.binary_pass)
    ]
    print(f"Run complete. Output directory: {actual_output_dir}")
    if failed_trials:
        print(f"FAILED: {len(failed_trials)} trial(s) did not pass grading.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
