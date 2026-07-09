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

## The mental model — Project and Task, with group scoping

The authoring model has **two tiers**: Project and Task. A project
declares shared defaults; a task declares its identity and any
overrides. The task's effective config is `merge(project, task,
task-wins)`.

Sometimes a subset of tasks needs *different* shared defaults from
the rest — a specific tool set, a different persona, a different
system prompt. That's expressed as a **group** on top of the
Project's `task_defaults`, and tasks opt in by name or by file
reference. Groups are not a separate tier; they're a selector-based
scoping of shared defaults, sitting between the pack-wide
`task_defaults` and the task's own overrides.

Around the authoring model, two more scopes complete the picture:

| Scope | Owned by | Lifetime | What lives here |
|---|---|---|---|
| **CLI + env** | Operator | This invocation | `--runtime`, `--user-model`, `--judge-model`, `--presets-file`; env vars for API keys and service URLs |
| **Run** | `run_config.yaml` | This invocation | Which models drive this run, how many workers, output dir for this run, which packs to include |
| **Project** | `project.yaml` | Pack lifetime | Default environment, default models, compute/storage/observability/orchestration policies, task-level defaults inherited by every task |
| **Task** | `task.yaml` + task-adjacent files | One task | Task identity, per-task overrides |
| **Trial** | Runtime-only | One trial | Auto-generated ids, per-trial state — never user-configurable |

Inside the Project scope, `task_defaults` applies pack-wide;
optional `task_groups` (inline in `project.yaml`) or external
`_shared/*.yaml` files apply to a subset of tasks that opt in.
Group defaults layer between the pack-wide `task_defaults` and the
task's own overrides, but they're not their own scope — they're a
mechanism inside Project.

## Config file inventory

The complete set of files a task pack can ship:

| File | Location | Purpose | Required |
|---|---|---|---|
| `project.yaml` | pack root | Pack-level defaults + typed sections | Optional |
| `run_config.yaml` | pack root | Per-invocation config (models, orchestrator run knobs, evaluation choice) | Required for execution |
| `_shared/domain.yaml` | category dir | External form of a group's shared defaults (category, tools, user_simulator, system_prompt) referenced by task-level `domain:` | Optional |
| `_shared/system_prompt.md` | category dir | System prompt bundled with a group's external file | Optional |
| `_shared/mcp_server.py` | category dir | MCP server bundled with a group's external file | Optional |
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
# Applied to every task; task fields override; group defaults (if the
# task opts into a group) layer between these and the task.
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

# ── Group-scoped defaults (optional) ────────────────────────────
# Defaults applied only to tasks that opt in. Two equivalent forms:
#
# 1. Inline groups declared here, referenced from a task via
#    `group: <name>` in task.yaml.
# 2. External files (any path under the pack), referenced from a
#    task via `domain: <path>` in task.yaml. Toloka-internal packs
#    typically use `_shared/domain.yaml` for this — the external
#    form colocates the group's shared prompt and MCP server with
#    its config.
#
# Both forms merge the same way: group defaults layer between
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
| `task_defaults.system_prompt` | Runner system message | Yes — task's `system_prompt` overrides (a group's `system_prompt`, if the task opts in, layers between) |
| `task_defaults.user_simulator` | `UserSimulator` config | Yes — task's `user_simulator` deep-merges |
| `task_defaults.policies` | Loop policies | Yes — task's `policies` deep-merges |
| `task_defaults.tools` | Adapter tool wiring | Yes — task or opted-in group adds/replaces |
| `task_defaults.grading_defaults` | `TrialGrader` combine method / weights / pass threshold | Yes — task's `grading.yaml.combine` deep-merges |
| `task_groups.<name>` / external group file | Task-level defaults for tasks that opt in via `group:` or `domain:` | Yes — task fields override group defaults |
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
  text) replaces the project/group prompt entirely; no merge.

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

## Group-scoped defaults

A group is a named bundle of shared defaults (typically `tools`,
`user_simulator`, `system_prompt`, `category`) applied only to tasks
that opt in. Groups sit inside the Project scope; they're not their
own tier.

Groups can be expressed two ways, and both mean the same thing:

### External form (`_shared/*.yaml`)

The pattern Toloka-internal packs use today. A group's config lives
in a file under any convenient directory — conventionally
`_shared/domain.yaml`. Tasks opt in via a `domain:` field in
`task.yaml`:

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
the group's fields onto the task; task fields win on conflict.
Relative paths inside the group file (e.g. `./system_prompt.md`,
`./mcp_server.py`) are resolved from the group file's directory, so
a group can bundle its shared prompt and MCP server alongside
`domain.yaml` and every referring task picks them up correctly.

**Why external form is popular.** A group is often more than just
YAML fields — it includes a system prompt written in Markdown and,
for tool-heavy scenarios, an MCP server implementation in Python.
The external form colocates all three in one directory, so a whole
"group package" ships as a unit.

### Inline form (`task_groups` in `project.yaml`)

For groups that are pure YAML — no bundled `.md` or `.py` — a
group's defaults can live directly in `project.yaml` under
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

Merge semantics are identical to the external form: group defaults
layer between `task_defaults` and the task's own fields.

### Currently group-mergeable fields

- `category` (label)
- `tools`
- `user_simulator`
- `system_prompt`

Fields not in this list stay task-scoped even when a group file
declares them. Extending the set is a schema addition on the group
model.

## Resolution — task effective config

For every task discovered by `tasks.discovery`, the loader produces
a `TaskDescription` (the wire type from ADR-0003) by layered merge:

```
                project.task_defaults
                (applied to every task)
                        │
                        ▼
                Group defaults
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
2. **Group defaults** (if the task opts in via `group:` or
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
- **Groups as reusable bundles.** A UI can present `task_groups` as
  a library of shared-config templates that tasks pick from,
  independent of whether they live inline in `project.yaml` or in
  external files.
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
- **New group-mergeable fields.** Extend the group schema alongside
  the underlying `TaskDescription` fields.
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
  The external group form is exactly the pattern that already
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
- **Unknown group reference.** A task's `group:` or `domain:`
  refers to a group that doesn't exist. Loader fails at load time
  with a clear error.
- **Adapter validation failure on merged TaskDescription.** Loader
  surfaces the specific merge step (project defaults, group
  defaults, task.yaml) that contributed the offending field.

## What the model deliberately isn't

- **A separate "category" or "sub-project" tier.** Group-scoped
  defaults are a mechanism inside Project, not a peer tier.
  Authoring stays two-tier (Project → Task); groups scope which
  tasks pick up which defaults.
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
- `_shared/domain.yaml` + `_shared/system_prompt.md` — an external
  group demonstrating the co-located form.
- `shared/environment.compose.yaml` + `shared/system_prompt.md` —
  the base compose + project-level default prompt.
- A minimal `run_config.yaml` — invocation-only fields.
- Nine tasks demonstrating: full inheritance, partial env override,
  full env override, non-env override (`max_turns`), group opt-in
  via `domain:`, group + task-level nested override.

Read the pack's
[`README.md`](../../examples/native/example-microservices-pack/README.md)
for the task-by-task walkthrough and the per-scope resolved-config
table.

---

*Return to [`docs/architecture/`](.) for the ADR index and the
canonical runtime-backends documentation.*
