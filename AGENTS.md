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
uv run ruff format --check tolokaforge tests scripts tools

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

# Validate task definitions (tasks/ must be cloned locally or use a custom TASKS_GLOB)
uv run tolokaforge validate --tasks "tasks/**/task.yaml"
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

### CI / GitHub Actions

| Workflow | Trigger | Purpose |
|---|---|---|
| `benchmark.yml` | Manual / scheduled | Run benchmark suites against branches |
| `ci.yml` | Push to `main`, PRs, manual | Lint, test matrix, build, integration, validate |
| `claude-code-review.yml` | PR opened/synced, `@claude` comment, inline review comment | AI code review via the Claude Code Action — flags violations of the rules in this file |
| `release-gate.yml` | Tag/release events | Final pre-release gate |

**Required GitHub secrets:**

| Secret | Required by |
|---|---|
| `ANTHROPIC_API_KEY` | `claude-code-review.yml` (the reviewer model) and any integration tests that use Claude |

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
| `tolokaforge/cli` | Command entrypoints |
| `tolokaforge/core` | Orchestration, grading, metrics, models, search |
| `tolokaforge/core/llm` | LLM abstractions — reasoning, schema, cache, usage, client |
| `tolokaforge/runner` | gRPC runner service (DB client, tool factory, LLM judge) |
| `tolokaforge/executor` | gRPC executor service |
| `tolokaforge/agent` | gRPC agent service |
| `tolokaforge/adapters` | Benchmark adapters (native, tau, tlk_mcp_core) |
| `tolokaforge/secrets` | Secret management (SecretManager, providers, config) |
| `tolokaforge/tools` | Tool registry and builtin tools |
| `tolokaforge/env` | Local environment services (JSON DB, mock web, RAG) |
| `tasks/` | Example tasks (external, cloned separately — see README) |

### Key Subsystems

- **CLI** (`tolokaforge/cli`): Entry point for all commands — `run`, `validate`, `docker`, etc.
- **Core** (`tolokaforge/core`): Orchestration engine, grading pipeline, metrics collection, model interfaces, model capability policies, and task search.
- **Runner** (`tolokaforge/runner`): gRPC service managing benchmark execution, database clients, tool instantiation, and LLM-as-judge evaluation (when `grading.yaml` configures `llm_judge`).
- **Executor** (`tolokaforge/executor`): gRPC service that executes individual agent steps in isolated environments.
- **Agent** (`tolokaforge/agent`): gRPC service wrapping LLM agent interactions.
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

## Python Conventions

### Style and Tooling

- `pyproject.toml` for project configuration
- `uv` for package management
- `ruff` for linting and formatting
- `pytest` for testing

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

### uv Workspace Rules

- **DO NOT** use `[project.optional-dependencies]` in workspace member packages
- All dev dependencies go in root `pyproject.toml` under `[dependency-groups]` → `dev` (PEP 735)
- Workspace members reference each other with `{ workspace = true }` in dependencies
- Every tool in `tools/` must be a uv workspace member (runnable via `uv run <tool-name>`)
- Every tool in `tools/` must register in `pyproject.toml` `[tool.uv.workspace]`

**Current workspace packages:**

- `tools/benchmark-analyzer` — Benchmark result analysis
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

- Bash scripts in `scripts/` organized by subdirectory: `benchmark/`, `setup/`, `lint/`, `tests/`, `release/`, `analysis/`
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
21. **Gemini's tool spec is a JSON-Schema SUBSET — `$defs`/`$ref`/`oneOf`/`discriminator` aren't supported and `additionalProperties:{schema}` silently flattens** — when Pydantic emits any of these (default behaviour for nested models, discriminated unions, and `dict[str, T]` parameters), property names inside the unsupported construct never reach the model. The wire-level symptom looks like "Gemini renames `qty` → `quantity`" / "`subject` → `title`"; the actual cause is schema-loss in transit. Fix lives in [`GeminiSchema`](tolokaforge/core/llm/schema_sanitizer.py) (extends `StrictSchema` with `flatten_oneof_discriminator=True`), routed via `schema_sanitizer: gemini` + `response_policy: array_dict_map` on the `gemini` preset. `prompt_policy: dict_map_hints` does NOT mitigate (the schema info is already gone before the prompt is read). When a new provider exhibits field-rename symptoms, suspect this class of bug *first* — see also rule #4 under "Adding a new model / provider".
22. **Field-omission failures observed in multi-turn evaluations are not single-turn-deterministic** — every registered model (including Gemini 3.1 Pro) passes the synthetic single-turn `test_required_fields_complete` test. The capability is `_CORE_CAPABILITIES`-exempt because every model passes the single-turn baseline; reproducing the multi-turn regression in a synthetic probe needs a multi-turn / heavy-context test variant that does not exist yet.
23. **Empty-content filler injection is a `ToolContentPolicy` capability** — `_convert_messages` substitutes `"I'll help you with that."` for empty assistant content alongside `tool_calls` ONLY when the active `ToolContentPolicy.inject_empty_assistant_filler == True`. Only the Nova `aws_nova` preset opts in (Bedrock rejects empty assistant content on tool turns; commit `73e01e9e6`). Every other preset — `default`, `anthropic*`, `openai_gpt5`, `xai_grok`, `qwen`, `gemini` — leaves the content empty. **Why this matters**: the substitution used to be unconditional. The 2026-04-30 OTS regression analysis showed Gemini pattern-matches the filler in past assistant turns and echoes `"I'll help you with that."` back as its own response content (~26-38% of trials). Live probes confirmed: 2/5 Gemini calls echo the filler when it's in context, 0/5 echo with empty content. Routing pinned by `tests/canonical/test_content_policy_filler_routing.py`.
24. **Task-pack `dict[str, Any]` parameters defeat schema enforcement** — when a tool parameter is declared `dict[str, Any]` in its task-pack Pydantic model (e.g. `tasks/ots_19_airlines/_domain/tools/mcp_tools_library/ots_19_airlines/zendesk/tools/create_item.py:31` `item: dict[str, Any]`), Pydantic generates `{type: object, additionalProperties: true}` with no inner `properties` / `required` list. `StrictSchema` does NOT rewrite `additionalProperties: true` (only `additionalProperties: <schema>`), so every model — including GPT-5.5 — receives the same permissive schema and must rely on system-prompt policy alone to know which inner fields are required. Field-omission failures (e.g. Gemini Pro forgetting `booking_channel` in 188/550 OTS trials) correlate with this shape. **Fix lives in the task pack** (declare a strict inner Pydantic model with explicit fields), not in the harness. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `schema_sanitizer`.
25. **MiniMax-M3 corrupts the `tags` array via XML→JSON tool-call conversion** — every M3 `tags` emission inside the schemaless `additionalProperties: true` `updates` / `item` object is malformed (2505/2505 airlines occurrences). The provider renders a repeated XML element as a single-key dict `{"item": X}` (76 %) or JSON-encodes / empties the array (`'["a"]'` / `''`, 23 %). The `minimax` preset wires `response_policy: minimax_m3_tags` — the `MinimaxM3TagRecoveryResponse` composite (`JsonRecursiveCoerceResponse` then `ItemRecursiveUnwrapResponse`) — which recurses into the parent and recovers the native list. **Scoping is load-bearing**: recovery is restricted to the declared-array tags sites `updates.tags` / `item.tags` via the `ARRAY_SITES` allowlist in [`response_policy.py`](tolokaforge/core/llm/response_policy.py). A schema-agnostic empty-string→`[]` was proven net-harmful — on MiniMax-M2.7 it corrupts scalar fields (`resolution_category__c`, `employee_id`, `keyword`). M2.7 emits native `tags` lists (0 corrupt) so it is NOT in this preset and is unaffected; the policy never touches `None`, never promotes scalar strings, and leaves multi-key dicts unchanged (no guessing). This is distinct from M3's genuine dict-map / discriminated-union model gap (gotcha #22 family), which stays `known_unsupported`. See [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) § `response_policy`.

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
| Troubleshooting | `docs/TROUBLESHOOTING.md` |
