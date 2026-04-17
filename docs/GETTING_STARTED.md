# Getting Started with Tolokaforge

This guide walks you through installing Tolokaforge, running a simple evaluation, and understanding the output.

For benchmark types and roadmap, see:
- `docs/BENCHMARK_TYPES.md`
- `docs/FUTURE_DEVELOPMENT.md`

## Prerequisites

- Python 3.10+
- A model API key (OpenAI, Anthropic, Google, OpenRouter, etc.)
- Docker (required — the only supported runtime for tool execution)

## Installation

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# For browser/mobile tools
uv run playwright install --with-deps chromium
```

PyPI install (without cloning repo):

```bash
pip install tolokaforge
pip install "tolokaforge[all]"  # optional full feature set
```

## Configure API Keys

Create `.env` in the repo root (or copy from `.env.example`):

```bash
cp .env.example .env
OPENROUTER_API_KEY=sk-or-...
# or OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY
```

## Start Environment Services (Recommended)

Browser, JSON DB, and RAG tasks rely on services. Start them with Docker:

```bash
docker compose up -d json-db mock-web rag-service
```

## Quick Start Run

Use one of the example configs in `examples/` or create a minimal one:

```yaml
models:
  agent:
    provider: "openai"
    name: "gpt-4o-mini"
    temperature: 0.0
  user:
    provider: "openai"
    name: "gpt-4o-mini"
    temperature: 0.3

orchestrator:
  workers: 1
  repeats: 1
  max_budget_usd: 5.0
  max_requests_per_second: 1.0
  max_attempt_retries: 1
  max_turns: 20

evaluation:
  tasks_glob: "examples/browser_task/dataset/tasks/**/task.yaml"
  output_dir: "results/quick_start"
```

Run it:

```bash
uv run tolokaforge run --config examples/browser_task/run_config.yaml
```

Check run progress/cost at any time:

```bash
uv run tolokaforge status --run-dir results/quick_start_<timestamp>
```

## External Task Packs

You can load tasks from external benchmark packs without copying them into this repo:

```yaml
evaluation:
  task_packs:
    - "/abs/path/private-pack-core"
    - "/abs/path/private-pack-mobile"
  tasks_glob: "**/task.yaml"
  output_dir: "results/task_pack_run"
```

Notes:
- Relative `tasks_glob` is evaluated under each `task_packs` root.
- In Docker mode, generate a compose override from your run config:

```bash
uv run python scripts/generate_task_pack_compose_override.py \
  --config my_run_config.yaml \
  --output docker-compose.taskpacks.override.yaml
```

For distributed queue workers (shared queue + shared artifacts path), run:

```bash
uv run tolokaforge prepare --config my_run_config.yaml --run-dir results/distributed_run --reset-queue
uv run tolokaforge worker --config my_run_config.yaml --run-dir results/distributed_run
```

For distributed execution with multiple machines, use a shared Postgres queue backend
by setting `queue_postgres_dsn` in your run config.

## Benchmark Type Requirements

| Benchmark type | Typical requirements |
| --- | --- |
| Knowledge/reasoning | API key only (single-turn) or API key + orchestrator (multi-turn) |
| Tool-use | API key + services as required by tools (often Docker + JSON DB) |
| Coding / STEM | API key + Docker/container runtime |
| Terminal-use | API key + sandboxed shell runtime (often Docker) |
| Browser-use | API key + Playwright + environment services |
| Mobile-use | API key + Playwright + mock-web/DB services |
| Long-horizon docs | API key + RAG service (+ LibreOffice headless for GDPval-style office document workflows) |
| Deep research | API key + controlled mock-web + search/index + rubric scorer |

## Output Structure

Results are written under `evaluation.output_dir` using split files:

```
results/quick_start/
├── trials/
│   └── browser_simple_navigation/
│       └── 0/
│           ├── task.yaml
│           ├── trajectory.yaml
│           ├── env.yaml
│           ├── metrics.yaml
│           ├── grade.yaml
│           └── logs.yaml
├── summary.csv
└── metrics.json
```

Key files:
- `trajectory.yaml`: conversation + tool calls
- `env.yaml`: final environment state
- `grade.yaml`: grading results

## Next Steps

- Read `docs/CONFIG.md` for full configuration options
- Read `docs/TASKS.md` to author tasks
- Read `docs/TOOLS.md` for tool details
- Read `docs/PYTHON_PACKAGE.md` for import-based usage patterns
- Read `docs/RUNNER.md` for queue/distributed execution
- Try `python examples/package_api/run_minimal_task_pack.py` for programmatic usage
- Try `python examples/analyze_results/analyze_run.py --run-dir <run_dir>` for programmatic analysis
- Use `tolokaforge validate --tasks ".../task.yaml"` to validate tasks
