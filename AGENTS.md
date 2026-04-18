# AGENTS.md

## Read This First

> **STOP. READ THIS BEFORE DOING ANYTHING.**

Non-negotiable rules for every AI agent working on this codebase:

1. **Surface failures explicitly** — do not add fallbacks that hide errors
2. **Quality over shortcuts** — this is production, not MVP
3. **Fix what you find** — broken code found = broken code fixed
4. **Challenge and verify** — question unclear requirements, check docs and source first
5. **Test behavior, not code** — mocks hide problems, test real behavior

**Question routing:**

| Question type | Where to look |
|---|---|
| Library/framework API | Context7 MCP → official docs → source code |
| Project architecture | `README.md` → `docs/` → source code |
| Product/requirements | Ask the user |

**Session startup:** Read `README.md` and `.vscode/tasks.json` before writing any code. Do not ask permission — these are essential context.

## Project Overview

Tolokaforge is an LLM tool-use benchmarking harness.

- Quick start: `README.md`
- Detailed guides: `docs/`

## Core Rules

1. Surface failures explicitly. Do not add fallbacks that hide errors.
2. Keep harness logic generic. Task-specific logic belongs in task packs.
3. Prefer deterministic grading when possible; use rubric judging only when needed.
4. Keep task quality high: natural user requests, non-trivial objectives, meaningful pass/fail signal.
5. Preserve backward compatibility for task contracts unless a migration is explicit.
6. Keep abstractions clean. Do not leak implementation details across boundaries.
7. Interfaces over implementation. Defer a perfect implementation if needed, but never postpone interface/protocol design.
8. Keep documentation and rules actual. Do not keep legacy mentions — update or remove them immediately.

## Setup and Commands

### Package Manager (uv)

We use `uv` as the package manager. It handles virtual environments, dependency resolution, and package installation automatically.

```bash
# Install all dependencies
uv sync

# Run Python scripts
uv run python <script>

# Run CLI tools
uv run tolokaforge --help

# List installed packages
uv pip list
```

**Key rules:**

- Always use `uv run` prefix for Python commands — never `pip install` or bare `python`
- Lockfile `uv.lock` ensures reproducible builds
- Virtual environment lives in `.venv`
- For new dependencies, add to `pyproject.toml` and run `uv sync`
- `uv` does **not** load `.env` — use `scripts/with_env.sh` wrapper when env vars are needed

**Troubleshooting `uv` availability:** If you get `command not found: uv`, use `scripts/with_env.sh uv ...` — it loads the shell profile correctly in addition to `.env` variables.

**Installing additional tools/packages:** Install them locally for immediate use, then also add the installation to `.devcontainer/Dockerfile` so the devcontainer stays reproducible.

### Linting and Formatting

We use `ruff` for linting and formatting Python code.

```bash
# Check for linting issues
uv run ruff check tolokaforge tests scripts tools

# Auto-fix linting issues
uv run ruff check . --fix

# Format code
uv run ruff format .

# Format check (CI)
uv run black --check tolokaforge tests scripts tools && uv run ruff format --check tolokaforge tests scripts tools

# All-in-one lint script
scripts/lint/run_ruff.sh
```

### Testing

Three test categories with distinct markers:

```bash
# Unit tests — no external services needed
uv run pytest tests/ -v -m unit

# Canonical tests — snapshot/contract tests, no external services
uv run pytest tests/ -v -m canonical

# Integration tests — require API keys and/or services
scripts/with_env.sh uv run pytest tests/ -v -m integration

# Validate task definitions
uv run tolokaforge validate --tasks "examples/**/task.yaml"
```

**`scripts/with_env.sh` convention:** Use `scripts/with_env.sh uv run ...` when you need `.env` variables (API keys, service URLs). Use plain `uv run ...` for tasks that don't need environment variables (unit tests, linting).

### Local Services

Browser, JSON DB, and RAG tasks require environment services. Start them with Docker:

```bash
make docker-build-core   # Build core images (db-service + runner)
make docker-up           # Start Docker services (core stack)
make docker-status       # Check service health
make docker-down         # Stop and remove services
```

### Docker

Docker commands are managed through the CLI via Makefile targets:

```bash
make docker-build        # Build all Docker images
make docker-build-core   # Build core images only (db-service + runner)
make docker-up           # Start Docker services (core stack)
make docker-down         # Stop and remove Docker services
make docker-status       # Show Docker service status
```

### Command Execution Tips

Use `tee` instead of `head`/`tail` for long test runs — losing output means rerunning expensive test suites:

```bash
uv run pytest -v 2>&1 | tee /tmp/test-output.log
```

## Architecture

### Directory Map

| Directory | Purpose |
|---|---|
| `tolokaforge/cli` | Command entrypoints |
| `tolokaforge/core` | Orchestration, grading, metrics, models, search |
| `tolokaforge/runner` | gRPC runner service (DB client, tool factory) |
| `tolokaforge/executor` | gRPC executor service |
| `tolokaforge/agent` | gRPC agent service |
| `tolokaforge/adapters` | Benchmark adapters (native, frozen_mcp_core) |
| `tolokaforge/tools` | Tool registry and builtin tools |
| `tolokaforge/env` | Local environment services (JSON DB, mock web, RAG) |
| `examples/` | Example tasks and run configurations |

### Key Subsystems

- **CLI** (`tolokaforge/cli`): Entry point for all commands — `run`, `validate`, `docker`, etc.
- **Core** (`tolokaforge/core`): Orchestration engine, grading pipeline, metrics collection, model interfaces, and task search.
- **Runner** (`tolokaforge/runner`): gRPC service managing benchmark execution, database clients, and tool instantiation.
- **Executor** (`tolokaforge/executor`): gRPC service that executes individual agent steps in isolated environments.
- **Agent** (`tolokaforge/agent`): gRPC service wrapping LLM agent interactions.
- **Adapters** (`tolokaforge/adapters`): Translate between task formats — native (built-in) and frozen_mcp_core.
- **Tools** (`tolokaforge/tools`): Registry of builtin tools available to agents during benchmark runs.
- **Environment Services** (`tolokaforge/env`): JSON DB state service, mock web service, and RAG service for local development.

## Development Workflow

### Feature Development

Follow this protocol: **plan → confirm → build → verify**.

1. **Plan** — Read repo documentation thoroughly and related GitHub issues. Analyze relevant code, identify dependencies, design the approach
2. **Confirm** — Create a detailed plan and discuss it with the user. Analyze the plan against our core principles. **Never start implementation without explicit confirmation** for large changes
3. **Build** — Split plan into stages and implement every stage with subtasks. Require a detailed report from every subtask and use it to correct/enrich the implementation plan. Update repository documentation after every stage — it should be actual at every point
4. **Verify** — Review the code according to our code standards. Lint passes, tests pass, no regressions
5. **Ship** — Commit, push, and create a PR

### Planning Principles

- **Focus on what/why, not how** — describe the goal and rationale
- **Reuse over create** — check what exists before building new
- **Decisions over code** — document why a choice was made, not implementation details
- **At end of plan:** list unresolved questions

### Documentation Standards

**KEEP ONLY ACTUAL INFORMATION** — no fluff, no marketing, no redundant examples.

What NOT to add:
- Verbose explanations of obvious concepts
- Redundant examples when one suffices
- Step-by-step tutorials duplicating README
- Speculative future features

What TO include:
- Unique technical details not in README
- Configuration schemas with field descriptions
- API signatures and parameters
- Error messages and their meanings
- Working code examples (minimal, runnable)

**Documentation locations:**

| File | Purpose |
|---|---|
| `README.md` | Project overview, quick start, basic usage |
| `AGENTS.md` | Agent instructions, development rules, conventions |
| `docs/*.md` | Detailed reference for specific subsystems |

Before adding documentation: check if info already exists in `README.md` or `AGENTS.md`. If it exists, link instead of duplicating.

## MCP Servers

Recommended MCP servers for AI agents working on this project:

- **Context7** — Library/framework documentation lookup. Use BEFORE guessing at APIs
- **GitHub** — PR creation, issue management, code search
- **Web Search** — Best practices, bug reports, when Context7 is insufficient

## Python Conventions

### Style and Tooling

- `pyproject.toml` for project configuration
- `uv` for package management
- `ruff` for linting and formatting
- `pytest` for testing

**Don't suppress warnings** — update code to use actual functionality instead.

### Preferred Libraries

| Purpose | Library |
|---|---|
| CLI argument parsing | `typer` |
| Retry logic | `tenacity` |

### uv Workspace Rules

- **DO NOT** use `[project.optional-dependencies]` in workspace member packages
- All dev dependencies go in root `pyproject.toml` under `[dependency-groups]` → `dev` (PEP 735)
- Workspace members reference each other with `{ workspace = true }` in dependencies
- Every tool in `tools/` must be a uv workspace member (runnable via `uv run <tool-name>`)
- Every tool in `tools/` must register in `pyproject.toml` `[tool.uv.workspace]`

**Current workspace packages:**

- `tools/dev-mcp` — Dev MCP server for AI agent interaction
- `tools/demo-recorder` — Demo recording utilities
- `tools/eval-orchestrator` — Benchmark eval splitting and merging for CI shards
- `tools/pricing-updater` — LLM pricing data updates

### Virtual Environment

One Python virtual environment: `.venv` (main project).

- Setup script: `scripts/setup/create_python_venv.sh`
- `uv run` automatically uses the correct virtual environment

## Code Standards

1. **Fail fast, don't mute errors.** Prefer explicit over defaults. Exception catching and defaults often mask problems.
2. **Don't repeat yourself.** Move common code to common functions, classes, and modules.
3. **Split complexity:**
   - Big functions (over 100 lines) → split into several smaller functions
   - No god-like classes — use class composition and object hierarchy
   - Big files → split into separate modules
4. **Minimize nesting depth.** If nesting level reaches ≥ 3, optimize readability:
   - Check inverse condition and return early instead of wrapping in a block
   - Extract logic into a separate function
   - Break out of loops early or `continue` early
5. **Self-describing code.** The best code doesn't need comments — it communicates through names, functions, classes, and modules.

## Dockerfile Guidelines

### Multi-Stage Builds

Use multi-stage builds to separate build dependencies from runtime:

```dockerfile
FROM image:tag AS base
# Common environment variables

FROM base AS builder
# Build dependencies and compilation

FROM base AS production
# Copy artifacts from builder, runtime configuration
```

### Layer Optimization

1. **Order layers by change frequency** — less frequently changed instructions first
2. **Combine RUN instructions** — use `&&` to chain commands
3. **Use .dockerignore** — exclude unnecessary files from build context
4. **Copy dependencies before source** — copy `pyproject.toml`/`uv.lock` before source code for better caching

### Security

1. **Non-root user** — create and use a dedicated user named `runner`
2. **Minimal base images** — prefer slim or alpine variants
3. **Pin base image versions** — use specific tags, never `latest`
4. **Use COPY, not ADD** — unless you specifically need ADD's features
5. **Minimize attack surface** — only install necessary packages

### Python-Specific

- Set `PYTHONUNBUFFERED=1` for proper logging
- Set `PYTHONDONTWRITEBYTECODE=1` to avoid .pyc files
- Use BuildKit cache mounts for pip/uv cache: `RUN --mount=type=cache,target=/path`

### Formatting

Use consistent casing: `FROM base AS builder`, not `FROM base as builder`.

## Repository Hygiene

### Root Cleanliness

Only standard project files in root: README, LICENSE, CHANGELOG, CONTRIBUTING, CONTRIBUTORS, CITATION, AGENTS.md, CLAUDE.md, pyproject.toml, uv.lock, Makefile, and dotfiles (.gitignore, .pre-commit-config.yaml, etc.).

No scripts, data files, temporary documents, or logs in root.

### Script Organization

- Bash scripts in `scripts/` organized by subdirectory: `setup/`, `lint/`, `tests/`
- Shared utilities (`common.sh`, `with_env.sh`) at `scripts/` root
- Exceptions: `tests/` for test helpers, `.devcontainer/` for container setup, Docker entrypoints alongside Dockerfiles
- Complex Python logic → `tools/` as uv workspace member
- Simple bash wrappers are fine in `scripts/`
- Wrap Python tools with simple bash scripts in `scripts/` for common usage
- See `scripts/README.md` for full guidelines

### No Temporary Artifacts

- `plans/` is gitignored — local planning only
- Use `docs/` for permanent development plans
- Never commit: log files, JSON data dumps, build outputs, scratch documents
- Data files belong in `tests/data/` or task fixture directories

## Task Design Quality Bar

1. Avoid tasks that always pass; target useful difficulty.
2. Avoid walkthrough-style scripted prompts.
3. Ensure grading checks agent-produced outcomes, not default/pre-filled values.
4. Route app/task state through the state service so grading can verify deterministically.

## Known Gotchas

1. **Browser automation** requires Chromium: `uv run playwright install --with-deps chromium`
2. **Golden-set tests** depend on Git LFS data under `tests/data/projects/`. Missing LFS content → fixture failures. Run `git lfs pull` first if needed.
3. **Formatting drift**: `ruff format --check` may report pre-existing drift in ~8 files. Known, not your fault.
4. **`black --check`** exits non-zero on pre-existing files. Same known drift.
5. **Benchmark runs** and e2e flows require API keys in `.env`. Unit and canonical tests do not.
6. **10 tests in `test_golden_set_projects.py`** need `git lfs pull`. Not required for normal development.
7. **JSON DB update API** uses JSON Patch-style operations: `{"ops": [{"op": "replace", "path": "$.field", "value": ...}]}`. Supported ops: `add`, `replace`, `remove`.
8. **Service startup**: Start both services in background (`&`) for JSON DB (port 8000) + Mock Web (port 8080). Mock Web requires `JSON_DB_URL=http://localhost:8000`.
9. **`tolokaforge run`** requires at least one LLM API key in `.env` (Anthropic, OpenAI, etc.).

## Detailed Documentation

| Topic | Location |
|---|---|
| Getting started | `docs/GETTING_STARTED.md` |
| Test suite | `tests/README.md` |
| Scripts | `scripts/README.md` |
| Task design | `docs/TASKS.md` |
| Grading | `docs/GRADING.md` |
| Configuration | `docs/CONFIG.md` |
| Docker / Runner | `docs/RUNNER.md` |
| Adapters | `docs/ADAPTERS.md` |
| Future plans | `docs/FUTURE_DEVELOPMENT.md` |
| API reference | `docs/API.md` |
| PyPI publishing | `docs/PYPI_PUBLISHING.md` |
| Troubleshooting | `docs/TROUBLESHOOTING.md` |
