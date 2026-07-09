# Projects — the top-level abstraction

This document describes the **Project** as the top-level abstraction
in TolokaForge: what a project owns, how tasks inherit from it, how
per-invocation settings compose on top, and how every config file
that a project ships fits into the layered model.

Companion reading:
[`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md) (compute substrates,
provider registry, and how a project's environment identity gets
materialised — out of scope here),
[`adr/0003-trial-spec-and-trial-result.md`](adr/0003-trial-spec-and-trial-result.md)
(the wire format the loader synthesises into),
[`adr/0009-environment-manifest.md`](adr/0009-environment-manifest.md)
(the `EnvironmentManifest` schema that `default_environment`
extends),
[`adr/0018-multi-container-under-shared-runtime.md`](adr/0018-multi-container-under-shared-runtime.md)
(the isolation case matrix this model preserves).

## The three pieces of a project

A TolokaForge project ships three kinds of configuration, each
with a distinct job. All three follow the same **base + delta**
pattern: shared config lives in `project.yaml` under an
explicitly-labelled defaults block; per-item files declare only
what's different.

- **`project.yaml`** at the project root — holds identity, task
  discovery, the environment default, and two labelled base
  blocks:
  - **`task_defaults`** — the base every task inherits (adapter
    type, prompt frame, user-simulator persona, tools, timeouts,
    stuck-heuristics, grading combine method).
  - **`run_defaults`** — the base every run inherits (compute,
    storage, observability, orchestrator run knobs).
- **`tasks/<name>/task.yaml`** — one per task. Each task's
  identity and any deltas on top of `task_defaults`.
- **`run_configs/<name>.yaml`** — one file per invocation
  profile under `run_configs/`. Each declares only its deltas on
  top of `run_defaults` (typically the models it runs, its
  output directory, and any compute overrides). You pick which
  one by passing `--config <path>` on the CLI.

The Project defines what the project **is** and how it **defaults
to running**. Each task and each run config file declares only
what makes it **different**. The loader deep-merges base + delta
on both layers.

## What a Project is

A **Project** is a container for a set of related AI-agent
evaluations that share a common setup. If a team is running two
hundred tasks that all exercise the same customer-support workflow
against the same backend services, a Project is where they declare
"these tasks belong together, they run against this environment,
they share this system prompt frame and this simulated-user
persona, they grade on this rubric." Once the Project declares
that shared setup, individual tasks only need to describe what
makes them unique — the specific customer complaint, the expected
outcome, the grading criteria.

Without a Project, every task carries its own copy of every
setting, and every run config redeclares the same compute /
storage / observability boilerplate. The Project pulls the common
bits up to one place, so per-task and per-run files stay small.

Concretely, `project.yaml` holds:

- **Identity** — `name`, `version`, `description`.
- **Task discovery** — how tasks are found under `tasks/`.
- **`default_environment`** — the Docker Compose stack every task
  runs in, plus its isolation stance.
- **`task_defaults`** — the base every task inherits (adapter
  type, turn budget, system prompt, user-simulator persona,
  tools, grading combine method, timeouts, stuck-heuristics).
- **`run_defaults`** — the base every run inherits (compute
  provider, storage backends, observability exporters,
  orchestrator run knobs).

One file, one place per shared block. There is exactly one Project
per project. A minimal example — the full field list lives in
[The Project schema](#the-project-schema):

```yaml
# project.yaml at the project root
name: "customer-support-eval"
version: 1
description: "Customer-support scenario evaluation suite."

tasks:
  discovery:
    glob: "tasks/**/task.yaml"

default_environment:
  compose_file: "./shared/environment.compose.yaml"
  isolation: "per_trial"

task_defaults:
  adapter_type: "native"
  max_turns: 20
  system_prompt: "./shared/system_prompt.md"
  timeouts:
    trial_seconds: 600
    tool_call_seconds: 60

run_defaults:
  compute:
    provider: "local-docker"
    workers: 2
    max_budget_usd: 20.0
  storage:
    artifacts: { type: "local", path: "./results" }
    logs:     { type: "local", path: "./logs" }
    queue:    { backend: "sqlite" }
  observability:
    tracing: { exporter: "none" }
    metrics: { exporter: "none" }
    logging: { level: "INFO", exporter: "stdout" }
  orchestrator:
    repeats: 1
    max_turns: 30
```

### Project = scenario = domain, one-to-one

In practice, a project maps to one business or evaluation scenario
(customer-support triage, backend refactoring, deep-research
question-answering, ...). All the tasks in a project share the
same tools, the same system-prompt frame, the same simulated-user
persona, the same services. The Project layer formalises what has
always been implicit: **one project = one scenario = one domain**.

This mapping is settled team guidance, not an accidental
convention. Multi-domain projects and multi-project domains are
not supported: if you need two scenarios, ship two projects. If
the same domain has to be used in two projects, duplicate the
domain bundle into each — that trade-off is preferred over
cross-project coupling.

Cross-scenario runs are a **run-config** concern — list multiple
projects in `evaluation.projects` (which is optional and defaults
to the enclosing project when omitted). Each project's own
Project spec provides its scenario-specific defaults. The harness
composes the run from the projects, not by mixing scenarios inside
one Project.

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

Two example `task.yaml` shapes:

```yaml
# tasks/api_endpoint_add/task.yaml — inherits everything.
task_id: "api_endpoint_add"
description: >
  Add a GET /health/ready endpoint to the backend service that
  returns 200 when postgres is reachable and 503 otherwise.
```

```yaml
# tasks/long_debugging_session/task.yaml — overrides max_turns
# only; everything else still inherits from the Project.
task_id: "long_debugging_session"
description: "Diagnose intermittent 500s on GET /orders and fix."
max_turns: 60
```

There can be many tasks in a Project. There is always at least one.

## What a run config is

A **run config** (a YAML file under `run_configs/` at the project
root) declares how a specific run of the project differs from the
shared base in `project.run_defaults`. It's the delta layer for
execution-side settings.

The base — `project.run_defaults` — typically holds the fields
that stay the same across most invocations: compute provider,
storage backends, observability exporters, orchestrator run
knobs. Each `run_configs/<name>.yaml` declares only what's
different for that profile — usually `models` (the whole point
of running multiple configs is often to swap models),
`evaluation.output_dir`, and sometimes a `compute.workers` or
`compute.max_budget_usd` override.

Fields a run config can touch:

- **`models`** (`agent`, `user`, `judge`) — typically declared
  in full per run config since they vary; shared model settings
  can go in `run_defaults.models` if desired.
- **`compute`** — deep-merges with `run_defaults.compute`. Set
  only the fields that differ (e.g. `workers: 16` for nightly).
- **`storage`** — deep-merges with `run_defaults.storage`. Point
  at S3 for nightly, keep local defaults for dev.
- **`observability`** — deep-merges with
  `run_defaults.observability`.
- **`orchestrator`** — deep-merges with
  `run_defaults.orchestrator`. Typical delta: `repeats: 5` for
  a nightly sweep.
- **`evaluation`** — `projects` (optional; defaults to the
  enclosing project), `tasks_glob`, `output_dir`,
  `harness_adapter`. This section has no equivalent in
  `run_defaults`; it's per-invocation only.
- **`engine`** — `presets_file` and other invocation-time engine
  config.

A slim example — the same one that ships with the microservices
pack:

```yaml
# run_configs/dev.yaml — declares only the deltas
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

evaluation:
  output_dir: "results/dev-2026-07-09"
```

Everything else — compute, storage, observability, orchestrator —
comes from `project.run_defaults` unchanged.

### Running a Project under a specific run config

Every invocation names its run config via `--config`:

```bash
tolokaforge run --config run_configs/dev.yaml
tolokaforge run --config run_configs/ci.yaml
tolokaforge run --config run_configs/nightly.yaml
```

The loader deep-merges `project.run_defaults` under the named
file, applies env vars and CLI flags on top, and hands the result
to the engine.

A typical project layout:

```
project root/
├── project.yaml
├── run_configs/
│   ├── dev.yaml
│   ├── ci.yaml
│   ├── nightly.yaml
│   └── demo.yaml
├── shared/
└── tasks/
```

A project with only one execution profile still uses the
`run_configs/` directory — put the single file inside. There is no
root-level "default" run config file; every invocation names its
config explicitly.

Because run configs live as files, they sit in version control
next to the Project — a team can `git blame` who set the nightly
sweep to use a specific judge model, and rolling back a run-config
change is trivial. A future UI managing projects and their runs
can present each file in `run_configs/` as a named execution
profile the user picks from.

## How Project, Task, and run config compose

Two independent base + delta chains run at load time. Both use
the same deep-merge, later-wins mechanic; they just apply to
different fields.

**Task-level chain** — produces the effective `TaskDescription`
the runner sees for each task:

1. `task.yaml` fields (per-task deltas — highest priority)
2. `project.task_defaults` (base)
3. Adapter default (per adapter type)
4. Engine default

Task wins on conflict; unspecified sub-fields inherit from the
base. Deep-typed merge on typed sections (e.g.
`environment_manifest.inputs` merges input by input). Full
replacement on certain fields when the task points at a different
file (e.g. a task-local `compose_file` replaces the project's).

**Run-level chain** — produces the effective run configuration:

1. CLI flags (one-off overrides for this invocation)
2. Environment variables (infrastructure fields only — API keys,
   service URLs, executor address)
3. `run_configs/<name>.yaml` (per-invocation deltas)
4. `project.run_defaults` (base)
5. Engine default

Higher entries override lower. `run_defaults` supplies compute,
storage, observability, orchestrator; each `run_configs/*.yaml`
declares only what's different. Fields with no `run_defaults`
counterpart (`models`, `evaluation.output_dir`) come entirely
from the delta file.

**The two chains don't interact directly.** Each task's resolved
`TaskDescription` runs against the resolved run configuration at
execution time, but they merge on separate chains. A task's
`max_turns` override doesn't change the run's worker count; a run
config's `models.judge` doesn't change any task's tools.

Nothing in this model is required to be complex. A project with
ten similar tasks might have a `project.yaml` with populated
`task_defaults` and `run_defaults`, a slim `run_configs/dev.yaml`,
and ten one-line `task.yaml` files.

### The picture, in one diagram

```
project root/
├── project.yaml                    ← identity, discovery, and BOTH bases:
│                                     • task_defaults  ─ base for tasks
│                                     • run_defaults   ─ base for runs
│                                     • default_environment
│
├── run_configs/                    ← per-invocation deltas on run_defaults
│   ├── dev.yaml                    (typically declares models, output_dir,
│   ├── ci.yaml                      and any compute overrides)
│   └── ...
│
├── shared/                         (optional; assets project.yaml points at)
│   ├── environment.compose.yaml
│   └── system_prompt.md
│
└── tasks/                          ← per-task deltas on task_defaults
    ├── task_a/
    │   └── task.yaml               (declares only identity + overrides)
    └── task_b/
        └── task.yaml
```

Task chain: `merge(project.task_defaults, task.yaml)` →
`TaskDescription`. Run chain:
`merge(project.run_defaults, run_configs/<name>.yaml, env, CLI)`.
Both feed the runner at execution time.

### Familiar shapes from other tools

If you've used Databricks Workflows, MLflow Projects, dbt projects,
or Prefect Deployments, this pattern will feel familiar. Each of
those tools has a "project-like" top-level entity that owns the
shared setup for a collection of items (jobs, entrypoints, models,
flows), with per-item overrides layered on top. TolokaForge's
Project is that top-level entity for AI-agent evaluations; a Task
is the per-item.

## The five config scopes — reference table

Pulling all of the above together, TolokaForge has five layers of
configuration that combine to determine what actually runs:

| Scope | Owned by | Lifetime | What lives here |
|---|---|---|---|
| **CLI + env** | Operator | This invocation | `--runtime`, `--user-model`, `--judge-model`, `--presets-file`; env vars for API keys and service URLs |
| **Run** | `run_configs/*.yaml` | This invocation | Deltas on `project.run_defaults` — typically `models`, `evaluation.output_dir`, per-run compute overrides |
| **Project** | `project.yaml` | Project lifetime | Identity, task discovery, `default_environment`, `task_defaults` (base for tasks), `run_defaults` (base for runs) |
| **Task** | `task.yaml` + task-adjacent files | One task | Task identity, per-task deltas on `task_defaults` |
| **Trial** | Runtime-only | One trial | Auto-generated ids, per-trial state — never user-configurable |

Project and Task are the *authoring* layers. Run and CLI+env are
the *execution* layers. Trial is runtime-produced and not
user-configurable.

## Config file inventory

The complete set of files a project can ship:

| File | Location | Purpose | Required |
|---|---|---|---|
| `project.yaml` | project root | Identity, discovery, `default_environment`, `task_defaults`, `run_defaults` | Required |
| `run_configs/*.yaml` | project root's `run_configs/` directory | Per-invocation deltas on `run_defaults` (models, output_dir, overrides) | At least one file required for execution |
| `shared/environment.compose.yaml` | project root or task dir | Base compose file referenced by `default_environment` | Optional |
| `shared/system_prompt.md` | project root | Default system prompt referenced by `task_defaults.system_prompt` | Optional |
| `task.yaml` | task dir | Task spec (identity, adapter, max_turns, initial_user_message, initial_state, tools, user_simulator, metadata, policies, grading path, system_prompt, adapter_settings, environment_manifest) | Required |
| `grading.yaml` | task dir | Grading rules (combine, state_checks, transcript_rules, llm_judge, custom_checks) | Required |
| `environment.compose.yaml` | task dir | Task-local compose (used when the task overrides the project default) | Optional |
| `initial_state.json` | task dir | DB tables + records + fs state seeded at trial start | Optional |
| `fixtures/` | task dir | Test data referenced by tools/services/initial_state | Optional |

**Adapter-specific files.** Adapters that group their tasks under a
Domain typically ship their own conventions for further sharing —
a Domain bundle directory next to the tasks, referenced from
`adapter_settings`. Those files are adapter-specific and documented
with the adapter they belong to; they are not part of the Project
schema. The harness passes them through as opaque
`adapter_settings` data.

The Project layer adds one file (`project.yaml`) and touches no
existing schemas. Every other file keeps the shape it has today.

## The Project schema

`project.yaml` at project root:

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
  isolation: "shared_ok"          # or "per_trial"; shared_ok is the default
  network_policy: "LOCALHOST_ONLY"
  security_context_defaults:
    user: "toloka"
    group: "toloka"

# ── Task defaults — base for every task ─────────────────────────
# Applied to every task; task.yaml deltas deep-merge on top.
# Includes task-shape properties (timeouts, stuck_heuristics,
# continue_prompt) that describe how the pack's tasks are
# shaped, not how a particular invocation executes.
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
  timeouts:
    trial_seconds: 600
    tool_call_seconds: 60
  stuck_heuristics:
    enabled: true
    max_repeated_tool_calls: 5
    max_idle_turns: 3
  continue_prompt: "Continue."

# ── Run defaults — base for every run config ────────────────────
# Applied to every run under run_configs/; a run_configs/<name>.yaml
# file's fields deep-merge on top. Holds the execution-side blocks
# that stay the same across most invocations. `models` typically
# varies per invocation and lives in run_configs/*.yaml, but
# shared model settings (e.g. a uniform temperature) can be
# placed here.
run_defaults:
  compute:
    provider: "local-docker"
    workers: 2
    max_budget_usd: 20.0
    max_requests_per_second: 10.0
    max_attempt_retries: 3
  storage:
    artifacts: { type: "local", path: "./results" }
    logs:     { type: "local", path: "./logs" }
    queue:    { backend: "sqlite" }
  observability:
    tracing: { exporter: "none" }
    metrics: { exporter: "none" }
    logging: { level: "INFO", exporter: "stdout" }
  orchestrator:
    repeats: 1
    max_turns: 30
    auto_start_services: true
    shuffle_trials: false
```

Adding a new top-level section is a schema addition on the Project
model. Every top-level key must match the declared schema; unknown
sections, unknown fields inside a section, and typos fail loud at
config load. There is no silent-preserve fallback — if the loader
doesn't recognise a key, it names the file, the offending key, and
the closest schema match and refuses to start.

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
sharing task-level fields across every task in a project:

- `adapter_type` — same for every task in the project (typical
  pattern: every task uses the same adapter).
- `system_prompt` — the shared system-prompt frame; each task can
  add specifics.
- `tools` — the tool set exposed to the agent.
- `user_simulator` — mode, persona, backstory (each task can
  override the persona or scripted flow).
- `max_turns`, `policies`, `grading_defaults`, `adapter_settings`
  — shared knobs.

This covers the great majority of cross-task sharing needs. A project
with three hundred customer-support tasks declares the shared
prompt, tools, and persona once in `task_defaults`; individual
tasks contribute only their identity, their specific initial state,
and their specific grading rubric.

**The harness core is Domain-agnostic.** Domain is not a concept
the Project schema or the harness runtime standardizes. Adapters
that group their tasks under a Domain typically ship a Domain
bundle (a directory next to the tasks with shared tools, prompt,
KB documents, and any adapter-specific code), and each task points
at that bundle through the opaque `adapter_settings` field on
`task_defaults` or `task.yaml`. The harness passes those settings
through to the adapter and doesn't interpret them.

Different adapters use different Domain conventions. The Project
schema treats every one of them as adapter-private data — same as
any other adapter-specific config. This keeps Domain semantics
where they belong (in the adapter that understands them) and keeps
the harness core small.

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

If an adapter ships its own Domain-shaped bundle merge, that merge
happens between `project.task_defaults` and the task's own fields,
driven by the adapter, and documented with the adapter. From the
Project's perspective it's a black-box adapter-side operation.

## Resolution — run effective config

For the invocation itself, the loader deep-merges the run-scoped
chain and then applies CLI + env overrides:

```
    Engine defaults
        │
        ▼
    project.run_defaults
    (base — compute, storage, observability, orchestrator)
        │
        ▼
    run_configs/<name>.yaml
    (delta — models, evaluation, per-invocation overrides, engine)
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

## Field ownership

Every field is either task-scoped or run-scoped. The two scopes
compose on independent chains — a task delta never touches a run
field and vice versa.

| Section | Base (project.yaml) | Delta |
|---|---|---|
| Identity (`name`, `version`, `description`) | project.yaml | — |
| `tasks.discovery` | project.yaml | — |
| `default_environment` | project.yaml | task.yaml's `environment_manifest` |
| `task_defaults.*` (adapter, prompt, tools, user_simulator, policies, grading_defaults, timeouts, stuck_heuristics, continue_prompt) | `project.task_defaults` | `task.yaml` |
| `compute` (provider, workers, budget, rate limits, retries) | `project.run_defaults.compute` | `run_configs/<name>.yaml` |
| `storage` (artifacts, logs, queue) | `project.run_defaults.storage` | `run_configs/<name>.yaml` |
| `observability` (tracing, metrics, logging) | `project.run_defaults.observability` | `run_configs/<name>.yaml` |
| `orchestrator` (repeats, max_turns, queue_backend, auto_start_services, shuffle_trials, schedule) | `project.run_defaults.orchestrator` | `run_configs/<name>.yaml` |
| `models` (`agent`, `user`, `judge`) | `project.run_defaults.models` (optional) | `run_configs/<name>.yaml` |
| `evaluation` (projects, tasks_glob, output_dir, harness_adapter) | — | `run_configs/<name>.yaml` |
| `engine` (presets_file) | — | `run_configs/<name>.yaml` |

### Precedence — task-scoped fields

Highest priority to lowest:

1. **`task.yaml`** value (delta).
2. **`project.task_defaults`** value (base).
3. **Adapter default** (per adapter type; includes any
   adapter-specific Domain-bundle merges).
4. **Engine default**.

### Precedence — run-scoped fields

Highest priority to lowest:

1. **CLI flag** (e.g. `--runtime`, `--workers`, `--user-model`).
2. **Environment variable** — infrastructure fields only
   (`DB_SERVICE_URL`, `RAG_SERVICE_URL`, `EXECUTOR_ADDRESS`,
   `TASK_PACKS_DIRS`, provider API keys).
3. **`run_configs/<name>.yaml`** value (delta).
4. **`project.run_defaults`** value (base).
5. **Engine default**.

The two chains never overlap. `project.task_defaults` doesn't
participate in the run-scoped chain; `project.run_defaults`
doesn't participate in the task-scoped chain.

### Why the split

The same Project spec runs under many different invocations
without editing `project.yaml`. `project.run_defaults` holds the
compute / storage / observability / orchestrator settings the
team uses by default; each `run_configs/<name>.yaml` declares
only what makes that profile different — different models for a
bake-off, more workers for nightly, S3 storage for archived
runs.

## Worked scenarios

Three concrete scenarios that show how base + delta plays out.
Each scenario trims the files to the fields that matter for the
point being made (`# ...` markers where irrelevant sections are
elided).

### Scenario A — Single-machine dev workflow

A team is iterating on a support-triage evaluation project on
their laptops.

Layout:

```
support-triage-pack/
├── project.yaml
├── run_configs/
│   └── dev.yaml
├── shared/
│   ├── environment.compose.yaml
│   └── system_prompt.md
└── tasks/
    ├── login_reset/
    ├── password_recovery/
    └── ...
```

`project.yaml` — identity + `task_defaults` + `run_defaults`:

```yaml
name: "support-triage-eval"
version: 1
description: "Customer-support triage evaluation suite."

tasks:
  discovery:
    glob: "tasks/**/task.yaml"

default_environment:
  compose_file: "./shared/environment.compose.yaml"
  runner_service: "runner"
  isolation: "per_trial"

task_defaults:
  adapter_type: "native"
  max_turns: 20
  system_prompt: "./shared/system_prompt.md"
  user_simulator:
    mode: "llm"
    persona: "frustrated support customer"
  timeouts:
    trial_seconds: 600
    tool_call_seconds: 60

run_defaults:
  compute:
    provider: "local-docker"
    workers: 2
    max_budget_usd: 20.0
  storage:
    artifacts: { type: "local", path: "./results" }
    logs:     { type: "local", path: "./logs" }
    queue:    { backend: "sqlite" }
  observability:
    tracing: { exporter: "none" }
    metrics: { exporter: "none" }
    logging: { level: "INFO", exporter: "stdout" }
  orchestrator:
    repeats: 1
    max_turns: 30
```

`run_configs/dev.yaml` — only the deltas:

```yaml
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

evaluation:
  output_dir: "results/dev-2026-07-09"
```

Invocation: `tolokaforge run --config run_configs/dev.yaml`.

**What base + delta looks like here.** `project.run_defaults` holds
compute / storage / observability / orchestrator — the boilerplate
that stays the same across every invocation on this project.
`run_configs/dev.yaml` declares only `models` (the thing being
evaluated) and `evaluation.output_dir` (where results land).

### Scenario B — Same project under CI and nightly sweeps

The project from Scenario A now ships with two additional run
configs for automated pipelines. `project.yaml` doesn't change;
the two new files declare only the fields that differ from
`run_defaults`.

Layout:

```
support-triage-pack/
├── project.yaml                    ← unchanged from Scenario A
├── run_configs/
│   ├── dev.yaml                    (unchanged from Scenario A)
│   ├── ci.yaml
│   └── nightly.yaml
├── shared/
└── tasks/
```

`run_configs/ci.yaml`:

```yaml
models:
  agent:
    provider: "openrouter"
    name: "anthropic/claude-haiku-4-5"      # cheaper model for CI
  user:
    provider: "openrouter"
    name: "anthropic/claude-haiku-4-5"
    temperature: 0.2
  judge:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.0

compute:
  max_budget_usd: 5.0                       # tighten CI budget

evaluation:
  output_dir: "results/ci/${CI_RUN_ID}"
```

`run_configs/nightly.yaml`:

```yaml
models:
  agent:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
  user:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.2
  judge:
    provider: "openrouter"
    name: "anthropic/claude-opus-4-8"       # stronger judge for reliability
    temperature: 0.0

compute:
  workers: 16                               # scale up
  max_budget_usd: 200.0                     # relaxed budget

storage:
  artifacts: { type: "s3", bucket: "team-toloka", prefix: "nightly" }
  logs:     { type: "s3", bucket: "team-toloka", prefix: "nightly-logs" }
  queue:    { backend: "postgres" }

observability:
  tracing: { exporter: "otlp", endpoint: "http://collector:4317" }
  metrics: { exporter: "prometheus", endpoint: "http://prom:9090" }
  logging: { exporter: "otlp" }

orchestrator:
  repeats: 5                                # higher sampling

evaluation:
  output_dir: "s3://team-toloka/nightly/${DATE}"
```

Invocation:

```bash
tolokaforge run --config run_configs/ci.yaml
tolokaforge run --config run_configs/nightly.yaml
```

**What base + delta looks like here.** `ci.yaml` is ~10 lines —
just the CI-specific model choice, a tighter budget, and its
output path. `nightly.yaml` is longer because nightly differs
more (S3 storage, OTLP tracing, more workers, more repeats), but
even it only declares the fields that actually differ; the
shared local-docker / per_trial / auto_start_services boilerplate
from `run_defaults` doesn't appear in either file.

### Scenario C — Cross-model bake-off

A researcher wants to compare three agent models against the same
evaluation project. `project.run_defaults` holds everything
constant so results are comparable; each run config file declares
only its agent model and output directory.

Layout:

```
support-triage-pack/
├── project.yaml                    ← unchanged
├── run_configs/
│   ├── agent-opus.yaml
│   ├── agent-sonnet.yaml
│   └── agent-gpt5.yaml
├── shared/
└── tasks/
```

`run_configs/agent-opus.yaml`:

```yaml
models:
  agent:
    provider: "openrouter"
    name: "anthropic/claude-opus-4-8"
    temperature: 0.0
  user:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.2
  judge:
    provider: "openrouter"
    name: "anthropic/claude-opus-4-8"
    temperature: 0.0

compute:
  workers: 8
  max_budget_usd: 50.0

orchestrator:
  repeats: 5

evaluation:
  output_dir: "results/bake-off/opus"
```

`run_configs/agent-sonnet.yaml` and `run_configs/agent-gpt5.yaml`
are identical except for `models.agent.name` and `output_dir`.

**What base + delta looks like here.** Comparing three run
configs is a diff on `models.agent.name` and `output_dir`
alone — everything else (environment, task defaults, compute
substrate, isolation) is held constant by the Project, which is
what makes the three runs' scores comparable. If someone changes
a knob in `run_defaults`, all three bake-off runs pick it up
uniformly without editing three files.

## Isolation — how much a run shares across trials

A run consists of many trials against a project's tasks. The
question this section answers is: **how much state carries from
one trial to the next?**

Isolation is expressed through two knobs:

- **`default_environment.isolation`** (on the Project, or
  overridable per task) — the between-trial default for services
  that don't declare their own label. `shared_ok` (default)
  means unlabelled services persist across trials; `per_trial`
  means unlabelled services are torn down and recreated between
  trials.
- **Per-service `tolokaforge.isolation` label** (on services in
  the compose file) — the authoritative declaration for that
  service: `shared`, `reset` (with a named primitive), or
  `ephemeral`. Wins over the task-level default for that
  service.

The per-service label is always authoritative.
`default_environment.isolation` only supplies the default for
services that omit a label. The more granular declaration wins —
consistent with every other override in the schema.

At a glance:

| Stance | Cost between trials | Safety guarantee | Setup complexity |
|---|---|---|---|
| Completely shared | Lowest — services persist | Weakest — all state carries over | Simple (defaults) |
| Declared mixed | Middle — reset primitives typically ~200 ms | Per-service explicit; strong where declared | Per-service labels + primitives |
| Total isolation | Highest — unlabelled services rebuilt per trial | Strong by default; relaxations require an explicit label | Task declares `isolation: per_trial` |

### Stance 1 — Completely shared (default)

Every service persists across trials. State carries over between
trials by default. This is what happens when a project doesn't
declare an isolation requirement (or declares `shared_ok`) and
no service has a label narrower than `shared`.

```yaml
# project.yaml — no isolation declared → defaults to shared_ok
default_environment:
  compose_file: "./shared/environment.compose.yaml"
```

```yaml
# shared/environment.compose.yaml — unlabelled services inherit
# the shared_ok default
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
  backend-api:
    image: "myrepo/example-backend:v1.4.0"
  # ...
```

**Cost.** Lowest — no per-trial teardown work.

**Safety.** Weakest. If any trial mutates a service's state, all
subsequent trials see the dirty state. Silent cross-trial
contamination is the failure mode.

**Pick this when:** services are stateless or genuinely
read-only (static-content HTTP servers, immutable reference DBs,
catalog services); tasks only *read* from the services.

**Never pick this when:** any service is mutated by any trial
and you can't afford the mutation to affect later trials. Prefer
Stance 2 instead.

### Stance 2 — Declared mixed (per-service)

Per-service labels decide what happens between trials. Some
services stay as-is, some get reset via a named primitive, some
are torn down and recreated. Any service that omits a label
inherits the task's `default_environment.isolation`.

```yaml
# project.yaml — shared_ok default; unlabelled services persist
default_environment:
  compose_file: "./shared/environment.compose.yaml"
```

```yaml
# shared/environment.compose.yaml — per-service isolation mix
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
    labels:
      tolokaforge.isolation: "reset"
      tolokaforge.reset_primitive: "postgres_template_db"

  backend-api:
    image: "myrepo/example-backend:v1.4.0"
    labels:
      tolokaforge.isolation: "shared"

  worker:
    image: "myrepo/example-worker:v1.4.0"
    labels:
      tolokaforge.isolation: "ephemeral"
```

**Cost.** Middle. Reset primitives are cheap compared to full
teardown (~200 ms per trial for a `postgres_template_db`); `shared`
services pay nothing between trials.

**Safety.** Per-service explicit. `reset` services return to a
known-clean state via the primitive; `shared` services keep
their state (pack author asserts this is safe); `ephemeral`
services are fully rebuilt.

**Pick this when:** the pack has a mix — some services are safe
to share, some need clean state per trial via cheap resets, some
must be rebuilt each time. Most realistic multi-container
workloads land here.

### Stance 3 — Total isolation

Task declares `isolation: per_trial`. Every unlabelled service
resolves to `ephemeral` — torn down and recreated between
trials. A specific service can still opt out with an explicit
`shared` or `reset` label if the pack author has a reason.

```yaml
# project.yaml
default_environment:
  compose_file: "./shared/environment.compose.yaml"
  isolation: "per_trial"          # unlabelled services default to ephemeral
```

```yaml
# shared/environment.compose.yaml — under isolation: per_trial,
# unlabelled services default to ephemeral; a labelled service
# can still opt out.
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
    # unlabelled → ephemeral (from per_trial default)

  immutable-catalog:
    image: "myrepo/catalog:v1.4.0"
    labels:
      tolokaforge.isolation: "shared"
      # explicit exception: this catalog is safe across trials
```

**Cost.** Highest. Every unlabelled service is torn down and
recreated per trial.

**Safety.** Strong by default. Unlabelled services can't
accidentally carry state. Any relaxation is an explicit,
reviewable label on the specific service that needs it.

**Pick this when:** trials mutate state destructively; safety is
paramount; you don't trust unlabelled services to be safe to
share.

### Task overrides

A task's own `environment_manifest.isolation` wins over the
project's `default_environment.isolation`; a task can also ship
its own compose file with different per-service labels. Both
changes make the task's resolved environment hash differently
(see below), so the task resolves to its own environment
identity.

## Environment identity

Every task's resolved environment — its compose file, bound
input values, and per-service isolation map — collapses to a
single canonical identity. Two tasks whose resolved environments
are identical share one environment identity; anything that
differs (a different input, an overridden compose file, a
different per-service label) gives the task its own identity.

```
                    project.yaml declares default_environment
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
       task_a (inherits)     task_b (inherits)     task_c (partial override)
              │                     │                     │
        identity=X            identity=X            identity=Y
              │                     │                     │
              └── same env ─────────┘                     │
                                                          │
                                              (different env)
```

The schema declares intent (what the environment looks like);
identity resolution makes that intent operative. How the runtime
uses these identities to materialise, share, or tear down actual
stacks is deliberately out of scope for this document.

## Per-service isolation vocabulary

Reference table for the per-service labels used in the isolation
stances above (see
[Isolation — how much a run shares across trials](#isolation--how-much-a-run-shares-across-trials)
for how to pick a stance).

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

When a service does not declare a label, its mode inherits the
task's `default_environment.isolation`: `shared_ok` (the project
default) resolves unlabelled services to `shared`; `per_trial`
resolves them to `ephemeral`. Per-service labels always win over
this default.

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

**Interaction with environment identity.** The resolved
per-service isolation map is part of each task's environment
identity. Two tasks that agree on the compose file but resolve to
different labels for a service resolve to different environment
identities.

## Adopting the Project layer

This document is the design; the engine work to implement it lands
in follow-up ADRs and PRs. The harness surfaces that need
refactoring include:

- **Task discovery and the loader** — currently walks
  `evaluation.tasks_glob` from the run config; needs to read
  `project.yaml` at project root and apply `task_defaults` on
  task load. Every pack must ship a `project.yaml`; there is no
  synthesised default.
- **Adapter interfaces** — adapters receive a fully-resolved
  `TaskDescription`; the loader merges `task_defaults` into task
  fields before adapter validation, without changing
  `TaskDescription` shape.
- **Run-config composition** — the loader grows a deep-merge pass
  that layers `run_configs/<name>.yaml` on top of
  `project.run_defaults` at load time.
- **CLI and engine defaults** — engine-level defaults for
  `compute` / `storage` / `observability` / `orchestrator` move
  from hard-coded to `run_defaults`-overridable, with
  `run_configs/*.yaml` values and CLI flags able to override on
  top.
- **Directory rename** — the runtime accepts `run_configs/`
  (plural) as the canonical location, with `run_config/`
  (singular) as a deprecated alias.
- **Legacy field redesign** — `evaluation.task_packs` on the run
  config becomes `evaluation.projects`, and the field turns
  **optional**: when omitted, the loader defaults to the
  enclosing project. `task_packs` stays accepted as a deprecated
  alias with a `DeprecationWarning` at config load. See the
  "Deprecations and migrations" section below for the migration
  notes.

Existing `adapter_settings.*` fields — including any Domain-bundle
references — flow through unchanged. The schema fails loud on
unknown keys, so a `project.yaml` migrating from an older shape
will surface every renamed or removed field at load time rather
than silently taking a default.

## Deprecations and migrations

Three changes carry a deprecation window. All remain accepted for
now; all should be migrated away from.

### 1. `evaluation.task_packs` on the run config

`evaluation.task_packs: [<path>, ...]` becomes
`evaluation.projects: [<path>, ...]` — same shape, now
**optional**. When omitted, the loader uses the enclosing project
(the project directory the run config file lives in). Explicit
only for the rare multi-project run.

To migrate: if your run config lives under
`<project>/run_configs/`, delete the `evaluation.task_packs` line
entirely. Otherwise rename `task_packs:` to `projects:` and keep
the list. `task_packs:` remains accepted as an alias with a
`DeprecationWarning` for one full minor-version release cycle
after the rename lands.

### 2. `run_config/` directory (singular)

Earlier iterations put run configs under `run_config/` at the
project root. The canonical directory is `run_configs/` (plural
— matches `tasks/`). `run_config/` remains accepted as a
deprecated alias for the same release window as (1).

To migrate: rename the directory. Nothing inside changes.

### 3. `docs/TASK_PACKS.md`

The pre-Project authoring guide is superseded by this document,
which covers the full model — project + run configs + tasks with
the concrete schema and worked scenarios. `TASK_PACKS.md` carries
a deprecation banner and will be removed once no in-tree link
still targets it. Update any external links you own.

## Schema enforcement

The Project schema is strict at both files.

- **`project.yaml` is required.** Packs without one fail at load
  with the missing-file path named.
- **Unknown top-level keys fail** in `project.yaml`, `task.yaml`,
  and every `run_configs/<name>.yaml`. Error names the file, the
  offending key, and the closest schema match.
- **Unknown sub-fields inside a declared section fail** the same
  way. Typos never silently take a default.
- **Required fields fail loud if missing.** No implicit defaults
  for structure.
- **`run_defaults` is optional as a whole block** — a project
  that omits it declares no shared run base and every run config
  stands alone. But if `run_defaults` is declared, every key
  inside it is validated.
- **Deprecation aliases are the sole exception** and are enumerated
  in the section above (`evaluation.task_packs`, `run_config/`
  directory). Aliases resolve to their canonical form at load and
  emit a `DeprecationWarning`; every other legacy name fails.
- **Adapter-specific `adapter_settings.*`** are validated by the
  adapter, not by the Project loader — the Project loader passes
  them through as opaque data. Every adapter must strict-validate
  its own settings; consult the adapter's own docs.

## Failure modes

- **`llm_judge` without a judge model.** A task's `grading.yaml`
  uses `llm_judge`, but the run config doesn't declare
  `models.judge`. Prevention: orchestrator refuses to start;
  error names the offending task(s) and the missing field.
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
- **Unknown section, unknown field, or typo in `project.yaml`,
  `task.yaml`, or a run config.** Fails at load; error names the
  file and the offending key.
- **Adapter validation failure on merged TaskDescription.** Loader
  surfaces the specific merge step (project defaults, task.yaml)
  that contributed the offending field.

## What the model deliberately isn't

- **A harness-level Domain abstraction.** Domain is an adapter-side
  concept and stays there. The harness core (loader, discovery,
  `RuntimeBackend`, `TrialGrader`, `TaskDescription` schema) is
  Domain-agnostic — it has no `project.domain` field, no Domain
  section in the schema, no unified Domain vocabulary at the
  harness level. Some adapters implement Domain-shaped bundling as
  their own convention, carried through the opaque
  `adapter_settings` field on the task; the Project schema and the
  harness core treat those bundles as adapter-private data.
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
The project ships:

- `project.yaml` at project root — identity + `default_environment`
  + `task_defaults` + `run_defaults`.
- `shared/environment.compose.yaml` + `shared/system_prompt.md` —
  the base compose + project-level default prompt.
- A slim `run_configs/dev.yaml` — just the models and output
  directory; everything else comes from `run_defaults`.
- Seven tasks demonstrating: full inheritance, partial env override
  (input value), full env override (task-local compose), and
  non-env override (`max_turns`).

Read the project's
[`README.md`](../../examples/native/example-microservices-pack/README.md)
for the task-by-task walkthrough and the per-scope resolved-config
table.

---

*Return to [`docs/architecture/`](.) for the ADR index and the
canonical runtime-backends documentation.*
