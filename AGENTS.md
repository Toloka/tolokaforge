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

### Secrets — single abstraction

`SecretManager` (in [`tolokaforge/secrets`](tolokaforge/secrets/)) is the **only** way to read any secret in this codebase. This applies to LLM API keys, database credentials, OAuth tokens, signing keys, and anything else of credential nature.

**Forbidden:**
- `os.environ.get(...)` / `os.getenv(...)` for any credential
- `load_dotenv()` outside `DotEnvProvider`
- `from dotenv import load_dotenv` outside `tolokaforge.secrets`
- Reading `.env` / `.netrc` / `.aws/credentials` / similar files directly
- Baking secrets into Docker images, build args, mounts, or image tags
- One-off helpers that hide an `os.environ` access

**Allowed:**
- `from tolokaforge.secrets import get_default`, then `get_default().get_secret("OPENAI_API_KEY")` (and the `get_secret_or_raise` / `validate_required` variants)
- Adding a new `SecretProvider` subclass when integrating a new secret backend (Vault, AWS Secrets Manager, etc.) — never a one-off call site
- `expand_secret_refs` when a **non-secret** config string must carry a secret value: it resolves `${secret:NAME}` inside that string. This is the only sanctioned expansion mechanism: do not hand-roll a second reference dialect at a new call site, and do not move expansion into `get_secret` (that path also feeds the log-redaction set and the container serializer, which resolve every enumerable key, so it would apply this syntax to credentials that legitimately contain `$`). See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § "Values may reference secrets".
- `SecretManager.export_to_environ(keys)` *only* when a subprocess (e.g. litellm SDK) demands `os.environ`; called inside the smallest possible scope, never sprinkled

`tolokaforge run` calls `init_default()` once at startup. Inside the runner container, `init_default_from(...)` is reconstructed from `TOLOKAFORGE_SECRETS_JSON`. There is no other initialization path. The CI test [`tests/unit/secrets/test_no_raw_secret_access.py`](tests/unit/secrets/test_no_raw_secret_access.py) static-greps for these patterns; adding a new violation will fail CI.

## Setup and Commands

### Package Manager (uv)

We use `uv` as the package manager. It handles virtual environments, dependency resolution, and package installation automatically.

```bash
# Install all dependencies
uv sync

# Run Python scripts
uv run python <script>

# Run CLI tools — direct after `uv tool install --editable . --python 3.12`
tolokaforge --help
# Or without the global install:
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
uv run ruff format --check tolokaforge tests scripts tools
```

### Testing

Three test categories with distinct markers. Tests live in three roots — `tests/` plus a
root inside each workspace package that owns a contract (`tolokaforge_models/tests/`,
`tolokaforge_coding_harnesses/tests/`). `[tool.pytest.ini_options] testpaths` names all
three, so omit the path; a command naming only `tests/` overrides `testpaths`, runs a
subset, and still reports green.

```bash
# Unit tests — no external services needed
uv run pytest -v -m unit

# Canonical tests — snapshot/contract tests, no external services
uv run pytest -v -m canonical

# Integration tests — require API keys and/or services; run in parallel
scripts/with_env.sh uv run pytest -v -m integration -n auto

# Validate task definitions — exits 1 on an invalid task or on a glob matching nothing
tolokaforge validate --tasks "tasks/**/task.yaml"
```

**`make validate`** runs the same command over `TASKS_GLOB` (`$(TASKS_DIR)/**/task.yaml`, `TASKS_DIR` defaulting to `tasks`). Task packs are cloned separately, so the target skips with a printed reason when nobody named a target — `TASKS_DIR` absent *and* `TASKS_GLOB` still the default derived from it. Point `TASKS_DIR` (or `TASKS_GLOB`) at your own pack to validate it; either override runs. The dev MCP's `validate_tasks` skips the same default for the same reason. See [`docs/CLI.md`](docs/CLI.md) § Task validation for the exit-code contract and the project layering.

**`scripts/with_env.sh` convention:** Use `scripts/with_env.sh uv run ...` when you need `.env` variables (API keys, service URLs). Use plain `uv run ...` for tasks that don't need environment variables (unit tests, linting).

**Parallel integration tests:** `-n auto` (pytest-xdist) spreads the integration lane across worker processes. `tests/integration/reset_recipes/conftest.py` gives each worker a unique `COMPOSE_PROJECT_NAME` for the reset-recipe suite, whose stacks all share the `compose` basename and would otherwise collide across workers. The rest of the integration suite derives per-test project names from slug-encoded `make_project_temp_dir` basenames, so it stays disjoint without the env pin.

### Local Services

Tasks that need environment services start them with Docker. The core stack (`make docker-up`) is db-service + runner only — it serves JSON DB tasks. **Browser and RAG tasks need the full stack** (adds rag-service + mock-web); there is no `make` target for it, so start it with `tolokaforge docker up --profile full`.

```bash
make docker-build-core              # Build core images (db-service + runner)
make docker-up                      # Start core stack (JSON DB tasks)
tolokaforge docker up --profile full  # Start full stack (adds RAG + browser services)
make docker-status                  # Check service health
make docker-down                    # Stop and remove services
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

### CI / GitHub Actions

| Workflow | Trigger | Purpose |
|---|---|---|
| `benchmark.yml` | Manual / scheduled | Run benchmark suites against branches |
| `ci.yml` | Push to `main`, PRs, manual | Lint, test matrix, build, integration, validate |
| `claude-review.yml` | PR opened/synced, `@claude` comment, inline review comment | AI code review via the Claude Code Action — flags violations of the rules in this file |
| `integrate-model.yml` | `integrate: <slug>` PR label / `workflow_dispatch` | Model auto-integration engine: observe → resolve → finalize (see `docs/AUTO_INTEGRATION.md`) |
| `publish-images.yml` | `image-v*` tag / `workflow_dispatch` (build-only dry-run) | Build + publish the four `tolokasoft1/tolokaforge-*` Docker Hub images via OIDC; rc tags → `pre-stable` (immutable `:X.Y.Z-rc.N`), stable tags → `release` (immutable `:X.Y.Z` then moving `:X.Y` + `:latest`), gated on an in-workflow rc-smoke |
| `release-gate.yml` | Tag/release events | Final pre-release gate |
| `slack-integrate.yml` | Scheduled / manual | Polls Slack for `@bot integrate <model>` requests, opens the draft PR, dispatches `integrate-model.yml` |

**Required GitHub secrets:**

| Secret | Required by |
|---|---|
| `ANTHROPIC_API_KEY` | `claude-review.yml` (the reviewer model) and any integration tests that use Claude. NOT used by `integrate-model.yml` (its resolve/finalize agent runs on OpenRouter via the LiteLLM gateway). |
| `ARENA_AUTOMATION_OPENROUTER_API_KEY` | `integrate-model.yml` - candidate-model probes/reprobes AND the resolve/finalize agent (routed through the LiteLLM -> OpenRouter gateway) |
| `ARENA_AUTOMATION_SLACK_BOT_TOKEN` | `integrate-model.yml` + `slack-integrate.yml` (thread notifications and the request poller; both degrade to a no-op without it) |
| `ARENA_AUTOMATION_LLM_PROXY_BASE_URL` + `_API_KEY` | the optional LLM-gateway route in both workflows (both, or neither). Absent means every request runs over OpenRouter and gateway availability reports as unknown. |
| whatever `vars.LLM_PROXY_HEADERS` references as `${secret:NAME}` | a gateway that admits callers by an attribution header. Both workflows pass the variable and the referenced secrets; see [`docs/AUTO_INTEGRATION.md`](docs/AUTO_INTEGRATION.md). |

Optional secrets (integration tests auto-skip without them): `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `NOVA_API_KEY`, `TYPESENSE_API_KEY`. CI passes provider keys to test jobs as env vars; runtime code reads them via `SecretManager` (never directly).

### Command Execution Tips

Prefer available specialized MCP servers like `dev`, `context7`, `github`, `perplexity` over generic bash like `gh`, `curl`, etc.

Use `tee` instead of `head`/`tail` for long test runs — losing output means rerunning expensive test suites:

```bash
uv run pytest -v 2>&1 | tee /tmp/test-output.log
```

## Architecture

### Directory Map

| Directory | Purpose |
|---|---|
| `tolokaforge/dx` | Terminal front-end (reference implementation of the `RunDisplayEvents` seam — see ADR-0019). Rich panels, banners, dry-run rendering, and the Click command tree under `tolokaforge/dx/cli/`. Optional dep, installed via `pip install 'tolokaforge[dx]'`. |
| `tolokaforge/core` | Orchestration, grading, metrics, models, search |
| `tolokaforge/core/llm` | LLM abstractions — reasoning, schema, cache, usage, client |
| `tolokaforge/runner` | gRPC runner service (DB client, tool factory, LLM judge) |
| `tolokaforge/adapters` | Benchmark adapters (native, tau, tlk_mcp_core) |
| `tolokaforge/secrets` | Secret management (SecretManager, providers, config) |
| `tolokaforge/tools` | Tool registry and builtin tools |
| `tolokaforge/env` | Local environment services (JSON DB, mock web, RAG) |
| `tasks/` | Example tasks (external, cloned separately — see README) |

### Key Subsystems

- **Terminal front-end** (`tolokaforge/dx`): Reference implementation of the `RunDisplayEvents` seam (ADR-0019). Owns the Click command tree (`tolokaforge/dx/cli/`) for `run`, `validate`, `docker`, etc., plus Rich panels, banners, and the dry-run renderer. Rich lives in the `[dx]` extras — headless-server installs do not pull it in.
- **Core** (`tolokaforge/core`): Orchestration engine, grading pipeline, metrics collection, model interfaces, model capability policies, and task search.
- **Runner** (`tolokaforge/runner`): gRPC service managing benchmark execution, database clients, tool instantiation, and LLM-as-judge evaluation (when `grading.yaml` configures `llm_judge`).
- **Adapters** (`tolokaforge/adapters`): Translate between task formats — native (built-in), tau-bench, and tlk_mcp_core.
- **Secrets** (`tolokaforge/secrets`): Universal secret management. `SecretManager` reads `.env` via `DotEnvProvider` then falls back to `os.environ` via `EnvProvider`; never pollutes `os.environ` itself. `tolokaforge run` calls `init_default()` once at startup; the runner container reconstructs the singleton from `TOLOKAFORGE_SECRETS_JSON`. See the **Secrets — single abstraction** rule below for the contract.
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
- **codebase-memory-mcp** — Code knowledge graph (symbols, references, call chains). First call for any "where is X / who calls X / how is X wired to Y" question — `search_graph`, `trace_path`, `get_code_snippet`. Prefer over `grep -r` / `find -name`. Per-engineer opt-in: `make cbm-onboard` (see [`scripts/README.md`](scripts/README.md))

## Python Conventions

### Style and Tooling

- `pyproject.toml` for project configuration
- `uv` for package management
- `ruff` for linting and formatting
- `pytest` for testing

The runtime Python version is single-sourced in `.python-version`; changing it propagates to dev, CI, the devcontainer (via uv), and all runtime Docker images. A canonical guard (`tests/canonical/test_python_version_single_source.py`) fails CI if a workflow or runtime Dockerfile hardcodes a version instead.

**Don't suppress warnings** — update code to use actual functionality instead.

### Protected Directories

> **DO NOT MODIFY ANYTHING IN `contrib/` DIRECTORY.**
>
> This directory contains external/vendored code. Any changes must go through proper vendoring/update processes.

### Preferred Libraries

| Purpose | Library |
|---|---|
| CLI argument parsing | `typer` |
| Retry logic | `tenacity` |

### Type system choices

Pick by what the type is *for*, not by habit. Pydantic is for cross-boundary data — using it for in-process values or behavioural contracts adds validation overhead with no payoff.

| Use case | Choice |
|---|---|
| Polymorphism / behaviour contract (anything that "implements this can plug in") | `typing.Protocol` (preferred — duck-typed) or `abc.ABC` |
| Internal value object passed between methods in one process | `@dataclasses.dataclass(frozen=True)` |
| Enumeration of named values | `class Foo(str, Enum)` |
| Data that crosses a serialisation boundary (gRPC, JSON file, YAML config, output bundle, snapshot) | Pydantic v2 `BaseModel` with `model_config = {"extra": "forbid"}` |

How this looks in the engine today:

- `BaseAdapter`, `ToolWrapper`, the LLM policy interfaces (`SystemPromptPolicy`, `ToolSchemaSanitizer`, `ResponsePolicy`, `ReasoningCodec`, …), `RunQueue` — `Protocol` or `ABC`.
- `ToolLifecycleContext`, `AttemptLease` — `@dataclass(frozen=True)`.
- `AdapterType`, `InvocationStyle`, `TrialStatus`, `ExecutionStatus` — `str, Enum`.
- `TaskDescription`, `ToolSchema`, `ToolSource`, `ModelConfig`, `Trajectory`, `Grade`, `Metrics`, `GradingConfig`, `TrialSpec`, `TrialResult` — Pydantic `BaseModel` (`extra="forbid"`).

When extending an existing contract, follow the existing choice unless you have an explicit reason to change it (e.g. a previously in-process value object now needs to serialise — promote it to Pydantic in a dedicated PR with the rationale).

### uv Workspace Rules

- **DO NOT** use `[project.optional-dependencies]` in workspace member packages
- All dev dependencies go in root `pyproject.toml` under `[dependency-groups]` → `dev` (PEP 735)
- Workspace members reference each other with `{ workspace = true }` in dependencies
- Every tool in `tools/` must be a uv workspace member (runnable via `uv run <tool-name>`)
- Every tool in `tools/` must register in `pyproject.toml` `[tool.uv.workspace]`

**Current workspace packages:**

- `tools/automation` — Arena model auto-integration: observe / resolve / finalize + Slack poller
- `tools/dev-mcp` — Dev MCP server (run tests, lint, format, validate tasks)
- `tools/pricing-updater` — LLM pricing data updates
- `tools/rubric-calibrator` — Rubric-judge calibration: agreement metrics + trust gate
- `external_adapters/tolokaforge-adapter-terminal-bench` — Terminal-Bench adapter
- `tolokaforge_models` — model data + per-model policy subclasses
- `tolokaforge_coding_harnesses` — coding-harness registry, installer, middleware proxy

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

- Bash scripts in `scripts/` organized by subdirectory: `analysis/`, `docker/`, `hatch/`, `setup/`, `tests/`
- Shared utilities (`common.sh`, `with_env.sh`) at `scripts/` root
- Exceptions: `tests/` for test helpers, `tasks/` for benchmark data, `.devcontainer/` for container setup, Docker entrypoints alongside Dockerfiles
- Complex Python logic → `tools/` as uv workspace member
- Simple bash wrappers are fine in `scripts/`
- Wrap Python tools with simple bash scripts in `scripts/` for common usage
- See `scripts/README.md` for full guidelines

### No Temporary Artifacts

- `plans/` is gitignored — local planning only
- Use `docs/` for permanent development plans
- Never commit: log files, JSON data dumps, build outputs, scratch documents
- Data files belong in `tests/data/`, `contrib/`, or task fixture directories

### No Project-Specific Content on main

- No domain-specific configs or runner scripts on `main` branch
- Do NOT commit project-specific scripts (e.g., proprietary domain runners) to `main`
- A run config lives **next to** the example it runs (`examples/<adapter>/<family>/run_config.yaml`). There is no separate `config/` directory.

## Task Design Quality Bar

1. Avoid tasks that always pass; target useful difficulty.
2. Avoid walkthrough-style scripted prompts.
3. Ensure grading checks agent-produced outcomes, not default/pre-filled values.
4. Route app/task state through the state service so grading can verify deterministically.

## Adding a new model / provider

Full six-step process: [`docs/ADD_NEW_MODEL.md`](docs/ADD_NEW_MODEL.md).

**Non-negotiable rules:**

1. **No PR merges without green capability tests** against the live provider, with test output quoted in the PR description.
2. **Be honest in `ModelCertificate`** — `required` vs `known_unsupported` must be explicit; the canonical test rejects silent omissions.
3. **No Python conditional branches on model name** — all model-specific behaviour goes through the preset registry and `ModelCapabilities` policy slots.
4. **"OpenAI-compatible" is API-envelope-only — schema dialects differ per provider.** When `test_dict_map_tool_call` or `test_discriminated_union_tool_call` fails for a new model with field-rename symptoms (model emits `quantity` for registered `qty`, `title` for `subject`, etc.), suspect schema-dialect mismatch (`$defs`/`$ref`/`oneOf`/`additionalProperties:{schema}` not in the provider's supported subset) before suspecting model behaviour. Capture the wire payload with a direct REST probe; the fix belongs in a per-provider `ToolSchemaSanitizer`, never in prompt engineering or task-pack field renaming. See gotcha #21.

The Bucket-A allow-list backing the [`tests/canonical/test_models_wheel_replay.py`](tests/canonical/test_models_wheel_replay.py) acceptance test tracks these paths. A new data-carrying file that lands **under an already-allowed prefix** (e.g. under `tolokaforge_models/`) is auto-classified Bucket A by [`tools/automation/src/automation/bucket_classifier.py`](tools/automation/src/automation/bucket_classifier.py)'s `BUCKET_A_ALLOWED_PREFIXES` and needs no classifier edit. A file **outside every allowed prefix** must be added to `BUCKET_A_ALLOWED_FILES` (single file) or `BUCKET_A_ALLOWED_PREFIXES` (new subtree) in the same PR. The classifier is exposed as `uv run automation classify-paths` for the `integrate-model.yml` finalize step.

## Known Gotchas

1. **Browser automation** requires Chromium: `uv run playwright install --with-deps chromium`
2. **Golden-set tests** depend on Git LFS data under `tests/data/projects/`. Missing LFS content → fixture failures. Run `git lfs pull` first if needed.
3. **Formatting drift**: `ruff format --check` reports pre-existing drift across the tree — no number is quoted here because a count in a doc goes stale silently. Run `mcp__dev__format_check` scoped to the files you touched (`paths=…`) and treat only *your* files' drift as yours to fix.
4. **`black --check` is clean tree-wide**, so a `black` failure is drift in the code you just wrote, not the known `ruff format` backlog. Both formatters must pass on a file you touch, and they disagree often enough that satisfying one is not satisfying the other.
5. **Benchmark runs** and e2e flows require API keys in `.env`. Unit and canonical tests do not.
6. **10 tests in `test_golden_set_projects.py`** need `git lfs pull`, plus the committed-corpus sweep in [`tests/unit/test_scratchpad_detector.py`](tests/unit/test_scratchpad_detector.py) (skips gracefully without LFS). Not required for normal development.
7. **JSON DB update API** uses JSON Patch-style operations: `{"ops": [{"op": "replace", "path": "$.field", "value": ...}]}`. Supported ops: `add`, `replace`, `remove`.
8. **Service startup**: Start both services in background (`&`) for JSON DB (port 8000) + Mock Web (port 8080). Mock Web requires `JSON_DB_URL=http://localhost:8000`.
9. **`tolokaforge run`** requires at least one LLM API key in `.env` (Anthropic, OpenAI, etc.).
10. **`tasks/` is external** — Task packs live outside the engine. Point `task_packs` in your config at any directory containing tasks, or place them in `tasks/`. See the bundled examples in `examples/` for the expected layout.
11. **GPT-5.4 / Qwen drop dict-map parameters** — `Dict[str, T]` schemas cause these models to silently omit or stringify the parameter. The `openai_gpt5` / `qwen` / `xai_grok` presets wire `StrictSchema` + `ArrayDictMapResponse` + `DictMapHints` to fix this. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `schema_sanitizer`.
12. **litellm OpenRouter routing lacks GPT-5 awareness** — our `StrictSchema` and `DictMapHints` handle all GPT-5 tool-schema adaptation; litellm's native `_remove_additional_properties` does not apply via OpenRouter. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § litellm OpenRouter routing caveat.
13. **Pydantic `Decimal` fields emit RE2-incompatible `pattern`** — `StrictSchema` strips all `pattern` / `format` keys and collapses the Decimal `anyOf` idiom to `{type: number}`. Raises `ValueError` if any RE2-unsafe regex survives post-sanitise. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `schema_sanitizer`.
14. **Anthropic `thinking_blocks` signatures must be preserved and replayed** — `AnthropicReasoningCodec` extracts full `{type, thinking, signature}` blocks; `_convert_messages` splices them back via the litellm `thinking_blocks` field. Claude 4.7's `display="omitted"` (empty text, populated signatures) also round-trips. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `reasoning_codec`.
15. **Claude 4.7 ignores `reasoning_effort`** — use litellm's `thinking={"type":"enabled","budget_tokens":N}` kwarg instead. The `anthropic_claude_4_7` preset handles this automatically (`reasoning_via_thinking_kwarg: true`, `reasoning_budget_default: 8000`, `drop_sampling_when_thinking: true`). See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `params_policy`.
16. **`Metrics.tokens_input` / `tokens_output` / `GenerationResult.token_usage` removed** — use `Metrics.usage.prompt_tokens` / `.completion_tokens` / `.reasoning_tokens` / `.cached_tokens` / `.cache_*_input_tokens`. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `usage` and [`docs/OUTPUT_FORMAT.md`](docs/OUTPUT_FORMAT.md) § `metrics.yaml`.
17. **Anthropic models default to ephemeral prompt caching** — `cache_policy: anthropic_ephemeral` on both Anthropic presets. Observable via `Metrics.usage.cache_read_input_tokens`. Disable for ablation with `cache_policy: none`. OpenRouter-routed cache counters use a dual-path fallback in `UsageExtractor`. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `cache_policy`.
18. **Every trial records its preset + policy fingerprint** in `task.yaml.model_config.<role>.resolved.*` and tool schemas in `results/tools_schemas/<task>__<model>.json`. See [`docs/OUTPUT_FORMAT.md`](docs/OUTPUT_FORMAT.md) § `task.yaml` + § `tools_schemas`.
19. **Gemini 3.1 Pro substitutes `:` for repeated `_` in tool names** — emits names like `workday_api:workday_api_get_employee` instead of the registered `workday_api_workday_api_get_employee`. The `tool_name_discipline` capability test reproduces this single-turn for Pro. **Gemini 3.5 Flash exhibits the same family in multi-turn production only** — verified live 2026-05-20 on `output/new_collected/tau_manufacturing/gemini_35_flash`: 2 of 7,817 tool calls (0.025 %) emitted malformed names — `'get_entity_info'` (missing `tau_manufacturing_` prefix) and `'default_api:tau_manufacturing_list_allocations_by_order'` (`:` separator + `default_api:` prefix). The single-turn `tool_name_discipline` test passes for 3.5 Flash, so the cert declares `required`; the production gap is the same family as gotcha #22 (single-turn synthetic doesn't catch multi-turn / heavy-context emergent failures). Gemini 3 Flash Preview unaffected on both surfaces. The 3.1 Pro certificate declares `known_unsupported` as the falsifiable record of the open regression.
20. **Gemini reasoning lives in `provider_specific_fields.reasoning_details`** — same envelope as Anthropic-via-OpenRouter but two new block types: `reasoning.text` (Pro lineage, readable) and `reasoning.encrypted` (Flash lineage, opaque payload). NO `signature`, NO `format`. `GeminiReasoningCodec` handles both; signed-replay continuity tests are not applicable. **OpenRouter sends a constant 48-char placeholder (`reasoning.encrypted` data = base64 of UUID `e24830a7-…b9c3`) when Gemini emitted no real thinking on a turn (e.g. tool-call follow-up turns). Real opaque blobs are 1000-3000+ chars. `GeminiReasoningCodec.encode_for_replay` drops anything < 100 chars so the placeholder doesn't waste prompt tokens or create few-shot patterns the model echoes back. `extract` does NOT filter — `trajectory.yaml` records what the wire actually returned.** See [`tolokaforge/core/llm/reasoning_codec.py`](tolokaforge/core/llm/reasoning_codec.py) § `GeminiReasoningCodec`.
21. **Gemini's tool spec is a JSON-Schema SUBSET — `$defs`/`$ref`/`oneOf`/`discriminator` aren't supported and `additionalProperties:{schema}` silently flattens** — when Pydantic emits any of these (default behaviour for nested models, discriminated unions, and `dict[str, T]` parameters), property names inside the unsupported construct never reach the model. The wire-level symptom looks like "Gemini renames `qty` → `quantity`" / "`subject` → `title`"; the actual cause is schema-loss in transit. Fix lives in [`GeminiSchema`](tolokaforge_models/src/tolokaforge_models/policies/gemini.py) (extends `StrictSchema` with `flatten_oneof_discriminator=True`), routed via `schema_sanitizer: gemini` + `response_policy: array_dict_map` on the `gemini` preset. `prompt_policy: dict_map_hints` does NOT mitigate (the schema info is already gone before the prompt is read). When a new provider exhibits field-rename symptoms, suspect this class of bug *first* — see also rule #4 under "Adding a new model / provider".
22. **Field-omission failures observed in multi-turn evaluations are not single-turn-deterministic** — every registered model (including Gemini 3.1 Pro) passes the synthetic single-turn `test_required_fields_complete` test. The capability is `_CORE_CAPABILITIES`-exempt because every model passes the single-turn baseline; reproducing the multi-turn regression in a synthetic probe needs a multi-turn / heavy-context test variant that does not exist yet.
23. **Empty-content filler injection is a `MessageAssemblyPolicy` capability** — `_convert_messages` substitutes `MessageAssemblyPolicy.empty_assistant_filler` for empty assistant content alongside `tool_calls` ONLY when the active `MessageAssemblyPolicy.inject_empty_assistant_filler == True`. The `aws_nova` and `aws_nova_openrouter` presets carry `NovaMessageAssembly` (filler defaults to `"I'll help you with that."`; Bedrock rejects empty assistant content on tool turns, commit `73e01e9e6`); every other preset — `default`, `anthropic*`, `openai_gpt5`, `xai_grok`, `qwen`, `gemini` — carries `NullMessageAssembly` and leaves the content empty. The filler string is per-instance data on `NovaMessageAssembly` rather than an engine constant because Gemini pattern-matches the substituted string in past assistant turns and echoes `"I'll help you with that."` back as its own response content (2026-04-30 OTS regression analysis: ~26-38% of trials on ots_19_airlines; live probes confirmed 2/5 Gemini calls echoed the filler when it was in context, 0/5 with empty content) — a universal filler is not safe, so every preset outside `aws_nova*` stays on `NullMessageAssembly`. A future provider that needs a different filler overrides it via `message_assembly_policy: {name: nova, params: {empty_assistant_filler: "..."}}` in a preset overlay. Routing pinned by `tests/canonical/test_message_assembly_filler_routing.py`.
24. **Task-pack `dict[str, Any]` parameters defeat schema enforcement** — when a tool parameter is declared `dict[str, Any]` in its task-pack Pydantic model (e.g. `tasks/ots_19_airlines/_domain/tools/mcp_tools_library/ots_19_airlines/zendesk/tools/create_item.py:31` `item: dict[str, Any]`), Pydantic generates `{type: object, additionalProperties: true}` with no inner `properties` / `required` list. `StrictSchema` does NOT rewrite `additionalProperties: true` (only `additionalProperties: <schema>`), so every model — including GPT-5.5 — receives the same permissive schema and must rely on system-prompt policy alone to know which inner fields are required. Field-omission failures (e.g. Gemini Pro forgetting `booking_channel` in 188/550 OTS trials) correlate with this shape. **Fix lives in the task pack** (declare a strict inner Pydantic model with explicit fields), not in the harness. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `schema_sanitizer`.
25. **MiniMax-M3 corrupts the `tags` array via XML→JSON tool-call conversion** — every M3 `tags` emission inside the schemaless `additionalProperties: true` `updates` / `item` object is malformed (2505/2505 airlines occurrences). The provider renders a repeated XML element as a single-key dict `{"item": X}` (76 %) or JSON-encodes / empties the array (`'["a"]'` / `''`, 23 %). The `minimax` preset wires `response_policy: minimax_m3_tags` — the `MinimaxM3TagRecoveryResponse` composite (`JsonRecursiveCoerceResponse` then `ItemRecursiveUnwrapResponse`) — which recurses into the parent and recovers the native list. **Scoping is load-bearing**: recovery is restricted to the declared-array tags sites `updates.tags` / `item.tags` via the `ARRAY_SITES` allowlist in [`minimax.py`](tolokaforge_models/src/tolokaforge_models/policies/minimax.py). A schema-agnostic empty-string→`[]` was proven net-harmful — on MiniMax-M2.7 it corrupts scalar fields (`resolution_category__c`, `employee_id`, `keyword`). M2.7 emits native `tags` lists (0 corrupt) so it is NOT in this preset and is unaffected; the policy never touches `None`, never promotes scalar strings, and leaves multi-key dicts unchanged (no guessing). This is distinct from M3's genuine dict-map / discriminated-union model gap (gotcha #22 family), which stays `known_unsupported`. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `response_policy`.

## Detailed Documentation

| Topic | Location |
|---|---|
| Getting started | `docs/GETTING_STARTED.md` |
| Test suite | `tests/README.md` |
| Scripts | `scripts/README.md` |
| Task design | `docs/TASKS.md` |
| Grading | `docs/GRADING.md` |
| Rubric grading design | `docs/RUBRIC_GRADING_DESIGN.md` |
| Judge replay (offline re-judging) | `docs/JUDGE_REPLAY.md` |
| Trace replay (re-checking trace constraints) | `docs/TRACE_REPLAY.md` |
| Rubric migration (retiring a judge criterion against recorded evidence) | `docs/RUBRIC_MIGRATION.md` |
| Configuration | `docs/CONFIG.md` |
| Docker / Runner | `docs/RUNNER.md` |
| Adapters | `docs/ADAPTERS.md` |
| CLI | `docs/CLI.md` |
| Model auto-integration | `docs/AUTO_INTEGRATION.md` |
| Future plans | `docs/FUTURE_DEVELOPMENT.md` |
| API reference | `docs/API.md` |
| Troubleshooting | `docs/TROUBLESHOOTING.md` |
