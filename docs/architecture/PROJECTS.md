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

## The mental model — four scopes, layered

Every config concern lives at exactly one primary scope. Higher
scopes may declare defaults that lower scopes inherit; the effective
value of a field depends on which scopes express it.

| Scope | Owned by | Lifetime | What lives here |
|---|---|---|---|
| **CLI + env** | Operator | This invocation | `--runtime`, `--user-model`, `--judge-model`, `--presets-file`; env vars for API keys and service URLs |
| **Run** | `run_config.yaml` | This invocation | Which models drive this run, how many workers, output dir for this run, which packs to include |
| **Project** | `project.yaml` | Pack lifetime | Default environment, default models, compute/storage/observability/orchestration policies, task-level defaults inherited by every task |
| **Category** | `_shared/domain.yaml` | Subset of tasks in one category | Shared category name, shared tools, shared user_simulator, shared system_prompt (deep-merged into referring task.yamls) |
| **Task** | `task.yaml` + task-adjacent files | One task | Task identity, per-task overrides of everything above |
| **Trial** | Runtime-only | One trial | Auto-generated ids, per-trial state — never user-configurable |

The Project sits at the top of the authoring stack. Category
(`_shared/domain.yaml`) is a real, load-bearing tier between Project
and Task — many task packs today already use it to share tools and
system prompts across a subset of tasks. The Project layer sits
above Category; Category sits above Task. Three levels total in the
authoring hierarchy, plus Run above (for invocation-specific
concerns) and CLI/env above that.

## Config file inventory

The complete set of files a task pack can ship:

| File | Location | Purpose | Required |
|---|---|---|---|
| `project.yaml` | pack root | Pack-level defaults + typed sections | Optional |
| `run_config.yaml` | pack root | Per-invocation config (models, orchestrator run knobs, evaluation choice) | Required for execution |
| `_shared/domain.yaml` | category dir | Category-level shared config (category, tools, user_simulator, system_prompt) | Optional |
| `_shared/system_prompt.md` | category dir | Shared LLM system instruction | Optional |
| `_shared/mcp_server.py` | category dir | MCP server implementation for shared tools | Optional |
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
# Glob discovery is VCS-managed; an inline list of task records is
# UI-managed. Both modes coexist.
tasks:
  discovery:
    glob: "tasks/**/task.yaml"
  # inline: [...]  # UI-managed alternative

# ── Default environment ─────────────────────────────────────────
# Full EnvironmentManifest shape. Every task inherits unless it
# declares its own environment_manifest. Task-level environment_manifest
# deep-merges on top per-task.
default_environment:
  compose_file: "./shared/environment.compose.yaml"
  runner_service: "runner"
  inputs:
    postgres_version: "16"
  isolation: "per_trial"                # per ADR-0009 enforcement
  network_policy: "LOCALHOST_ONLY"
  security_context_defaults:
    user: "toloka"
    group: "toloka"

# ── Task-level defaults ─────────────────────────────────────────
# Inherited by every task under tasks.discovery. Category
# (_shared/domain.yaml) layers on top of these; task.yaml layers
# on top of category.
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
  tools: {}                        # tool defaults; category or task adds/replaces
  grading_defaults:                # merges into each task's GradingConfig
    combine:
      method: "weighted_average"
      pass_threshold: 0.7
      weights:
        state_checks: 0.5
        llm_judge: 0.5

# ── Models ──────────────────────────────────────────────────────
# Default agent / user / judge models. run_config.yaml MAY declare
# its own; run_config wins on conflict. When any task uses llm_judge,
# either project.models.judge or run_config.models.judge MUST be
# present — the orchestrator refuses to start otherwise.
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
# Provider selection, resource limits, backend mode, TypeSense,
# stuck-heuristics, timeouts, budget.
compute:
  provider: "local-docker"         # local-docker, kubernetes,
                                   # aws-batch, modal, ...
  workers: 4                       # default; run_config.orchestrator.workers overrides
  max_budget_usd: 100.0
  max_requests_per_second: 10.0
  max_attempt_retries: 3
  runtime_mode: "per_trial"        # shared | per_trial within the provider
  timeouts:
    trial_seconds: 600
    tool_call_seconds: 60
  stuck_heuristics:
    enabled: true
    max_repeated_tool_calls: 5
    max_idle_turns: 3
  typesense:
    enabled: false
    mode: "disabled"               # local | remote | disabled
  # Provider-specific sub-sections live under `compute.<provider>`.
  # kubernetes:
  #   cluster: "prod-cluster"
  #   namespace: "toloka"
  #   resource_class: "gpu-large"

# ── Storage ─────────────────────────────────────────────────────
# Artifacts, logs, queue backend, fixtures.
storage:
  artifacts:
    type: "local"                  # local | s3 | gcs | azure-blob
    path: "./results"
  logs:
    type: "local"
    path: "./logs"
  queue:
    backend: "sqlite"              # sqlite | postgres
    # postgres_dsn: "postgresql://..."

# ── Observability ───────────────────────────────────────────────
# Tracing, metrics, logging exporters.
observability:
  tracing:
    exporter: "none"               # none | otlp
    # endpoint: "http://collector:4317"
  metrics:
    exporter: "none"               # none | prometheus
    # endpoint: "http://prom:9090"
  logging:
    level: "INFO"
    exporter: "stdout"             # stdout | otlp

# ── Orchestration ───────────────────────────────────────────────
# Auto-start policies, continue prompts, shuffle, schedule.
orchestration:
  auto_start_services: true
  continue_prompt: "Continue."
  shuffle_trials: false
  # schedule:
  #   cron: "0 6 * * *"
```

Every section maps 1:1 to an existing config concern in the codebase
today. Nothing invented; every field has a home in an existing
component's consumption graph.

### Section responsibilities

| Section | Feeds | Overridable at task level |
|---|---|---|
| `identity` (`name`, `version`, `description`) | Project registry, UI | No |
| `tasks.discovery` | Loader task discovery | No |
| `default_environment` | `RuntimeBackend` per-trial provision | Yes — task's `environment_manifest` deep-merges |
| `task_defaults.adapter_type` | Adapter selection per task | Yes — task's `adapter_type` overrides |
| `task_defaults.max_turns` | `ToolCallingLoop` per-trial budget | Yes — task's `max_turns` overrides |
| `task_defaults.system_prompt` | Runner system message | Yes — task's `system_prompt` overrides (category layer may also override) |
| `task_defaults.user_simulator` | `UserSimulator` config | Yes — task's `user_simulator` deep-merges |
| `task_defaults.policies` | Loop policies | Yes — task's `policies` deep-merges |
| `task_defaults.tools` | Adapter tool wiring | Yes — task or category adds/replaces |
| `task_defaults.grading_defaults` | `TrialGrader` combine method / weights / pass threshold | Yes — task's `grading.yaml.combine` deep-merges |
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
                                    # (compose_file, runner_service, etc.
                                    # inherit from project.default_environment)
```

The task's resolved manifest = merge(`project.default_environment`,
`task.environment_manifest`, task-wins).

The same rule applies to any other section a task overrides:
`user_simulator`, `policies`, `grading.combine`, `metadata`,
`adapter_settings`.

### Full override — replace entirely

Some fields replace instead of merge:

- **`environment_manifest.compose_file`**: pointing at a different
  file replaces the compose reference entirely. The task no longer
  shares the project's runtime stack.
- **`system_prompt`**: pointing at a different file (or inline
  text) replaces the project/category prompt entirely; no merge.

```yaml
# tasks/schema_isolation_migration/task.yaml
task_id: "schema_isolation_migration"
description: "..."

environment_manifest:
  compose_file: "./environment.compose.yaml"    # replaces project default
  runner_service: "runner"
```

### Fields that cannot be project-scoped

Some fields are inherent to the task and can only live at task
level:

- `task_id`, `name`, `description` — identity.
- `initial_state.json` payload — per-task seed data.
- `grading.yaml`'s `llm_judge.rubric`, `state_checks`,
  `transcript_rules`, `custom_checks` — must be explicit per task
  for audit.
- Fixture file contents — per-task data.

## The category tier — `_shared/domain.yaml`

`_shared/domain.yaml` under a category directory declares
category-level shared config. A task references it via a `domain:`
field in `task.yaml`:

```yaml
# tasks/support_triage/some_task/task.yaml
task_id: "support_triage_01"
domain: "../../_shared/domain.yaml"   # relative to task dir
description: "..."
```

The loader deep-merges the domain fields onto the task, with task
fields winning on conflict. Currently, category-mergeable fields
are:

- `category` — the task's category label.
- `tools` — the tool set exposed to the agent.
- `user_simulator` — the simulated-user config (mode, persona,
  scripted_flow).
- `system_prompt` — the LLM system instruction (path or inline).

Relative paths in the domain file (e.g. references to
`system_prompt.md` or `mcp_server.py` living next to `domain.yaml`)
are rewritten by the loader to resolve from the domain-file
directory, not the task directory — so a category can bundle its
shared prompt and MCP server alongside `domain.yaml` and every
referring task picks them up correctly.

### Where category sits in the layered merge

The full task-config resolution order — highest priority to lowest:

1. `task.yaml` fields.
2. `_shared/domain.yaml` fields (if the task references a domain).
3. `project.yaml.task_defaults` fields.
4. Adapter-level defaults (per adapter type).
5. Engine defaults (Pydantic model defaults).

Every level layers via deep-typed merge. No free-form merge on
untyped fields.

## Resolution — the task effective config

For every task discovered by `tasks.discovery`, the loader produces
a `TaskDescription` (the wire type from ADR-0003) by layered merge:

```
                    project.yaml.task_defaults
                             │
                             ▼
                    _shared/domain.yaml
                    (if task.yaml has `domain:`)
                             │
                             ▼
                    task.yaml
                             │
                             ▼
        environment_manifest merge with project.default_environment
                             │
                             ▼
              grading.yaml
              (combined with project.task_defaults.grading_defaults)
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

Highest priority to lowest:

1. **CLI flag** (e.g. `--runtime`, `--workers`, `--user-model`).
2. **Environment variable** — for infrastructure fields only
   (`DB_SERVICE_URL`, `RAG_SERVICE_URL`, `EXECUTOR_ADDRESS`,
   `TASK_PACKS_DIRS`, provider API keys).
3. **`run_config.yaml`** value.
4. **`project.yaml`** value.
5. **Engine default** (from the Pydantic model).

For task-scoped fields the chain forks separately after Project:
project.task_defaults → category domain.yaml → task.yaml → adapter
default → engine default. That resolved TaskDescription then interacts
with the run-scoped fields above at execution time.

### Why the split

Keeping the two files separate lets a single Project spec run under
many different configurations without editing it. A CI pipeline runs
with one `run_config.yaml` (parallel workers, fast model), a nightly
regression sweep runs with another (more repeats, stronger model,
larger output volume), and a stakeholder demo runs with a third —
all against the same `project.yaml`. That's the same separation of
*invariant spec* from *variant execution* that a
`deployments/<name>.yaml` layer will formalise at broader scope.

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
  by the model's JSON Schema. New sections shipped by tolokaforge
  extend the schema; the UI adapts without bespoke code.
- **Category tier in the tree.** A UI shows Project → Category →
  Task as a browsable tree; each level is editable independently.
- **Typed primitives everywhere.** Sub-fields are strings, ints,
  enums, references (by name) to other resources, or further typed
  sub-objects. No untyped free-form fields.
- **Task inventory modes.** `tasks.discovery.glob` is VCS-managed;
  `tasks.inline: [...]` is a UI-managed list of task records
  embedded in the Project. Both modes coexist; the loader
  normalises to an internal list.
- **Provider selection triggers sub-section reveal.**
  `compute.provider: kubernetes` makes the `compute.kubernetes`
  block meaningful; UI shows only the sub-section matching the
  current provider (discriminated union).
- **Version field.** `project.version` gives the UI a lever for
  schema migration when the shape breaks. Unknown top-level
  sections warn but preserve.

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
  entry-point group. Projects reference by string tag in
  `task_defaults.adapter_type` (or per-task `adapter_type`).
- **New reset primitives.** Schema enum extension in tolokaforge
  itself. The safety contract stays engine-side.
- **New task-defaults fields.** Extend the `task_defaults` model
  alongside the underlying `TaskDescription` schema.
- **Deployment/profile layer above the project.** Slots in without
  changing the Project schema.
- **Workspace/organisation layer above projects.** For
  cross-project quotas, permissions, and cost accounting. Adds an
  entity above Project without changing the Project schema.

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
- **`_shared/domain.yaml`** continues to work exactly as today.
  The Project layer merges BEFORE domain.yaml is applied, so the
  merge order is: engine defaults → project.task_defaults →
  category domain.yaml → task.yaml.
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
- **Silent cross-trial contamination.** A `shared` service
  persists across trials of a task; a trial mutates state; the
  next trial sees dirty state. Prevention: default isolation for
  undeclared services is `ephemeral`; `shared` is opt-in.
- **Silent cross-task contamination.** Tasks A and B share a
  stack via inheritance; A's trials mutate a `shared` service;
  B's trials see the mutation. Prevention: same as above.
- **Non-canonical YAML causing hash misses.** Prevention: runtime
  canonicalises key order, quoting, and whitespace before hashing.
- **Cross-run assumption.** A user runs task A, then task B in a
  separate invocation, and expects B to see A's state.
  Prevention: the model is documented as within-run only.
- **Reset-primitive failure.** A `reset` service's primitive
  fails mid-run. Prevention: primitive failures terminate the
  affected task's remaining trials with an explicit reason
  (analogous to `TerminationReason.PROVISION_ERROR`).
- **Input-override typos.** A task overrides `postgress_version`
  (misspelt); the compose file's `${postgres_version}` binds to
  its default. Prevention: input names validated against the
  compose file's declared `${...}` references at load time.
- **Unknown section in `project.yaml`.** A pack declares a
  section an older loader doesn't recognise. Prevention: unknown
  sections warn but preserve; older loaders don't fail.
- **Adapter validation failure on merged TaskDescription.** The
  merged Task shape fails adapter validation. Prevention: the
  loader surfaces the specific merge step (project defaults,
  category domain, task.yaml) that contributed the offending
  field.

## What the model deliberately isn't

- **A free-form deep-merge system.** Every override is typed and
  bounded.
- **A four-level authoring hierarchy.** Project → Category → Task
  = three levels. Deeper breaks override semantics; the loader
  does not recurse.
- **A plugin registry for reset primitives.** New primitives
  extend the engine's schema enum. Third-party extensibility for
  primitives is deliberately deferred.
- **A cross-run stack persistence surface.** All sharing is
  within a single `tolokaforge run` invocation.
- **In-place editing of compose files.** Compose files are read
  at load time, input-substituted in memory, hashed, and
  materialised. On-disk files are never mutated.
- **A parallel wire format.** The Project layer merges INTO the
  existing `TaskDescription` from ADR-0003; no new wire type.

## Worked example

See
[`examples/native/example-microservices-pack/`](../../examples/native/example-microservices-pack/).
The pack ships:

- `project.yaml` at pack root — every section declared.
- `_shared/domain.yaml` — category-level tier demo (shared tools +
  system_prompt across a subset of tasks).
- `shared/environment.compose.yaml` — the base compose the project
  references.
- A minimal `run_config.yaml` — invocation-only fields.
- Nine tasks demonstrating: full inheritance, partial env override
  (input value), full env override (task-local compose), non-env
  override (max_turns), nested typed field override
  (user_simulator.persona), and category-tier inheritance via
  `_shared/domain.yaml`.

Read the pack's
[`README.md`](../../examples/native/example-microservices-pack/README.md)
for the task-by-task walkthrough and the per-scope resolved-config
table.

---

*Return to [`docs/architecture/`](.) for the ADR index and the
canonical runtime-backends documentation.*
