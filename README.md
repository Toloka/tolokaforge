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

# 2. Start environment services (browser/mobile/RAG tasks)
docker compose up -d json-db mock-web rag-service

# 3. Run an example benchmark
uv run tolokaforge run --config examples/browser_task/run_config.yaml

# 4. Check results
uv run tolokaforge status --run-dir results/my_run
uv run tolokaforge analyze --trajectory results/my_run/trials/task_id/0/trajectory.yaml
```

For distributed execution, task packs, and advanced workflows see the [Runner Guide](docs/RUNNER.md).

## Project Structure

```
tolokaforge/          # Installable Python package
├── cli/              # CLI commands (run, validate, status, analyze)
├── core/             # Orchestration, grading, metrics, queue
├── tools/            # Built-in + MCP tool system
└── env/              # Environment services (JSON DB, mock web, RAG)
examples/             # Example tasks and run configurations
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
| Python package API | [docs/PYTHON_PACKAGE.md](docs/PYTHON_PACKAGE.md) |
| Task packs | [docs/TASK_PACKS.md](docs/TASK_PACKS.md) |
| Configuration reference | [docs/REFERENCE.md](docs/REFERENCE.md) |
| Security model | [docs/SECURITY.md](docs/SECURITY.md) |
| Adapter architecture | [docs/ADAPTER_ARCHITECTURE.md](docs/ADAPTER_ARCHITECTURE.md) |
| Benchmark types | [docs/BENCHMARK_TYPES.md](docs/BENCHMARK_TYPES.md) |
| Testing guide | [tests/README.md](tests/README.md) |

## Examples

| Example | Description |
| --- | --- |
| [`examples/package_api/`](examples/package_api/) | Programmatic run via Python imports |
| [`examples/analyze_results/`](examples/analyze_results/) | Metrics and failure attribution analysis |
| [`examples/distributed_run/`](examples/distributed_run/) | Queue-backed distributed execution |
| [`examples/custom_grading/`](examples/custom_grading/) | Custom scoring patterns |
| [`examples/browser_task/`](examples/browser_task/) | Browser task authoring workflow |

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
