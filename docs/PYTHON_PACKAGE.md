# Python Package Usage

Tolokaforge supports both CLI-driven runs and programmatic runs via Python imports.

## Install

Core package:

```bash
pip install tolokaforge
```

Common extras:

```bash
pip install "tolokaforge[browser]"   # Playwright + Pillow
pip install "tolokaforge[docker]"    # Docker + gRPC runtime
pip install "tolokaforge[server]"    # FastAPI/Uvicorn services
pip install "tolokaforge[rag]"       # BM25 RAG search
pip install "tolokaforge[all]"       # Full feature set
```

## Public Python API

```python
from tolokaforge import (
    Orchestrator,
    RunConfig,
    TaskConfig,
    Trajectory,
    Grade,
    compute_pass_at_k,
    calculate_task_metrics,
    calculate_aggregate_metrics,
    attribute_failure,
    create_run_queue,
)
```

## Programmatic Run Example

Use the runnable example script:

```bash
python examples/package_api/run_minimal_task_pack.py \
  --provider openrouter \
  --model openrouter/openai/gpt-4o-mini
```

This script:
1. Uses an external task pack (`examples/package_api/minimal_task_pack`)
2. Constructs `RunConfig` in Python
3. Runs the harness through `Orchestrator`
4. Writes results under `results/package_api_<timestamp>`

## Programmatic Analysis Example

Analyze an existing run directory with exported APIs:

```bash
python examples/analyze_results/analyze_run.py --run-dir results/package_api_YYYYMMDD_HHMMSS
```

This writes `analysis_summary.json` with:
1. Per-task metrics
2. Aggregate run metrics (cost/tokens/latency/pass@k)
3. Failure attribution summary

For distributed queue workflow examples, see `examples/distributed_run/README.md`.

## Minimal Inline Example

```python
from pathlib import Path

from tolokaforge import Orchestrator, RunConfig

config = RunConfig(
    models={
        "agent": {
            "provider": "openrouter",
            "name": "openrouter/openai/gpt-4o-mini",
            "temperature": 0.0,
        }
    },
    orchestrator={
        "workers": 1,
        "repeats": 1,
        "runtime": "docker",
        "queue_backend": "sqlite",
    },
    evaluation={
        "task_packs": [str(Path("examples/package_api/minimal_task_pack").resolve())],
        "tasks_glob": "**/task.yaml",
        "output_dir": "results/package_inline_example",
    },
)

orch = Orchestrator(config)
orch.load_tasks()
orch.run()
```

## Notes

1. Package wheels exclude bundled benchmark tasks; use `evaluation.task_packs` for public/private content.
2. Browser/mobile tools require the `browser` extra.
3. Docker is the only supported runtime; install the `docker` extra (`pip install "tolokaforge[docker]"`).
4. For queue/distributed execution details, see `docs/RUNNER.md`.
