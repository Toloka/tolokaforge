# Tolokaforge

Created by **Renaud de la Gueronniere** | **Toloka AI**

A benchmarking harness for evaluating tool-using LLM agents. Multi-turn agent/user loops, sandboxed execution, deterministic grading, and rich telemetry — across any provider via LiteLLM.

## Highlights

- **Agent + User Loop** – Multi-turn conversations where both agent and user models call tools.
- **Sandboxed Execution** – Tool calls proxy into Dockerized services with no external network access.
- **MCP-Compatible Tooling** – Tasks declare tools via Model Context Protocol or built-ins.
- **Deterministic Grading** – JSONPath assertions, state hashes, transcript rules, optional LLM judges.
- **Rich Metrics** – pass@k, cost/token estimates, latency percentiles, failure attribution.
- **Distributed Runner** – SQLite for local runs, Postgres for multi-machine execution.
- **Bring-Your-Own Models** – Any provider supported by LiteLLM (OpenAI, Anthropic, Google, Azure, Bedrock, Ollama, OpenRouter, and more).

## Installation

```bash
pip install tolokaforge                # core
pip install "tolokaforge[browser]"     # + Playwright
pip install "tolokaforge[all]"         # everything
```

Dev install:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

See [Python Package Guide](docs/PYTHON_PACKAGE.md) for all extras and programmatic API usage.

## Quick Start

```bash
# 1. Configure provider keys
cp .env.example .env

# 2. Run one of the included examples
uv run tolokaforge run --config examples/native/coding/run_config.yaml

# 3. Check results
uv run tolokaforge status --run-dir results/coding_example
uv run tolokaforge analyze --trajectory results/coding_example/trials/<task_id>/0/trajectory.yaml
```

That's it. Docker services for browser / mobile / RAG tasks start automatically via
[`auto_start_services`](tolokaforge/core/models.py) (default: `true`).

### What is a run config?

A run config (e.g. `examples/native/coding/run_config.yaml`) is a single YAML file
that fully specifies an evaluation. The harness reads it and runs the benchmark:

```yaml
models:                       # which LLM(s) drive the agent + user simulator
  agent:    {provider: openrouter, name: anthropic/claude-sonnet-4-6, ...}
  user:     {provider: openrouter, name: anthropic/claude-sonnet-4-6, ...}

orchestrator:                 # how the run is executed
  workers: 4                  # parallel trials
  repeats: 1                  # trials per task
  max_turns: 20

evaluation:                   # what to evaluate
  tasks_glob: "**/task.yaml"  # which tasks to load (relative to task_packs root or repo)
  task_packs:                 # optional: directories that contain task.yaml files
    - "examples/native/coding/dataset"
  output_dir: "results/coding_example"
```

To write your own benchmark, copy a working example as a starting point:

```bash
cp examples/native/coding/run_config.yaml my_run.yaml
$EDITOR my_run.yaml         # change model, tasks_glob, output_dir
uv run tolokaforge run --config my_run.yaml
```

Every example under [`examples/`](examples/) ships a `run_config.yaml` next to its
task data. There is no global "default" config — the run config and the tasks it
points at always travel together.

For distributed execution and advanced workflows see the [Runner Guide](docs/RUNNER.md).

## Project Structure

```
tolokaforge/          # Installable Python package
├── cli/              # CLI commands (run, validate, status, analyze)
├── core/             # Orchestration, grading, metrics, queue
├── tools/            # Built-in + MCP tool system
└── env/              # Environment services (JSON DB, mock web, RAG)
examples/             # Reference task layouts with runnable run_config.yaml
├── native/           # default `native` adapter
│   ├── browser_task/
│   ├── coding/
│   ├── native_shared_domain/
│   └── tool_use/
└── terminal_bench/   # `terminal_bench` adapter (Docker compose)
```

## Documentation

| Topic | Link |
| --- | --- |
| Getting started | [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) |
| Task authoring | [docs/TASKS.md](docs/TASKS.md) |
| Grading system | [docs/GRADING.md](docs/GRADING.md) |
| Tool reference | [docs/TOOLS.md](docs/TOOLS.md) |
| Browser/mobile tools | [docs/BROWSER_TOOLS.md](docs/BROWSER_TOOLS.md) |
| Runner & distributed execution | [docs/RUNNER.md](docs/RUNNER.md) |
| Adapter architecture | [docs/ADAPTER_ARCHITECTURE.md](docs/ADAPTER_ARCHITECTURE.md) |
| Analytics & failure attribution | [docs/ANALYTICS.md](docs/ANALYTICS.md) |
| Python package API | [docs/PYTHON_PACKAGE.md](docs/PYTHON_PACKAGE.md) |
| Task packs | [docs/TASK_PACKS.md](docs/TASK_PACKS.md) |
| Configuration reference | [docs/REFERENCE.md](docs/REFERENCE.md) |
| Security model | [docs/SECURITY.md](docs/SECURITY.md) |
| Docker runtime | [docs/BENCHMARK_BACKEND_DESIGNS.md](docs/BENCHMARK_BACKEND_DESIGNS.md) |
| Benchmark types | [docs/BENCHMARK_TYPES.md](docs/BENCHMARK_TYPES.md) |
| Testing guide | [tests/README.md](tests/README.md) |

## Examples

| Example | Description |
| --- | --- |
| [`examples/native/coding/`](examples/native/coding/) | Simplest native pattern — file-write grading |
| [`examples/native/tool_use/`](examples/native/tool_use/) | Structured tool-call grading |
| [`examples/native/browser_task/`](examples/native/browser_task/) | Browser tool against mock-web fixtures |
| [`examples/native/native_shared_domain/`](examples/native/native_shared_domain/) | `_shared/domain.yaml` + FastMCP pattern |
| [`examples/terminal_bench/`](examples/terminal_bench/) | Docker-compose stacks with `terminal_bench` adapter |

## Testing

```bash
make test              # all tests
make test-unit         # fast, isolated
make test-functional   # mocked externals
```

See [tests/README.md](tests/README.md) for integration/E2E tests and contribution guidelines.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Citation

Use [CITATION.cff](CITATION.cff) or [CITATION.bib](CITATION.bib) when referencing Tolokaforge in papers or reports.
