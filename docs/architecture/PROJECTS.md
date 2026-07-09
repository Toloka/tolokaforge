# Projects — the top-level abstraction

This document describes the **Project** as the top-level abstraction
in TolokaForge: what a project owns, how tasks inherit from it, how
per-invocation settings compose on top, and how every config file
that a task pack ships fits into the layered model.

Companion reading:
[`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md) (the `RuntimeBackend`
seam that consumes a project's compute selection),
[`adr/0003-trial-spec-and-trial-result.md`](adr/0003-trial-spec-and-trial-result.md)
(the wire format the loader synthesises into),
[`adr/0009-environment-manifest.md`](adr/0009-environment-manifest.md)
(the `EnvironmentManifest` schema that `default_environment`
extends),
[`adr/0018-multi-container-under-shared-runtime.md`](adr/0018-multi-container-under-shared-runtime.md)
(the isolation case matrix this model preserves).

## What a Project is

A **Project** is a container for a set of related AI-agent
evaluations that share a common setup. If a team is running fifty
tasks that all exercise the same customer-support workflow against
the same backend services, a Project is where they declare "these
fifty tasks belong together, they run against this environment,
they use these models, they grade on this rubric." Once the
Project declares that shared setup, individual tasks only need to
describe what makes them unique — the specific customer complaint,
the expected outcome, the grading criteria.

Without a Project, every task carries its own copy of every
setting. The Project pulls the common bits up to one place, so the
tasks themselves stay small.

Concretely, a Project owns:

- **The default environment** every task runs in — a Docker Compose
  stack of services (databases, backends, tools).
- **The default models** driving the agent, the simulated user, and
  the judge.
- **The compute policy** — where trials run, how much parallelism,
  budget caps, timeouts, stuck-heuristics.
- **The storage backends** for artifacts, logs, and the trial queue.
- **The observability sinks** — where traces, metrics, and logs go.
- **The orchestration policy** — retries, priority, scheduling.
- **Task-level defaults** every task inherits — adapter type, turn
  budget, system prompt, user-simulator persona, tools, grading
  combine method.

One file, `project.yaml` at the pack root, holds all of this. There
is exactly one Project per pack.

## What a Task is

A **Task** is one specific evaluation scenario — one thing the
agent has to accomplish. A task might be "add a
`/health/ready` endpoint to the backend service." Another might be
"diagnose why the `/orders` endpoint is slow and land a fix."

Every task lives in its own directory under `tasks/`:

```
tasks/
├── api_endpoint_add/
│   ├── task.yaml
│   └── grading.yaml
└── db_query_tuning/
    ├── task.yaml
    └── grading.yaml
```

A task's `task.yaml` declares its identity (`task_id`,
`description`) and, optionally, any settings that override the
Project's defaults for this task. A task that declares only
`task_id` and `description` inherits *everything* from the
Project — same environment, same models, same tools, same turn
budget, same grading combine method.

If a task needs something specific — a different postgres version,
its own compose file, a longer turn budget, an isolated database —
it declares only that one field. Everything else continues to
inherit. Small tasks stay small.

There can be many tasks in a Project. There is always at least one.

## What a Domain is

A **Domain** is a bundle of shared settings for a *subset* of tasks
that share a scenario type. Imagine a pack has fifty tasks total,
and twenty of them are customer-support scenarios (with a specific
support-agent system prompt, a specific set of ticket-manipulation
tools, and a specific simulated-customer persona). The other thirty
are backend-engineering tasks with different tools and a different
prompt.

The support-tasks' shared setup doesn't belong in the twenty
`task.yaml` files (duplicated twenty times, drift-prone). It also
doesn't belong in `project.yaml` at the pack level — those fields
wouldn't fit the backend-engineering tasks.

A Domain fills that gap. It's a YAML file (conventionally
`_shared/domain.yaml`) that holds the shared bundle for one
scenario type. Tasks opt in by pointing at the Domain file:

```yaml
# tasks/support_triage_01/task.yaml
task_id: "support_triage_01"
domain: "../../_shared/domain.yaml"
description: "..."
```

That's the entire cost of opting in. The twenty support tasks each
add one line; they all inherit the Domain's tools, prompt, and
persona; the backend tasks don't reference it and stay unaffected.

A Domain can also ship bundled files — a Markdown system prompt,
a Python MCP-server implementation — so a whole "domain package"
lives together in one directory. That's why the pattern uses an
external file rather than an inline block in `project.yaml`:
co-location.

A pack can have zero, one, or several Domains. A task references at
most one Domain today. Many tasks can reference the same Domain.

## How Project, Task, and Domain compose

For any given task, the loader combines three sources of settings
to produce the effective configuration:

1. **The Project** provides pack-wide defaults.
2. **The Domain** the task opts into (if any) layers
   subset-specific defaults on top.
3. **The Task** provides its own identity and any per-task
   overrides on top of that.

Task wins over Domain, Domain wins over Project. If a task declares
no overrides and points at no Domain, it uses the Project's
defaults verbatim. If it points at a Domain, it inherits the
Domain's fields where the Project's defaults would otherwise apply.
If it declares its own fields, those beat both.

Nothing in this model is required to be complex. A pack with ten
similar tasks might have a `project.yaml`, no Domains at all, and
ten one-line `task.yaml` files. A pack with fifty tasks spread
across three scenario types might have a `project.yaml`, three
Domain files under `_shared/`, and fifty task files each with one
line saying which Domain they belong to.

### The picture, in one diagram

```
task pack root/
├── project.yaml                    ← ONE Project (pack-wide defaults)
│                                     (models, compute, environment,
│                                      storage, observability, ...)
│
├── _shared/                        ← ZERO OR MORE Domains
│   ├── domain.yaml                 (subset-specific defaults)
│   ├── system_prompt.md            (bundled with the Domain)
│   └── mcp_server.py               (bundled with the Domain)
│
└── tasks/                          ← ONE OR MORE Tasks
    ├── task_a/
    │   └── task.yaml               (opts into a Domain, or not)
    └── task_b/
        └── task.yaml
```

For a given task, effective config = merge(Project → Domain →
Task), later sources winning on conflict.

### Familiar shapes from other tools

If you've used Databricks Workflows, MLflow Projects, dbt projects,
or Prefect Deployments, this pattern will feel familiar. Each of
those tools has a "project-like" top-level entity that owns the
shared setup for a collection of items (jobs, entrypoints, models,
flows), with per-item overrides layered on top. TolokaForge's
Project is that top-level entity for AI-agent evaluations; a Task
is the per-item; a Domain is a middle layer for subset-specific
shared setup when a single pack contains multiple scenario types.

## Scopes surrounding the Project

The Project is the *authoring* top layer — it's what task authors
maintain in version control. When a project actually runs, two
more scopes come into play above it:

| Scope | Owned by | Lifetime | What lives here |
|---|---|---|---|
| **CLI + env** | Operator | This invocation | `--runtime`, `--user-model`, `--judge-model`, `--presets-file`; env vars for API keys and service URLs |
| **Run** | `run_config.yaml` | This invocation | Which models drive this run, how many workers, output dir for this run, which packs to include |
| **Project** | `project.yaml` | Pack lifetime | Default environment, default models, compute/storage/observability/orchestration policies, task-level defaults inherited by every task |
| **Task** | `task.yaml` + task-adjacent files | One task | Task identity, per-task overrides |
| **Trial** | Runtime-only | One trial | Auto-generated ids, per-trial state — never user-configurable |

`run_config.yaml` picks per-invocation settings (which model, how
many workers, where to write results *this time*). The same Project
can run with many different `run_config.yaml` files — one for CI,
one for a nightly sweep, one for a stakeholder demo — without
editing the Project itself. CLI flags and environment variables sit
above that for one-off overrides.

Trial-level state (the specific docker containers, the specific
trial id) is produced at runtime and is not something a user
configures.

## Config file inventory

The complete set of files a task pack can ship:

| File | Location | Purpose | Required |
|---|---|---|---|
| `project.yaml` | pack root | Pack-level defaults + typed sections | Optional |
| `run_config.yaml` | pack root | Per-invocation config (models, orchestrator run knobs, evaluation choice) | Required for execution |
| `_shared/domain.yaml` | domain dir | A Domain's shared defaults (category, tools, user_simulator, system_prompt), referenced by task-level `domain:` | Optional |
| `_shared/system_prompt.md` | domain dir | System prompt bundled with a Domain | Optional |
| `_shared/mcp_server.py` | domain dir | MCP server bundled with a Domain | Optional |
| `shared/environment.compose.yaml` | pack root or task dir | Base compose file referenced by `default_environment` | Optional |
| `task.yaml` | task dir | Task spec (identity, adapter, max_turns, initial_user_message, initial_state, tools, user_simulator, metadata, policies, grading path, system_prompt, adapter_settings, environment_manifest, domain ref) | Required |
| `grading.yaml` | task dir | Grading rules (combine, state_checks, transcript_rules, llm_judge, custom_checks) | Required |
| `environment.compose.yaml` | task dir | Task-local compose (used when the task overrides the project default) | Optional |
| `initial_state.json` | task dir | DB tables + records + fs state seeded at trial start | Optional |
| `fixtures/` | task dir | Test data referenced by tools/services/initial_state | Optional |

The Project layer adds one file (`project.yaml`) and touches no
existing schemas. Every other file keeps the shape it has today.

## The Project schema

`project.yaml` at pack root:

```yaml
# ── Identity ────────────────────────────────────────────────────
name: "microservices-eval"
version: 1
description: "..."

# ── Task inventory ──────────────────────────────────────────────
tasks:
  discovery:
    glob: "tasks/**/task.yaml"
  # inline: [...]  # UI-managed alternative

# ── Default environment ─────────────────────────────────────────
# Every task inherits unless it declares its own environment_manifest.
# Task-level environment_manifest deep-merges on top per-task.
default_environment:
  compose_file: "./shared/environment.compose.yaml"
  runner_service: "runner"
  inputs:
    postgres_version: "16"
  isolation: "per_trial"
  network_policy: "LOCALHOST_ONLY"
  security_context_defaults:
    user: "toloka"
    group: "toloka"

# ── Task-level defaults (pack-wide) ─────────────────────────────
# Applied to every task; task fields override; Domain defaults (if the
# task opts into a Domain) layer between these and the task.
task_defaults:
  adapter_type: "native"
  max_turns: 20
  system_prompt: "./shared/system_prompt.md"
  user_simulator:
    mode: "llm"
    persona: "curious engineer"
  policies:
    max_tool_calls_per_turn: 10
  metadata: {}
  adapter_settings: {}
  tools: {}
  grading_defaults:
    combine:
      method: "weighted_average"
      pass_threshold: 0.7
      weights:
        state_checks: 0.5
        llm_judge: 0.5

# ── Domains (optional) ──────────────────────────────────────────
# A Domain is a bundle of shared defaults applied only to tasks that
# opt in. Two equivalent forms:
#
# 1. Inline in this project.yaml under `task_groups.<name>`,
#    referenced from a task via `group: <name>` in task.yaml.
# 2. External file (any path under the pack), referenced from a task
#    via `domain: <path>` in task.yaml. Toloka-internal packs use
#    `_shared/domain.yaml` for this — the external form co-locates
#    the Domain's shared prompt and MCP server with its config.
#
# Both forms merge the same way: Domain defaults layer between
# task_defaults and the task's own fields.
#
# task_groups:
#   customer_support:
#     category: "customer_support"
#     tools:
#       agent:
#         enabled: ["read_ticket", "update_ticket", "search_kb"]
#     system_prompt: "./prompts/support.md"
#     user_simulator:
#       persona: "frustrated support customer"

# ── Models ──────────────────────────────────────────────────────
models:
  agent:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.0
  user:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.2
  judge:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.0

# ── Compute ─────────────────────────────────────────────────────
compute:
  provider: "local-docker"
  workers: 4
  max_budget_usd: 100.0
  max_requests_per_second: 10.0
  max_attempt_retries: 3
  runtime_mode: "per_trial"
  timeouts:
    trial_seconds: 600
    tool_call_seconds: 60
  stuck_heuristics:
    enabled: true
    max_repeated_tool_calls: 5
    max_idle_turns: 3
  typesense:
    enabled: false
    mode: "disabled"
  # Provider-specific sub-sections:
  # kubernetes:
  #   cluster: "prod-cluster"
  #   namespace: "toloka"

# ── Storage ─────────────────────────────────────────────────────
storage:
  artifacts:
    type: "local"
    path: "./results"
  logs:
    type: "local"
    path: "./logs"
  queue:
    backend: "sqlite"
    # postgres_dsn: "postgresql://..."

# ── Observability ───────────────────────────────────────────────
observability:
  tracing:
    exporter: "none"
  metrics:
    exporter: "none"
  logging:
    level: "INFO"
    exporter: "stdout"

# ── Orchestration ───────────────────────────────────────────────
orchestration:
  auto_start_services: true
  continue_prompt: "Continue."
  shuffle_trials: false
  # schedule:
  #   cron: "0 6 * * *"
```

### Section responsibilities

| Section | Feeds | Overridable at task level |
|---|---|---|
| `identity` (`name`, `version`, `description`) | Project registry, UI | No |
| `tasks.discovery` | Loader task discovery | No |
| `default_environment` | `RuntimeBackend` per-trial provision | Yes — task's `environment_manifest` deep-merges |
| `task_defaults.adapter_type` | Adapter selection per task | Yes — task's `adapter_type` overrides |
| `task_defaults.max_turns` | `ToolCallingLoop` per-trial budget | Yes — task's `max_turns` overrides |
| `task_defaults.system_prompt` | Runner system message | Yes — task's `system_prompt` overrides (a Domain's `system_prompt`, if the task opts in, layers between) |
| `task_defaults.user_simulator` | `UserSimulator` config | Yes — task's `user_simulator` deep-merges |
| `task_defaults.policies` | Loop policies | Yes — task's `policies` deep-merges |
| `task_defaults.tools` | Adapter tool wiring | Yes — task or opted-in Domain adds/replaces |
| `task_defaults.grading_defaults` | `TrialGrader` combine method / weights / pass threshold | Yes — task's `grading.yaml.combine` deep-merges |
| `task_groups.<name>` / external Domain file | Domain defaults for tasks that opt in via `group:` or `domain:` | Yes — task fields override Domain defaults |
| `models.agent` / `models.user` / `models.judge` | LLM clients (run-level) | No at task level — `run_config.models` overrides at run level |
| `compute.*` | Orchestrator run-init, `RuntimeBackend` selection | No at task level — `run_config.orchestrator.*` overrides at run level |
| `storage.*` | Artifact writer, log writer, queue backend | No at task level |
| `observability.*` | Tracing/metrics/logging sinks | No at task level |
| `orchestration.*` | Orchestrator run behaviour | No at task level |

## Task override semantics

Two patterns cover every task-level override:

### Partial override — deep-merge

Any typed sub-field a task declares wins over the corresponding
project default; everything else inherits.

```yaml
# tasks/postgres_upgrade_test/task.yaml
task_id: "postgres_upgrade_test"
description: "..."

environment_manifest:
  inputs:
    postgres_version: "17"          # overrides project default of "16"
```

The task's resolved manifest = merge(`project.default_environment`,
`task.environment_manifest`, task-wins).

### Full override — replace entirely

Some fields replace instead of merge:

- **`environment_manifest.compose_file`**: pointing at a different
  file replaces the compose reference entirely. The task no longer
  shares the project's runtime stack.
- **`system_prompt`**: pointing at a different file (or inline
  text) replaces the project/Domain prompt entirely; no merge.

```yaml
# tasks/schema_isolation_migration/task.yaml
task_id: "schema_isolation_migration"
description: "..."

environment_manifest:
  compose_file: "./environment.compose.yaml"
  runner_service: "runner"
```

### Fields that cannot be project-scoped

- `task_id`, `name`, `description` — inherent identity.
- `initial_state.json` payload — per-task seed data.
- `grading.yaml`'s `llm_judge.rubric`, `state_checks`,
  `transcript_rules`, `custom_checks` — must be explicit per task
  for audit.
- Fixture file contents — per-task data.

## Domains — the detailed spec

"What a Domain is" (above) covers the concept. This section goes
into the mechanics: the two forms a Domain can take, how paths
resolve, and which fields a Domain is allowed to declare.

### External form (`_shared/*.yaml`)

The pattern Toloka-internal packs already use, and the one that
scales best when a Domain has bundled Markdown or Python
assets. A Domain's config lives in a file under any convenient
directory — conventionally `_shared/domain.yaml`. Tasks opt in via
a `domain:` field in `task.yaml`:

```yaml
# tasks/support_triage_01/task.yaml
task_id: "support_triage_01"
domain: "../../_shared/domain.yaml"
description: "..."
```

```yaml
# _shared/domain.yaml
category: "customer_support"
tools:
  agent:
    enabled: ["read_ticket", "update_ticket", "search_kb"]
    mcp_server: "./mcp_server.py"
system_prompt: "./system_prompt.md"
user_simulator:
  persona: "frustrated support customer"
```

The loader (`tolokaforge/adapters/_task_loader.py:141`) deep-merges
the Domain's fields onto the task; task fields win on conflict.

**Path resolution inside a Domain file.** Relative paths inside
`domain.yaml` (e.g. `./system_prompt.md`, `./mcp_server.py`) are
resolved from the Domain file's directory, *not* from each
referring task's directory. That means a Domain can ship its own
system prompt and MCP server alongside `domain.yaml`, and every
task that opts in picks them up correctly regardless of where the
task lives in the pack. The whole Domain package moves as a unit.

### Inline form (`task_groups` in `project.yaml`)

For Domains that are pure YAML — no bundled `.md` or `.py` — the
same defaults can live directly in `project.yaml` under
`task_groups.<name>`. Tasks opt in via a `group:` field in
`task.yaml`:

```yaml
# project.yaml (excerpt)
task_groups:
  polite_customer:
    user_simulator:
      persona: "polite enterprise customer"
      backstory: "..."
```

```yaml
# tasks/enterprise_triage/task.yaml
task_id: "enterprise_triage"
group: "polite_customer"
description: "..."
```

The schema field is called `task_groups` because it's a *group* of
tasks (the ones that reference it by name) — but conceptually
it's the same thing as a Domain. Merge semantics are identical to
the external form: the Domain's fields layer between
`task_defaults` and the task's own fields, with the task winning.

The inline form is the right pick when a Domain has no bundled
assets and the pack's project.yaml is small enough that the extra
sections don't crowd it. External form is the right pick when the
Domain ships with Markdown/Python assets, or when the pack has
many Domains and each deserves its own directory.

### Fields a Domain may declare

A Domain currently declares:

- `category` — a label used for grouping in reporting and UI.
- `tools` — the tool set exposed to the agent (`tools.agent.enabled`,
  `tools.agent.mcp_server`, `tools.user.*`).
- `user_simulator` — mode, persona, backstory, scripted_flow.
- `system_prompt` — path to a Markdown file, or inline text.

Fields not in this list stay task-scoped even when a Domain file
declares them. Extending the set is a schema addition on the
Domain model — additive, backward-compatible.

## Resolution — task effective config

For every task discovered by `tasks.discovery`, the loader produces
a `TaskDescription` (the wire type from ADR-0003) by layered merge:

```
                project.task_defaults
                (applied to every task)
                        │
                        ▼
                Domain defaults
                (if task opts in via `group:` or `domain:`)
                        │
                        ▼
                task.yaml
                        │
                        ▼
    environment_manifest merge with project.default_environment
                        │
                        ▼
              grading.yaml
              (merged with project.task_defaults.grading_defaults)
                        │
                        ▼
                Adapter validates the final shape
                        │
                        ▼
                    TaskDescription
                (the wire format for the runner)
```

Each step preserves the invariants of the layer above: if the
project declares `isolation: per_trial` in `default_environment`,
that stays enforced (ADR-0009); if a task's `grading.yaml` uses
`llm_judge`, a run-level `models.judge` must be present.

## Resolution — the run effective config

For the invocation itself, the loader combines Project and
run_config, then applies CLI + env overrides:

```
    Engine defaults
        │
        ▼
    project.yaml.<run-relevant sections>
    (models, compute, storage, observability, orchestration)
        │
        ▼
    run_config.yaml
    (models overrides, orchestrator run knobs, evaluation, engine)
        │
        ▼
    Environment variables
    (API keys, DB_SERVICE_URL, RAG_SERVICE_URL, TASK_PACKS_DIRS, ...)
        │
        ▼
    CLI flags
    (--runtime, --user-model, --judge-model, --presets-file, --workers)
        │
        ▼
    Effective RunConfig
```

## Relationship to `run_config.yaml`

`project.yaml` and `run_config.yaml` cover different concerns.

- **`project.yaml`** owns settings **invariant across runs** —
  what the project *is*. Every invocation reads these.
- **`run_config.yaml`** owns settings **specific to a single
  invocation** — how *this run* is configured. A pack can be run
  many times with different `run_config.yaml` files.

### Field ownership and precedence

Some fields live only in one file; others may appear in both, with
`run_config.yaml` winning on conflict.

| Setting | project.yaml | run_config.yaml | Resolution |
|---|---|---|---|
| `default_environment` | ✓ | — | Project-only |
| `task_defaults` / `task_groups` | ✓ | — | Project-only |
| `tasks.discovery` | ✓ | — | Project-only |
| `models.agent` / `models.user` / `models.judge` | ✓ default | ✓ override | run_config overrides |
| `compute.provider` | ✓ | — | Project-only |
| `compute.<provider>` sub-sections | ✓ | — | Project-only |
| `compute.workers` | ✓ default | `orchestrator.workers` | run_config overrides |
| `compute.runtime_mode` | ✓ default | `orchestrator.runtime` | CLI > run_config > project |
| `compute.max_budget_usd` | ✓ default | `orchestrator.max_budget_usd` | run_config overrides |
| `compute.timeouts` / `stuck_heuristics` / `typesense` | ✓ default | `orchestrator.*` | run_config overrides sub-fields |
| `storage.artifacts.path` | ✓ default | `evaluation.output_dir` | run_config overrides |
| `storage.queue.backend` | ✓ default | `orchestrator.queue_backend` | run_config overrides |
| `observability.*` | ✓ | — | Project-only |
| `orchestration.*` | ✓ default | `orchestrator.*` (matching sub-fields) | run_config overrides |
| `orchestrator.repeats` / `max_turns` (run-wide cap) | — | ✓ | run_config-only |
| `evaluation.task_packs` | — | ✓ | run_config-only |
| `evaluation.harness_adapter` | — | ✓ | run_config-only |
| `engine.presets_file` | — | ✓ | run_config-only |

### Precedence chain — every field

Highest priority to lowest:

1. **CLI flag** (e.g. `--runtime`, `--workers`, `--user-model`).
2. **Environment variable** — for infrastructure fields only
   (`DB_SERVICE_URL`, `RAG_SERVICE_URL`, `EXECUTOR_ADDRESS`,
   `TASK_PACKS_DIRS`, provider API keys).
3. **`run_config.yaml`** value.
4. **`project.yaml`** value.
5. **Engine default** (from the Pydantic model).

For task-scoped fields the chain resolves inside the Project scope:

1. **task.yaml** value.
2. **Domain defaults** (if the task opts in via `group:` or
   `domain:`).
3. **`project.task_defaults`** value.
4. **Adapter default** (per adapter type).
5. **Engine default**.

That resolved `TaskDescription` then interacts with the run-scoped
chain above at execution time.

### Why the split

Keeping the two files separate lets a single Project spec run under
many different configurations without editing it. A CI pipeline runs
with one `run_config.yaml` (parallel workers, fast model), a nightly
regression sweep runs with another (more repeats, stronger model,
larger output volume), and a stakeholder demo runs with a third —
all against the same `project.yaml`.

## Runtime mechanism — content-addressed stack dedup

The `RuntimeBackend` computes a stable hash over each task's resolved
`EnvironmentManifest` (compose file + bound inputs + per-service
isolation modes). Tasks whose hashes match share one running stack
within a single run.

```
                    project.yaml declares default_environment
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
       task_a (inherits)     task_b (inherits)     task_c (partial override)
              │                     │                     │
       resolved=X            resolved=X            resolved=Y
              │                     │                     │
              └────── same ─────────┘                     │
                       │                                  │
              ┌────────▼────────┐               ┌─────────▼────────┐
              │   stack #1      │               │    stack #2      │
              │   hash=X        │               │    hash=Y        │
              └─────────────────┘               └──────────────────┘
```

Content-addressing is what makes the model correct: when tasks
inherit the project default, their resolved manifests are byte-
identical, hash identical, and share a stack. When a task overrides
a single input (partial), its resolved manifest hashes differently
and it gets its own stack. When a task overrides `compose_file`
(full), it hashes differently and gets its own stack. The schema
declares intent; the hash makes the intent operative.

**Registry and lifecycle.** The backend maintains an in-run registry
keyed by hash. The first task with a given hash provisions a fresh
stack and takes the initial reference. Every subsequent task with
the same hash increments the reference count. When a task finishes
its last trial the count decrements; when it drops to zero the
stack tears down.

**Hash inputs.** The canonicalised YAML representation of the
resolved compose file, the ordered service-name → isolation-mode
map, and the bound input values. Hash function: SHA-256.
Canonicalisation normalises key ordering, quoting, and whitespace
so syntactically-different-but-semantically-identical compose files
hash identically.

**Scope.** A single `tolokaforge run` invocation. Cross-run
persistence is a separate concern.

## Per-service isolation vocabulary

Services in the compose file are annotated with an isolation mode
via TolokaForge extension labels:

```yaml
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
    labels:
      tolokaforge.isolation: "reset"
      tolokaforge.reset_primitive: "postgres_template_db"
```

| Mode | Behaviour | When to pick |
|---|---|---|
| `ephemeral` | Service is torn down and recreated between trials | Safest; trials mutate state destructively |
| `shared` | Service persists across trials on the same stack | Cheapest; safe only when trials don't mutate service state |
| `reset` | Service persists across trials; state resets between trials via a named primitive | Middle-ground; state-mutating tasks with cheap reset semantics |

The default when a service does not declare an isolation mode is
`ephemeral` (fail-loud).

Reset primitives are a closed enum:

```python
ResetPrimitive = Literal[
    "postgres_template_db",       # CREATE DATABASE new TEMPLATE base
    "sqlite_truncate",            # TRUNCATE across declared tables
    "filesystem_workspace_swap",  # rsnapshot-style dir swap
    "redis_flushdb",              # single-DB flush
    "none",                       # explicit no-op for shared-safe services
]
```

New primitives extend the enum via a schema change in tolokaforge
itself. There is no plugin mechanism.

**Interaction with dedup.** The isolation-mode map is part of the
hash input. Two tasks that agree on the compose file but disagree
on `services.postgres.isolation` do not share a stack.

## Compute providers

The `compute.provider` field selects the `RuntimeBackend`
implementation via a registry:

- `local-docker` — provisions stacks via Docker Compose on the
  orchestrator host.
- `kubernetes` — provisions stacks as Pods on a target cluster.
  Provider-specific sub-section (`compute.kubernetes.cluster`,
  `namespace`, `resource_class`, `service_account`).
- `aws-batch` — job-queue backend for batch workloads.
- `modal`, `gcp-batch`, `azure-container-instances` — additional
  substrates.

**Third-party providers** register via the entry-point group
`tolokaforge.compute_providers`. A third-party provider ships its
own package, implements `RuntimeBackend`, and declares an
entry-point; packs reference it by string tag in `compute.provider`.

**Provider-specific configuration.** Each provider declares its own
typed sub-section under `compute.<provider>`. The loader validates
the sub-section against the selected provider's schema; sub-sections
for unselected providers are ignored.

**Deployment/profile layer.** A `deployments/<name>.yaml` layer
above the Project provides values for placeholders in the project
spec — the same project runs on `dev-cluster` under one deployment
and `prod-cluster` under another without changing `project.yaml`.

## Component-to-scope map

Every component that consumes config reads from a specific set of
scopes.

| Component | Lifecycle | Scopes read |
|---|---|---|
| CLI | Invocation | CLI flags |
| Orchestrator | Run init | Run + Project + Env |
| Conductor | Per-trial body | Task (resolved) + Run |
| TrialExecutor | Per-trial bracket | Task (resolved env) |
| TrialGrader | Per-trial grading | Task (grading.yaml) + Run (`models.judge`) |
| RuntimeBackend | Provision / teardown | Project (`compute`) + Task (`environment_manifest`) + Env (service URLs) |
| ToolCallingLoop | Per-trial body | Task (`max_turns`) + Project (`compute.timeouts`, `compute.stuck_heuristics`) |
| Adapter | Run init | Run (`evaluation.harness_adapter`) + Task (`adapter_type`, `adapter_settings`) |
| UserSimulator | Per-trial body | Task (`user_simulator`) + Run (`models.user` when `mode: llm`) |
| TypeSense | Per-trial provision | Project (`compute.typesense`) + Task (`SearchConfig`) |
| LLM clients | Run init | Run + Project (`models.*`) + Env (API keys) |

## UI-friendliness

The schema is designed for UI editing.

- **Section-per-form.** Every section under the Project is its own
  Pydantic model. A UI renders one form section per model, driven
  by the model's JSON Schema.
- **Domains as reusable bundles.** A UI can present Domains as a
  library of shared-config templates that tasks pick from,
  independent of whether they live inline in `project.yaml` (under
  `task_groups`) or in external files.
- **Typed primitives everywhere.** Sub-fields are strings, ints,
  enums, references (by name) to other resources, or further typed
  sub-objects. No untyped free-form fields.
- **Task inventory modes.** `tasks.discovery.glob` is VCS-managed;
  `tasks.inline: [...]` is a UI-managed list of task records
  embedded in the Project. Both modes coexist.
- **Provider selection triggers sub-section reveal.**
  `compute.provider: kubernetes` makes the `compute.kubernetes`
  block meaningful; UI shows only the sub-section matching the
  current provider.
- **Version field.** `project.version` gives the UI a lever for
  schema migration when the shape breaks. Unknown top-level
  sections warn but preserve.

## Extensibility mechanisms

The Project schema is designed to grow without breaking existing
packs.

- **New top-level sections.** Add a Pydantic model, register it in
  the project schema. Older loaders warn on the unknown key but
  preserve the field.
- **New Domain-mergeable fields.** Extend the Domain schema
  alongside the underlying `TaskDescription` fields.
- **New providers for `compute`.** Ship a `RuntimeBackend`
  implementation, declare an entry-point in
  `tolokaforge.compute_providers`.
- **New adapter types.** Register in the `tolokaforge.adapters`
  entry-point group.
- **New reset primitives.** Schema enum extension in tolokaforge
  itself.
- **New task-defaults fields.** Extend the `task_defaults` model
  alongside the underlying `TaskDescription` schema.
- **Deployment/profile layer above the project.** Slots in without
  changing the Project schema.
- **Workspace/organisation layer above projects.** For
  cross-project quotas, permissions, and cost accounting.

## Backward compatibility

- **Packs without `project.yaml` work unchanged.** The loader
  synthesises a default Project from `run_config.yaml` +
  discovered tasks.
- **Packs with `project.yaml` get inheritance** for tasks that
  don't declare overrides. Tasks that do continue to work exactly
  as before.
- **Adding sections to `project.yaml` doesn't break older
  packs.** Older packs simply don't declare those sections; the
  runtime uses hard-coded defaults.
- **Existing `_shared/domain.yaml` semantics preserved verbatim.**
  The external Domain form is exactly the pattern that already
  ships. Task-level `domain:` references continue to work.
- **`run_config.yaml`** continues to work exactly as today.
  Fields declared in `run_config` override same-named fields in
  `project`.
- **Adapters** receive a fully-resolved `TaskDescription` and
  validate it as always. The Project layer does not modify
  `TaskDescription` shape.

## Failure modes

- **`llm_judge` without a run-level judge model.** Orchestrator
  refuses to start; error names the offending task(s) and the
  missing field.
- **Silent cross-trial contamination.** Default isolation for
  undeclared services is `ephemeral`; `shared` is opt-in.
- **Silent cross-task contamination.** Same as above.
- **Non-canonical YAML causing hash misses.** Runtime canonicalises
  before hashing.
- **Cross-run assumption.** Model is documented as within-run only.
- **Reset-primitive failure.** Terminates the affected task's
  remaining trials with an explicit reason.
- **Input-override typos.** Input names validated against the
  compose file's declared `${...}` references at load time.
- **Unknown section in `project.yaml`.** Warn but preserve.
- **Unknown Domain reference.** A task's `group:` or `domain:`
  refers to a Domain that doesn't exist. Loader fails at load time
  with a clear error.
- **Adapter validation failure on merged TaskDescription.** Loader
  surfaces the specific merge step (project defaults, Domain
  defaults, task.yaml) that contributed the offending field.

## What the model deliberately isn't

- **A separate "category" or "sub-project" tier.** Domains are a
  mechanism inside the Project scope, not a peer tier. Authoring
  stays two-tier (Project → Task); Domains scope which tasks pick
  up which defaults, layered between Project and Task at merge time.
- **A free-form deep-merge system.** Every override is typed and
  bounded.
- **A plugin registry for reset primitives.** New primitives
  extend the engine's schema enum.
- **A cross-run stack persistence surface.** All sharing is
  within a single `tolokaforge run` invocation.
- **In-place editing of compose files.** Compose files are read
  at load time, input-substituted in memory, hashed, and
  materialised.
- **A parallel wire format.** The Project layer merges INTO the
  existing `TaskDescription` from ADR-0003; no new wire type.

## Worked example

See
[`examples/native/example-microservices-pack/`](../../examples/native/example-microservices-pack/).
The pack ships:

- `project.yaml` at pack root — every section declared.
- `_shared/domain.yaml` + `_shared/system_prompt.md` — a Domain
  demonstrating the external co-located form.
- `shared/environment.compose.yaml` + `shared/system_prompt.md` —
  the base compose + project-level default prompt.
- A minimal `run_config.yaml` — invocation-only fields.
- Nine tasks demonstrating: full inheritance, partial env override,
  full env override, non-env override (`max_turns`), Domain opt-in
  via `domain:`, Domain + task-level nested override.

Read the pack's
[`README.md`](../../examples/native/example-microservices-pack/README.md)
for the task-by-task walkthrough and the per-scope resolved-config
table.

---

*Return to [`docs/architecture/`](.) for the ADR index and the
canonical runtime-backends documentation.*
