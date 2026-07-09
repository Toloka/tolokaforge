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
evaluations that share a common setup. If a team is running two
hundred tasks that all exercise the same customer-support workflow
against the same backend services, a Project is where they declare
"these tasks belong together, they run against this environment,
they use these models, they grade on this rubric." Once the Project
declares that shared setup, individual tasks only need to describe
what makes them unique — the specific customer complaint, the
expected outcome, the grading criteria.

Without a Project, every task carries its own copy of every setting.
The Project pulls the common bits up to one place, so the tasks
themselves stay small.

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

### Pack = scenario, one-to-one

In practice, a task pack maps to one business or evaluation scenario
(customer-support triage, backend refactoring, deep-research
question-answering, ...). All the tasks in a pack share the same
tools, the same system-prompt frame, the same simulated-user
persona, the same services. The Project layer formalises what has
always been implicit: one pack = one scenario = one Project.

If you want to run tasks from multiple scenarios in a single
invocation, that's a **run-config** concern — list multiple packs
in `evaluation.task_packs` and each pack's Project provides its own
scenario-specific defaults.

## What a Task is

A **Task** is one specific evaluation scenario — one thing the
agent has to accomplish. A task might be "add a `/health/ready`
endpoint to the backend service." Another might be "diagnose why
the `/orders` endpoint is slow and land a fix." A support task
might be "handle a customer's login-reset request under a specific
account state."

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

## How Project and Task compose

For any given task, the loader combines two sources of settings to
produce the effective configuration:

1. **The Project** provides pack-wide defaults (from
   `task_defaults` and section-level defaults).
2. **The Task** provides its own identity and any per-task
   overrides on top.

Task wins on conflict. If a task declares no overrides, it uses
the Project's defaults verbatim. If it declares a partial
override (e.g. one input value on the environment), the merge is
deep-typed — task-declared sub-fields win; everything else
inherits.

Nothing in this model is required to be complex. A pack with ten
similar tasks might have a `project.yaml` and ten one-line
`task.yaml` files. A pack with three hundred tasks all sharing
one scenario has one `project.yaml` declaring the shared setup and
three hundred small task files that mostly say only their own
identity.

### The picture, in one diagram

```
task pack root/
├── project.yaml                    ← ONE Project (pack-wide defaults)
│                                     (models, compute, environment,
│                                      storage, observability, ...)
│
├── shared/                         (optional; assets the Project points at)
│   ├── environment.compose.yaml    (base compose file)
│   └── system_prompt.md            (default system prompt)
│
└── tasks/                          ← ONE OR MORE Tasks
    ├── task_a/
    │   └── task.yaml               (inherits or overrides)
    └── task_b/
        └── task.yaml
```

For a given task, effective config = merge(Project → Task), Task
winning on conflict.

### Familiar shapes from other tools

If you've used Databricks Workflows, MLflow Projects, dbt projects,
or Prefect Deployments, this pattern will feel familiar. Each of
those tools has a "project-like" top-level entity that owns the
shared setup for a collection of items (jobs, entrypoints, models,
flows), with per-item overrides layered on top. TolokaForge's
Project is that top-level entity for AI-agent evaluations; a Task
is the per-item.

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
| `shared/environment.compose.yaml` | pack root or task dir | Base compose file referenced by `default_environment` | Optional |
| `shared/system_prompt.md` | pack root | Default system prompt referenced by `task_defaults.system_prompt` | Optional |
| `task.yaml` | task dir | Task spec (identity, adapter, max_turns, initial_user_message, initial_state, tools, user_simulator, metadata, policies, grading path, system_prompt, adapter_settings, environment_manifest) | Required |
| `grading.yaml` | task dir | Grading rules (combine, state_checks, transcript_rules, llm_judge, custom_checks) | Required |
| `environment.compose.yaml` | task dir | Task-local compose (used when the task overrides the project default) | Optional |
| `initial_state.json` | task dir | DB tables + records + fs state seeded at trial start | Optional |
| `fixtures/` | task dir | Test data referenced by tools/services/initial_state | Optional |

**Adapter-specific files.** Some adapters ship their own
conventions for further sharing beyond `task_defaults` — for
example, the `native` adapter's `_shared/domain.yaml` merge, used
by the [`native_shared_domain`](../../examples/native/native_shared_domain/)
example. Those files are adapter-specific and documented with the
adapter they belong to; they are not part of the Project schema.

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

# ── Task-level defaults ─────────────────────────────────────────
# Applied to every task; task fields override on deep-typed merge.
# This is the primary mechanism for sharing task-level config
# across a pack — system_prompt, tools, user_simulator, max_turns,
# adapter_type all live here and inherit unless a task overrides.
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
| `task_defaults.system_prompt` | Runner system message | Yes — task's `system_prompt` overrides |
| `task_defaults.user_simulator` | `UserSimulator` config | Yes — task's `user_simulator` deep-merges |
| `task_defaults.policies` | Loop policies | Yes — task's `policies` deep-merges |
| `task_defaults.tools` | Adapter tool wiring | Yes — task overrides |
| `task_defaults.adapter_settings` | Adapter-specific settings (bundle paths, tool registry, etc.) | Yes — task's `adapter_settings` deep-merges |
| `task_defaults.grading_defaults` | `TrialGrader` combine method / weights / pass threshold | Yes — task's `grading.yaml.combine` deep-merges |
| `models.agent` / `models.user` / `models.judge` | LLM clients (run-level) | No at task level — `run_config.models` overrides at run level |
| `compute.*` | Orchestrator run-init, `RuntimeBackend` selection | No at task level — `run_config.orchestrator.*` overrides at run level |
| `storage.*` | Artifact writer, log writer, queue backend | No at task level |
| `observability.*` | Tracing/metrics/logging sinks | No at task level |
| `orchestration.*` | Orchestrator run behaviour | No at task level |

Adding a new top-level section is a schema addition on the Project
model. Unknown sections warn but don't fail — older loaders keep
working when new packs declare newer sections.

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
  text) replaces the project prompt entirely; no merge.

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

## Sharing task-level config across many tasks

The Project's `task_defaults` section is the primary mechanism for
sharing task-level fields across every task in a pack:

- `adapter_type` — same for every task in the pack (typical
  pattern: every task uses the same adapter).
- `system_prompt` — the shared system-prompt frame; each task can
  add specifics.
- `tools` — the tool set exposed to the agent.
- `user_simulator` — mode, persona, backstory (each task can
  override the persona or scripted flow).
- `max_turns`, `policies`, `grading_defaults`, `adapter_settings`
  — shared knobs.

This covers the great majority of cross-task sharing needs. A pack
with three hundred customer-support tasks declares the shared
prompt, tools, and persona once in `task_defaults`; individual
tasks contribute only their identity, their specific initial state,
and their specific grading rubric.

**Adapter-specific shared-config conventions.** Some adapters ship
further conventions for sharing patterns that don't fit
`task_defaults` — for example, the `native` adapter's optional
`_shared/domain.yaml` merge (see the
[`native_shared_domain`](../../examples/native/native_shared_domain/)
example). Those conventions are adapter-specific and documented
with the adapter that implements them. The Project schema itself
does not attempt to unify them or model them as first-class
concepts — each adapter documents its own patterns.

## Resolution — task effective config

For every task discovered by `tasks.discovery`, the loader produces
a `TaskDescription` (the wire type from ADR-0003) by layered merge:

```
        project.task_defaults
        (applied to every task)
                │
                ▼
        task.yaml fields
        (per-task overrides)
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

If an adapter ships its own shared-config merge (e.g. native's
`_shared/domain.yaml`), that merge happens between
`project.task_defaults` and the task's own fields, driven by the
adapter, and documented with the adapter. From the Project's
perspective it's a black-box adapter-side operation.

## Resolution — run effective config

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
| `task_defaults` | ✓ | — | Project-only |
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

Highest priority to lowest.

For task-scoped fields:

1. **task.yaml** value.
2. **`project.task_defaults`** value.
3. **Adapter default** (per adapter type; includes any
   adapter-specific shared-config merges like native's
   `_shared/domain.yaml`).
4. **Engine default**.

For run-scoped fields:

1. **CLI flag** (e.g. `--runtime`, `--workers`, `--user-model`).
2. **Environment variable** — infrastructure fields only
   (`DB_SERVICE_URL`, `RAG_SERVICE_URL`, `EXECUTOR_ADDRESS`,
   `TASK_PACKS_DIRS`, provider API keys).
3. **`run_config.yaml`** value.
4. **`project.yaml`** value.
5. **Engine default**.

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
  sections warn but preserve — forward-compat by design.

## Extensibility mechanisms

The Project schema is designed to grow without breaking existing
packs.

- **New top-level sections.** Add a Pydantic model, register it in
  the project schema. Older loaders warn on the unknown key but
  preserve the field.
- **New providers for `compute`.** Ship a `RuntimeBackend`
  implementation, declare an entry-point in
  `tolokaforge.compute_providers`. No fork required.
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
  discovered tasks. Existing task-level `environment_manifest`
  declarations are honoured as full overrides.
- **Packs with `project.yaml` get inheritance** for tasks that
  don't declare overrides. Tasks that do continue to work exactly
  as before.
- **Adding sections to `project.yaml` doesn't break older
  packs.** Older packs simply don't declare those sections; the
  runtime uses hard-coded defaults.
- **Existing adapter-specific shared-config patterns** — the
  native adapter's `_shared/domain.yaml` merge, and any other
  adapter's own conventions — continue to work unchanged. The
  Project layer sits above them; nothing about the merge machinery
  changes.
- **`run_config.yaml`** continues to work exactly as today.
  Fields declared in `run_config` override same-named fields in
  `project`.
- **Adapters** receive a fully-resolved `TaskDescription` and
  validate it as always. The Project layer does not modify
  `TaskDescription` shape.

## Failure modes

- **`llm_judge` without a run-level judge model.** A task's
  `grading.yaml` uses `llm_judge`, but neither
  `project.models.judge` nor `run_config.models.judge` is
  declared. Prevention: orchestrator refuses to start; error
  names the offending task(s) and the missing field.
- **Silent cross-trial contamination.** Default isolation for
  undeclared services is `ephemeral` (fail-loud); `shared` is
  opt-in per service.
- **Silent cross-task contamination.** Same as above.
- **Non-canonical YAML causing hash misses.** Runtime canonicalises
  key order, quoting, and whitespace before hashing.
- **Cross-run assumption.** Model is documented as within-run only.
- **Reset-primitive failure.** Terminates the affected task's
  remaining trials with an explicit reason.
- **Input-override typos.** Input names validated against the
  compose file's declared `${...}` references at load time.
- **Unknown section in `project.yaml`.** Warn but preserve.
- **Adapter validation failure on merged TaskDescription.** Loader
  surfaces the specific merge step (project defaults, task.yaml)
  that contributed the offending field.

## What the model deliberately isn't

- **A first-class Domain tier.** The Project schema does not model
  Domain as a peer or sub-tier. In current TolokaForge usage a pack
  is one scenario, and pack-wide `task_defaults` covers the
  sharing. Any further sharing convention is adapter-specific and
  lives with the adapter, not in the Project.
- **A unifier of adapter-specific sharing conventions.** Each
  adapter documents its own patterns for sharing tools, prompts,
  or code across tasks; the Project schema doesn't try to abstract
  over them.
- **A free-form deep-merge system.** Every override is typed and
  bounded.
- **A three-or-more-level authoring hierarchy.** Two tiers only:
  Project → Task.
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
- `shared/environment.compose.yaml` + `shared/system_prompt.md` —
  the base compose + project-level default prompt.
- A minimal `run_config.yaml` — invocation-only fields.
- Seven tasks demonstrating: full inheritance, partial env override
  (input value), full env override (task-local compose), and
  non-env override (`max_turns`).

Read the pack's
[`README.md`](../../examples/native/example-microservices-pack/README.md)
for the task-by-task walkthrough and the per-scope resolved-config
table.

---

*Return to [`docs/architecture/`](.) for the ADR index and the
canonical runtime-backends documentation.*
