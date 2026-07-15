# Tolokaforge

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
pip install tolokaforge                # library only (headless / server)
pip install "tolokaforge[dx]"          # + terminal CLI (Rich panels, banners)
pip install "tolokaforge[browser]"     # + Playwright
pip install "tolokaforge[all]"         # everything
```

The `[dx]` extras install the terminal front-end that owns the `tolokaforge` CLI — Rich panels, banners, and the Click command tree. Without them the library still imports (`from tolokaforge.core.orchestrator import Orchestrator`), and the `tolokaforge` console script prints an install hint pointing at `pip install 'tolokaforge[dx]'`. Front-end pluggability is recorded in [ADR-0019](docs/architecture/adr/0019-front-end-plugin-namespace.md).

Dev install:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
uv tool install --editable '.[dx]' --python 3.12   # exposes `tolokaforge` on PATH
```

The last line installs the `tolokaforge` command globally (into `~/.local/bin/`). `--editable` keeps it pointing at your working tree so `git pull` updates it. All examples below assume `tolokaforge` is on PATH; if you skip the install step, prefix every command with `uv run` (e.g. `uv run tolokaforge run …`).

See [Python Package Guide](docs/PYTHON_PACKAGE.md) for all extras and programmatic API usage.

## Quick Start

```bash
# 1. Configure provider keys
cp .env.example .env

# 2. Run one of the included examples
tolokaforge run --config examples/native/coding/run_config.yaml

# 3. Check results
tolokaforge status --run-dir results/coding_example
tolokaforge analyze --trajectory results/coding_example/trials/<task_id>/0/trajectory.yaml
```

Running `tolokaforge` with no subcommand drops into an interactive shell with tab-completion of every subcommand and flag — see [docs/CLI.md](docs/CLI.md) § Interactive shell.

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
tolokaforge run --config my_run.yaml
```

Every example under [`examples/`](examples/) ships a `run_config.yaml` next to its
task data. There is no global "default" config — the run config and the tasks it
points at always travel together.

For distributed execution and advanced workflows see the [Runner Guide](docs/RUNNER.md).

## Runtime modes

Every trial runs inside a Docker stack. Two runtime backends decide how
that stack is scoped:

- **`shared`** (default): one stack materialises at run start and every
  trial hits it. Fast startup, lowest overhead.
- **`per_trial`**: a fresh stack materialises for each trial via
  [Testcontainers](https://testcontainers.com/). Strict isolation —
  DB rows, filesystem edits, service state never leak across trials.
  Use it whenever a task mutates its environment.

Pick per-run via the CLI flag or the config key:

```bash
tolokaforge run --config my_run.yaml --runtime per_trial
```

```yaml
# my_run.yaml
orchestrator:
  runtime: per_trial            # or "shared" (default)
```

A task can also declare its requirement via `environment_manifest.isolation`
in `task.yaml` — the orchestrator refuses to run a `per_trial` task on the
shared backend so cross-trial contamination is a startup-time error, not a
silent grading bug.

Read [docs/guides/isolated_trials.md](docs/guides/isolated_trials.md) for
a walkthrough (when to use each mode, how to opt in, cost tradeoffs), or
[docs/architecture/RUNTIME_BACKENDS.md](docs/architecture/RUNTIME_BACKENDS.md)
for the full lifecycle deep-dive.

## Multi-container tasks

Tasks aren't limited to the engine's built-in stack (runner + db-service ±
RAG ± mock-web). A task can declare its own `docker-compose.yaml` — the
task pack ships the compose file next to `task.yaml`, and the engine
materialises that stack instead. Real databases, real APIs, custom
services, whatever the compose file names.

```yaml
# task.yaml
environment_manifest:
  compose_file: "./environment.compose.yaml"
  runner_service: "runner"
  isolation: "shared_ok"        # or "per_trial"
```

The [`multi_service`](examples/native/multi_service/) example is the
smallest working demonstration (runner + db-service + an nginx serving a
product catalog); its two siblings scale up to a multi-endpoint join and a
full PostgREST + postgres three-tier stack.

Read [docs/guides/multi_container_tasks.md](docs/guides/multi_container_tasks.md)
for a walkthrough, or [ADR-0018](docs/architecture/adr/0018-multi-container-under-shared-runtime.md)
for the case-matrix that decides which mode fits your task.

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
│   ├── multi_service/          # task-declared compose (nginx catalog)
│   ├── multi_service_advanced/ # multi-endpoint join
│   ├── multi_service_postgres/ # PostgREST + postgres three-tier
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
| Runtime backends (shared vs per-trial) | [docs/architecture/RUNTIME_BACKENDS.md](docs/architecture/RUNTIME_BACKENDS.md) |
| Isolated trials guide | [docs/guides/isolated_trials.md](docs/guides/isolated_trials.md) |
| Multi-container task guide | [docs/guides/multi_container_tasks.md](docs/guides/multi_container_tasks.md) |
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

Single-container tasks — one runner container plus the engine's built-in
services (db-service, mock-web, RAG on demand):

| Example | Description |
| --- | --- |
| [`examples/native/coding/`](examples/native/coding/) | Simplest native pattern — file-write grading |
| [`examples/native/tool_use/`](examples/native/tool_use/) | Structured tool-call grading |
| [`examples/native/browser_task/`](examples/native/browser_task/) | Browser tool against mock-web fixtures |
| [`examples/native/native_shared_domain/`](examples/native/native_shared_domain/) | `_shared/domain.yaml` + FastMCP pattern |
| [`examples/terminal_bench/`](examples/terminal_bench/) | Docker-compose stacks with `terminal_bench` adapter |

Multi-container tasks — the task ships its own compose file declaring
additional services (see [multi_container_tasks.md](docs/guides/multi_container_tasks.md)):

| Example | Description |
| --- | --- |
| [`examples/native/multi_service/`](examples/native/multi_service/) | Smallest multi-service task — runner + db-service + nginx product catalog |
| [`examples/native/multi_service_advanced/`](examples/native/multi_service_advanced/) | Multi-endpoint aggregation across two task-specific HTTP APIs |
| [`examples/native/multi_service_postgres/`](examples/native/multi_service_postgres/) | Realistic three-tier stack — PostgREST + postgres, no application code in the task pack |

## Testing

```bash
make test              # all tests
make test-unit         # fast, isolated
make test-functional   # mocked externals
```

See [tests/README.md](tests/README.md) for integration/E2E tests and contribution guidelines.

## License

Apache-2.0 — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Citation

Use [CITATION.cff](CITATION.cff) or [CITATION.bib](CITATION.bib) when referencing Tolokaforge in papers or reports.
