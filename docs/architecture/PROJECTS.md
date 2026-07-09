# Projects — the top-level abstraction

This document describes the **Project** as the top-level abstraction
in TolokaForge: what a project owns, how tasks inherit from it, how
per-invocation settings compose on top, and how every config file
that a project ships fits into the layered model.

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

## Vocabulary

**The abstraction is called a Project.** New code, new docs, and
this document use that name consistently.

Two places in the current codebase still carry the legacy name
"task pack." Both are being migrated away from:

- **`docs/TASK_PACKS.md`** — the older authoring guide. Superseded
  by this document; a deprecation banner at the top of that file
  points readers here.
- **`evaluation.task_packs`** — a schema field on the run config
  that lists project directories a run pulls tasks from. Being
  redesigned: the new field is `evaluation.projects` and it's
  optional — a run config that omits it defaults to the enclosing
  project (i.e. the project directory the run config lives in).
  `task_packs` is preserved as a deprecated alias with a
  `DeprecationWarning` at config load. See "Deprecations and
  migrations" below.

Outside those two, "task pack" is no longer used. Wherever the
word "pack" still appears in the codebase, it refers to a Project.

For clarity: this doc uses **Project** for the abstraction,
**project directory** for its filesystem layout, and the words
*scenario* and *domain* only when the point is specifically to
name the semantic equivalences settled by team review (see
"Project = scenario = domain, one-to-one" below).

## The three pieces of a project

A TolokaForge project ships three kinds of configuration, each
with a distinct job:

- **`project.yaml`** at the project root — declares the project's
  identity and everything that stays the same across every run of
  it: the default environment, compute policy,
  storage backends, observability sinks, orchestration policy, and
  the task-level defaults every task inherits.
- **`run_config/*.yaml`** — one or more named run configs under a
  `run_config/` directory at the project root. Each file declares
  per-invocation choices: which models drive *this* run, how many
  workers to spawn, where to write output, which projects to
  include. The same Project can run under many different run
  configs (CI, nightly, demo) — you pick which one by passing
  `--config <path>` at invocation time.
- **`task.yaml`** files under `tasks/<name>/` — one per task. Each
  task's identity and any settings that override the Project's
  defaults for that specific task.

The Project defines what the project **is**. The run config picks
**how this run is configured**. Each task defines what makes it
**unique**. The rest of the project (grading rules, fixtures,
environment compose files, shared assets) lives at whichever level
owns it.

Everything below drills into each of these three pieces and how
they compose.

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
- **The compute policy** — where trials run, how much parallelism,
  budget caps, timeouts, stuck-heuristics.
- **The storage backends** for artifacts, logs, and the trial queue.
- **The observability sinks** — where traces, metrics, and logs go.
- **The orchestration policy** — retries, priority, scheduling.
- **Task-level defaults** every task inherits — adapter type, turn
  budget, system prompt, user-simulator persona, tools, grading
  combine method.

One file, `project.yaml` at the project root, holds all of this. There
is exactly one Project per project.

A minimal example — the full field list lives further down in
[The Project schema](#the-project-schema):

```yaml
# project.yaml at the project root
name: "customer-support-eval"
version: 1
description: "Customer-support scenario evaluation suite."

default_environment:
  compose_file: "./shared/environment.compose.yaml"

task_defaults:
  adapter_type: "native"
  max_turns: 20
  system_prompt: "./shared/system_prompt.md"

compute:
  provider: "local-docker"
  workers: 2

# Also declared here: storage, observability, orchestration —
# see the full schema below. Note: `models` lives on the run
# config, not the project — see "What a run config is" below.
```

### Project = scenario = domain, one-to-one

In practice, a project maps to one business or evaluation scenario
(customer-support triage, backend refactoring, deep-research
question-answering, ...). All the tasks in a project share the
same tools, the same system-prompt frame, the same simulated-user
persona, the same services. The Project layer formalises what has
always been implicit: **one project = one scenario = one domain**.
(The historical vocabulary "task pack" refers to the same entity
— see the Vocabulary section above.)

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

A **run config** (a YAML file under `run_config/` at the project
root) declares how a specific run of the project is configured —
the settings that vary between one invocation and the next while
the Project itself stays the same.

Concretely, a run config owns:

- **Which models** drive this particular run — the agent, the
  simulated user, the judge.
- **Which projects** to include in this invocation (a single run
  can pull tasks from more than one project).
- **Run-wide caps** — number of workers, per-trial repeat count,
  per-trial max-turns ceiling, budget cap, output directory for
  this run's results.
- **A task filter** — an optional glob that narrows down which
  tasks under the project this invocation actually runs.

The same Project can run under many different run configs:

- A CI pipeline runs a fast, cheap sweep with a small model and
  low repeat count.
- A nightly regression sweep uses a stronger model, higher
  repeats, and writes to a durable output location.
- A stakeholder demo pins one specific model version to keep
  results reproducible.

All three read the same Project. The run config picks the
per-invocation choices; the Project provides everything else.

**Fields that appear in both files.** Some settings — `models`,
`workers`, `output_dir`, runtime mode — have sensible defaults in
`project.yaml` and can be overridden per invocation in a run
config. When both files declare the same field, the run config
wins for that invocation.

**A run config is required for execution** — you need one to
actually invoke `tolokaforge run` — but a minimal run config can
be a few lines if the Project already declares the defaults it
needs.

A minimal example:

```yaml
# run_config/dev.yaml (inside a project directory)
orchestrator:
  repeats: 3                          # each task runs 3 times
  max_turns: 30                       # run-wide ceiling

evaluation:
  # `projects:` omitted — the run operates on the enclosing
  # project by default. Uncomment to run against a different
  # project or to combine several.
  # projects:
  #   - "."                             # this project
  #   - "../other-project"              # additional project
  # tasks_glob: "tasks/support_*/task.yaml"   # optional filter
  # output_dir: "results/nightly-2026-07-09"  # overrides project default

# Models — always declared on the run config, never on the
# project. The model being evaluated is the invocation's variable;
# everything else is Project setup.
models:
  agent:
    provider: "openrouter"
    name: "anthropic/claude-opus-4-8"
  user:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.2
  judge:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
```

### Running a Project under a specific run config

Run configs live as named files under `run_config/` at the project
root. Every invocation names one via the CLI's `--config` flag:

```bash
tolokaforge run --config run_config/dev.yaml
tolokaforge run --config run_config/ci.yaml
tolokaforge run --config run_config/nightly.yaml
tolokaforge run --config run_config/demo.yaml
```

Each file is a complete, self-contained run config. Run configs do
not inherit from each other; if two run configs need shared
boilerplate, duplicate it.

A typical project layout:

```
project root/
├── project.yaml
├── run_config/
│   ├── dev.yaml
│   ├── ci.yaml
│   ├── nightly.yaml
│   └── demo.yaml
├── shared/
└── tasks/
```

A project with only one execution profile still uses the
`run_config/` directory — put the single file inside (e.g.
`run_config/dev.yaml`). There is no root-level "default" run
config file; every invocation names its config explicitly.

Because run configs live as files, they sit in version control
next to the Project — a team can `git blame` who set the nightly
sweep to use a specific judge model, and rolling back a run-config
change is trivial. A future UI managing projects and their runs
can present each file in `run_config/` as a named execution profile
the user picks from.

## How Project, Task, and run config compose

Three sources of settings combine at two separate layers when a
run executes.

**Task-level layer** — produces the effective `TaskDescription`
the runner sees for each task:

1. `task.yaml` fields (per-task overrides — highest priority)
2. `project.task_defaults` (project-wide defaults inherited by every
   task)
3. Adapter default (per adapter type)
4. Engine default

Task wins on conflict; unspecified sub-fields inherit. Deep-typed
merge on typed sections (e.g. `environment_manifest.inputs` merges
input by input, not whole-object). Full replacement on certain
fields when the task points at a different file (e.g. a task-local
`compose_file` replaces the project's).

**Run-level layer** — produces the effective run configuration:

1. CLI flags — one-off overrides for this invocation
2. Environment variables — infrastructure fields only (API keys,
   service URLs, executor address)
3. run config file — per-invocation choices
4. `project.yaml` — project-wide defaults
5. Engine default

Higher entries override lower. Fields that only appear in
`project.yaml` (like `default_environment`) apply automatically;
fields that only appear in run configs (like `orchestrator.repeats`
or `models`) are per-invocation only; fields that appear in both
let the run config decide for that specific invocation.

**The two layers don't interact directly.** Each task's resolved
`TaskDescription` runs against the resolved run configuration at
execution time, but they merge on separate chains. A task's
`max_turns` override doesn't change the run's worker count; a run
config's `models.judge` doesn't change any task's tools.

Nothing in this model is required to be complex. A project with
ten similar tasks might have a small `project.yaml`, a slim
`run_config/dev.yaml`, and ten one-line `task.yaml` files. A
project with three hundred tasks all sharing one scenario has one
`project.yaml` declaring the shared setup, one run config file per
invocation scenario under `run_config/`, and three hundred small
task files that mostly say only their own identity.

### The picture, in one diagram

```
project root/
├── project.yaml                    ← the Project (project-wide defaults)
│                                     invariant across runs — compute,
│                                     environment, storage,
│                                     observability, orchestration, ...
│
├── run_config/                     ← named run configs (per-invocation)
│   ├── dev.yaml                    each declares the models THIS
│   └── ...                         run uses, workers, output dir,
│                                     task filter, ...
│
├── shared/                         (optional; assets the Project points at)
│   ├── environment.compose.yaml    (base compose file)
│   └── system_prompt.md            (default system prompt)
│
└── tasks/                          ← the Tasks (one or more)
    ├── task_a/
    │   └── task.yaml               (inherits Project, may override)
    └── task_b/
        └── task.yaml
```

Task-level chain: `merge(project.task_defaults, task.yaml)` →
`TaskDescription`, task-wins. Run-level chain:
`merge(project, run_config, env, CLI)`, later-wins. Both feed the
runner at execution time.

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
| **Run** | `run_config/*.yaml` | This invocation | Which models drive this run, how many workers, output dir for this run, which projects to include |
| **Project** | `project.yaml` | Project lifetime | Default environment, compute/storage/observability/orchestration policies, task-level defaults inherited by every task |
| **Task** | `task.yaml` + task-adjacent files | One task | Task identity, per-task overrides |
| **Trial** | Runtime-only | One trial | Auto-generated ids, per-trial state — never user-configurable |

Project and Task are the *authoring* layers — what task authors
maintain in version control. Run and CLI+env are the *execution*
layers — what operators pick per invocation. Trial is
runtime-produced and not user-configurable.

The precedence rules for how these layers interact are detailed
in "How Project, Task, and run config compose" above and in the
per-field field-ownership table under "Relationship to
the run config" below.

## Config file inventory

The complete set of files a project can ship:

| File | Location | Purpose | Required |
|---|---|---|---|
| `project.yaml` | project root | Project-level defaults + typed sections | Optional |
| `run_config/*.yaml` | project root's `run_config/` directory | Per-invocation config (models, orchestrator run knobs, evaluation choice) | At least one file required for execution |
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
  isolation: "per_trial"
  network_policy: "LOCALHOST_ONLY"
  security_context_defaults:
    user: "toloka"
    group: "toloka"

# ── Task-level defaults ─────────────────────────────────────────
# Applied to every task; task fields override on deep-typed merge.
# This is the primary mechanism for sharing task-level config
# across a project — system_prompt, tools, user_simulator, max_turns,
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

# Note: `models` (agent, user, judge) do NOT live on the Project.
# The model being evaluated is the invocation's variable and lives
# on the run config; see "What a run config is" above and the
# field-ownership table below.

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
| `compute.*` | Orchestrator run-init, `RuntimeBackend` selection | No at task level — `run_config.orchestrator.*` overrides at run level |
| `storage.*` | Artifact writer, log writer, queue backend | No at task level |
| `observability.*` | Tracing/metrics/logging sinks | No at task level |
| `orchestration.*` | Orchestrator run behaviour | No at task level |

Adding a new top-level section is a schema addition on the Project
model. Unknown sections warn but don't fail — older loaders keep
working when new projects declare newer sections.

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
    run_config/<name>.yaml
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

## Relationship to the run config

The rule of thumb is short:

> **Everything about the project lives in `project.yaml`. Everything
> about the invocation lives in the run config.**

A useful test: if you copied the project to a colleague and they ran
it with their own run config, the content of your `project.yaml`
should still be exactly what you meant.

Every concrete setting falls into one of three categories.

### Category 1 — Invariant, `project.yaml` only

Settings that describe what the project **is**. Change any of them
and you're testing a different thing.

- **Identity** — `name`, `version`, `description`.
- **Task inventory** — `tasks.discovery`.
- **Default environment** — `default_environment` in full, including
  isolation, network policy, security context defaults.
- **Task-level defaults** — `task_defaults` in full: `adapter_type`,
  `max_turns` (task-level default), `system_prompt`,
  `user_simulator`, `tools`, `adapter_settings`, `policies`,
  `grading_defaults`.
- **Compute topology** — `compute.provider`, provider-specific
  sub-sections (e.g. `compute.kubernetes.*`), `compute.timeouts`,
  `compute.stuck_heuristics`. These reflect task-shape properties
  (how long tasks reasonably take, what heuristics detect
  agent-stuck states) and deployment topology — not per-run knobs.
- **Observability endpoints** — `observability.tracing`,
  `metrics`, `logging`. Properties of the deployed infrastructure
  the project targets.
- **Orchestration policy** — `orchestration.auto_start_services`,
  `continue_prompt`, `shuffle_trials`, `schedule`. Project-level
  behavioural policy.

### Category 2 — Per-invocation, run config only

Settings that describe **how this specific run happens**. Every
invocation has its own.

- **`evaluation.projects`** (optional) — which projects THIS run
  includes. When omitted, defaults to the enclosing project (the
  project directory the run config file lives in). Explicit only
  for the rare multi-project run. The legacy field
  `evaluation.task_packs` is accepted as a deprecated alias; see
  "Deprecations and migrations" below.
- **`evaluation.tasks_glob`** — filter narrowing which of a
  project's tasks THIS run actually executes.
- **`evaluation.output_dir`** — per-run output location.
- **`evaluation.harness_adapter`** — invocation-level adapter
  selection.
- **`orchestrator.repeats`** — statistical sampling choice for THIS
  run (how many trials per task).
- **`orchestrator.max_turns`** (run-wide ceiling) — distinct from
  the task-level `max_turns` default; this is a run-wide safety
  cap.
- **`engine.presets_file`** — the model-preset overlay for THIS
  invocation.
- **`models`** (`agent`, `user`, `judge`) — the models being
  evaluated. Never on the Project: the model is the invocation's
  variable, the whole point of running the eval. Putting a
  "default" model on the Project would silently affect results
  when a run forgot to declare its own. The orchestrator refuses
  to start a run whose grading uses `llm_judge` without a
  `models.judge` on the run config.

### Category 3 — Configurable, either file (run_config wins)

Settings the Project has a sensible default for, but a specific
run may want to override. The Project sets a default so runs don't
have to declare everything; runs override when they need to.

- **`compute.workers`** — Project's default matches typical
  parallelism for the project's stack; runs scale up for
  nightly sweeps or down for local dev.
- **`compute.max_budget_usd`** — Project has a reasonable
  ceiling; runs may tighten (CI) or loosen (nightly).
- **`compute.runtime_mode`** — Project's default reflects trial
  isolation needs; dev runs may relax to `shared`.
- **`storage.artifacts.path`**, **`storage.logs.path`** — Project
  has a default like `./results`; runs almost always name a
  per-run path (e.g. `results/nightly-2026-07-09`).
- **`storage.queue.backend`** — Project targets `postgres` at
  scale; dev overrides to `sqlite`.

### Field ownership and precedence

Some fields live only in one file; others may appear in both, with
the run config winning on conflict.

| Setting | project.yaml | run config | Resolution |
|---|---|---|---|
| `default_environment` | ✓ | — | Project-only |
| `task_defaults` | ✓ | — | Project-only |
| `tasks.discovery` | ✓ | — | Project-only |
| `models.agent` / `models.user` / `models.judge` | — | ✓ | run_config-only |
| `compute.provider` | ✓ | — | Project-only |
| `compute.<provider>` sub-sections | ✓ | — | Project-only |
| `compute.workers` | ✓ default | `orchestrator.workers` | run_config overrides |
| `compute.runtime_mode` | ✓ default | `orchestrator.runtime` | CLI > run_config > project |
| `compute.max_budget_usd` | ✓ default | `orchestrator.max_budget_usd` | run_config overrides |
| `compute.timeouts` / `stuck_heuristics` | ✓ default | `orchestrator.*` | run_config overrides sub-fields |
| `storage.artifacts.path` | ✓ default | `evaluation.output_dir` | run_config overrides |
| `storage.queue.backend` | ✓ default | `orchestrator.queue_backend` | run_config overrides |
| `observability.*` | ✓ | — | Project-only |
| `orchestration.*` | ✓ default | `orchestrator.*` (matching sub-fields) | run_config overrides |
| `orchestrator.repeats` / `max_turns` (run-wide cap) | — | ✓ | run_config-only |
| `evaluation.projects` (optional; defaults to enclosing project) | — | ✓ | run_config-only |
| `evaluation.harness_adapter` | — | ✓ | run_config-only |
| `engine.presets_file` | — | ✓ | run_config-only |

### Precedence chain — every field

Highest priority to lowest.

For task-scoped fields:

1. **task.yaml** value.
2. **`project.task_defaults`** value.
3. **Adapter default** (per adapter type; includes any
   adapter-specific Domain-bundle merges).
4. **Engine default**.

For run-scoped fields:

1. **CLI flag** (e.g. `--runtime`, `--workers`, `--user-model`).
2. **Environment variable** — infrastructure fields only
   (`DB_SERVICE_URL`, `RAG_SERVICE_URL`, `EXECUTOR_ADDRESS`,
   `TASK_PACKS_DIRS`, provider API keys).
3. **Run config file** value.
4. **`project.yaml`** value.
5. **Engine default**.

### Why the split

Keeping the two files separate lets a single Project spec run under
many different configurations without editing it. A CI pipeline runs
with one run config (parallel workers, fast model), a nightly
regression sweep runs with another (more repeats, stronger model,
larger output volume), and a stakeholder demo runs with a third —
all against the same `project.yaml`.

## Worked scenarios

Three concrete scenarios that show how the same Project spec pairs
with different run config files. Each scenario trims the
files to the fields that matter for the point being made
(`# ...` markers where irrelevant sections are elided).

### Scenario A — Single-machine dev workflow

A team is iterating on a support-triage evaluation project on
their laptops. The Project holds everything the project IS; the
run config is thin because the Project already has good defaults.

Layout:

```
support-triage-pack/
├── project.yaml
├── run_config/
│   └── dev.yaml
├── shared/
│   ├── environment.compose.yaml
│   └── system_prompt.md
└── tasks/
    ├── login_reset/
    ├── password_recovery/
    └── ...
```

`project.yaml`:

```yaml
name: "support-triage-eval"
version: 1
description: "Customer-support triage evaluation suite."

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

compute:
  provider: "local-docker"
  workers: 2
  max_budget_usd: 20.0
  runtime_mode: "per_trial"

storage:
  artifacts: { type: "local", path: "./results" }
  logs:     { type: "local", path: "./logs" }
  queue:    { backend: "sqlite" }

# No `models` here — the model being evaluated lives on the run
# config, not the project.
```

`run_config/dev.yaml`:

```yaml
# evaluation.projects omitted — defaults to the enclosing project.
evaluation:
  output_dir: "results/dev-2026-07-09"

orchestrator:
  repeats: 1

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
```

Invocation: `tolokaforge run --config run_config/dev.yaml`.

**What the boundary looks like here.** Everything about the
project lives in `project.yaml`: the environment, the task
defaults, the compute provider, the storage backends. The run
config names the models being evaluated plus this run's
per-invocation choices (`repeats`, `output_dir`). The Project has
no default model; every run declares its own. `evaluation.projects`
is omitted because the run config lives inside the project's
`run_config/` directory — the enclosing project is the implicit
default.

### Scenario B — Same project under CI and nightly sweeps

The project from Scenario A now ships with two additional run configs
for automated pipelines. Same Project — same identity, same task
defaults, same environment — but two different execution profiles.

Layout:

```
support-triage-pack/
├── project.yaml                    ← unchanged from Scenario A
├── run_config/
│   ├── dev.yaml                    (unchanged from Scenario A)
│   ├── ci.yaml
│   └── nightly.yaml
├── shared/
└── tasks/
```

`project.yaml` is the same as Scenario A.

`run_config/ci.yaml`:

```yaml
# evaluation.projects omitted — defaults to the enclosing project.
evaluation:
  output_dir: "results/ci/${CI_RUN_ID}"

orchestrator:
  repeats: 1
  workers: 2
  max_budget_usd: 5.0               # tighten CI budget

models:
  agent:
    provider: "openrouter"
    name: "anthropic/claude-haiku-4-5"    # cheaper model for CI
  user:
    provider: "openrouter"
    name: "anthropic/claude-haiku-4-5"
    temperature: 0.2
  judge:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.0
```

`run_config/nightly.yaml`:

```yaml
# evaluation.projects omitted — defaults to the enclosing project.
evaluation:
  output_dir: "s3://team-toloka/nightly/${DATE}"

orchestrator:
  repeats: 5                        # higher sampling for nightly
  workers: 16                       # scale up
  max_budget_usd: 200.0             # relaxed budget

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
    name: "anthropic/claude-opus-4-8"     # stronger judge for reliability
    temperature: 0.0
```

Invocation:

```bash
tolokaforge run --config run_config/ci.yaml
tolokaforge run --config run_config/nightly.yaml
```

**What the boundary looks like here.** The Project stays the same
across CI, nightly, and dev — the identity of what's being tested
doesn't change. What varies between the run configs is purely
execution: `repeats` (statistical sampling), `workers`
(parallelism), `max_budget_usd` (cost cap), `models` (which agent
and judge are on the hot seat), and `output_dir` (where results
land). Every run config declares its own `models` in full;
there's no default on the Project.

### Scenario C — Cross-model bake-off

A researcher wants to compare three agent models against the same
evaluation project. The Project holds everything constant so
results are comparable; each run config declares its own models,
holding `user` and `judge` identical across the three so only the
`agent` varies.

Layout:

```
support-triage-pack/
├── project.yaml                    ← unchanged
├── run_config/
│   ├── agent-opus.yaml
│   ├── agent-sonnet.yaml
│   └── agent-gpt5.yaml
├── shared/
└── tasks/
```

`project.yaml` — same as Scenario A. Nothing about models on it;
that's the whole point.

`run_config/agent-opus.yaml`:

```yaml
# evaluation.projects omitted — defaults to the enclosing project.
evaluation:
  output_dir: "results/bake-off/opus"

orchestrator:
  repeats: 5

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
```

`run_config/agent-sonnet.yaml` and `run_config/agent-gpt5.yaml`
are identical except for `models.agent.name` and `output_dir`.

**What the boundary looks like here.** This is the whole point of
keeping models out of `project.yaml`. The subject of the
evaluation — the model being tested — is the invocation's variable;
everything else (environment, task defaults, compute, isolation)
is held constant by the Project so the three runs' scores are
comparable. Because `models` lives only on the run config, there
is no path for a "default model" on the Project to silently
affect results. Each run config declares its `agent`, `user`, and
`judge` explicitly; comparing three run configs is a diff on
those three files alone.

## Isolation — how much a run shares across trials

A run consists of many trials against a project's tasks. The
question this section answers is: **how much state carries from
one trial to the next?** The Project schema supports three
distinct stances — total isolation, completely shared, and
declared mixed — each expressed through the same three schema
knobs combined differently.

At a glance:

| Stance | Cost per trial | Safety guarantee | Setup complexity |
|---|---|---|---|
| Total isolation | Highest — full stack cold-start (~30–45 s) | Strongest — nothing carries over | Simplest (defaults) |
| Completely shared | Lowest — one stack for the whole run (~0 s inter-trial) | Weakest — all state persists across trials | Simple (uniform `shared` labels) |
| Declared mixed | Middle — reset primitives typically ~200 ms | Per-service explicit; strong where declared | More setup (per-service labels + primitives) |

The three isolation knobs the schema exposes:

- **`default_environment.isolation`** (on the Project's default
  environment, or overridable per task) — the task's
  requirement: `per_trial` or `shared_ok`. The orchestrator
  refuses to start a run whose backend can't satisfy the task's
  declared requirement (see ADR-0009).
- **`compute.runtime_mode`** (on the Project's `compute`
  section) — the runtime backend selection: `per_trial`
  materialises a fresh stack per trial; `shared` materialises
  one stack for the whole run.
- **Per-service `tolokaforge.isolation` label** (on services
  inside the compose file) — the per-service mode:
  `ephemeral`, `shared`, or `reset` (with a named reset
  primitive). Only meaningful under `runtime_mode: shared`; under
  `per_trial` the stack is thrown away between trials regardless.

The three stances below combine these knobs differently.

### Stance 1 — Total isolation

Every trial materialises a fresh copy of the stack. Nothing
persists between trials. This is what `PerTrialRuntimeBackend`
does.

```yaml
# project.yaml
default_environment:
  compose_file: "./shared/environment.compose.yaml"
  isolation: "per_trial"          # task requires per-trial isolation

compute:
  provider: "local-docker"
  runtime_mode: "per_trial"       # runtime materialises fresh stacks
```

Per-service labels in the compose file are informational under
this stance — the stack is torn down and rebuilt for each trial
regardless of what any label says.

**Cost.** Highest. Every trial pays a full compose cold-start
(measured at ~30–45 s for a four-service stack). A 100-trial run
pays ~50 minutes of overhead on top of the actual agent work.

**Safety.** Strongest. Nothing can leak from one trial into the
next — services, databases, filesystem state, network topology
all rebuild from the manifest.

**Pick this when:** trials mutate state destructively; safety is
paramount; the pack's compose stack is small enough that
cold-start cost is acceptable; you don't yet trust the pack's
services to be safe to share.

### Stance 2 — Completely shared

The stack materialises once at run start and is shared by every
trial in the run. State persists between trials by default. This
is what `SharedStackRuntimeBackend` does.

```yaml
# project.yaml
default_environment:
  compose_file: "./shared/environment.compose.yaml"
  isolation: "shared_ok"          # task accepts a shared stack

compute:
  provider: "local-docker"
  runtime_mode: "shared"          # one stack for the whole run
```

```yaml
# shared/environment.compose.yaml — every service opts into shared
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
    labels:
      tolokaforge.isolation: "shared"
  backend-api:
    image: "myrepo/example-backend:v1.4.0"
    labels:
      tolokaforge.isolation: "shared"
  # ...
```

**Cost.** Lowest. One stack for the whole run; no inter-trial
overhead. A 100-trial run pays cold-start once, not 100 times.

**Safety.** Weakest. If any trial mutates a service's state, all
subsequent trials see the dirty state. Silent cross-trial
contamination is the failure mode.

**Pick this when:** services are stateless or genuinely read-only
(static-content HTTP servers, immutable reference DBs, catalog
services); the tasks under evaluation only *read* from the
services; large batch runs where cold-start dominates the
wall-clock cost.

**Never pick this when:** any service is mutated by any trial and
you can't afford the mutation to affect later trials. Prefer
Stance 3 instead.

### Stance 3 — Declared mixed (per-service)

The stack materialises once, and per-service labels decide what
happens between trials. Some services stay as-is, some get reset
via a named primitive, some are torn down and recreated. This is
still `SharedStackRuntimeBackend`, but with fine-grained
per-service control.

```yaml
# project.yaml
default_environment:
  compose_file: "./shared/environment.compose.yaml"
  isolation: "shared_ok"

compute:
  provider: "local-docker"
  runtime_mode: "shared"
```

```yaml
# shared/environment.compose.yaml — per-service isolation mix
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
    labels:
      tolokaforge.isolation: "reset"
      tolokaforge.reset_primitive: "postgres_template_db"
      # ~200 ms per trial: CREATE DATABASE new TEMPLATE base

  backend-api:
    image: "myrepo/example-backend:v1.4.0"
    labels:
      tolokaforge.isolation: "shared"
      # stateless, no reset needed

  worker:
    image: "myrepo/example-worker:v1.4.0"
    labels:
      tolokaforge.isolation: "ephemeral"
      # torn down and recreated between trials
```

**Cost.** Middle. Reset primitives are typically ~100× cheaper
than full-stack cold-start (200 ms vs 30–45 s), and `shared`
services pay nothing. Between-trial cost is roughly the sum of
per-service reset costs, dominated by the slowest primitive.

**Safety.** Per-service explicit. `reset` services return to a
known-clean state via the named primitive; `shared` services keep
their state (author asserts this is safe); `ephemeral` services
are fully rebuilt.

**Pick this when:** the pack has a mix — some services are safe
to share (stateless HTTP, immutable catalogs), some need clean
state per trial via cheap resets (postgres template-DB clone,
sqlite truncate), and a few must be rebuilt each time. Most
realistic multi-container workloads land here.

### How the three knobs interact

Two interaction rules matter:

**Rule 1 — the task's requirement must match the backend.**
`default_environment.isolation: per_trial` on a task means the
runtime backend MUST provide per-trial stacks. If the run picks
`compute.runtime_mode: shared`, the orchestrator refuses to start
the run and names the offending task. This is the fail-loud
enforcement from ADR-0009 — silent cross-trial contamination
never gets to happen by config mistake.

**Rule 2 — per-service labels only matter under `shared`.**
Under `runtime_mode: per_trial`, the entire stack rebuilds
between trials; per-service `tolokaforge.isolation` labels have
no effect (the labels are still validated by the loader, they
just don't drive any behaviour).

Task overrides layer on top: a task's own
`environment_manifest.isolation` wins over the project's
`default_environment.isolation`; a task can ship its own compose
file with different per-service labels. Both changes make the
task's resolved environment hash differently (see below), so it
gets its own stack.

### Interaction with content-addressed dedup

The per-service isolation map is part of the hash the
`RuntimeBackend` computes for each task's resolved environment.
Two tasks that agree on the compose file but disagree on
`services.postgres.isolation` do not share a stack — they hash
differently and get separate stacks within the run. The declared
stance is baked into the identity of the stack.

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

Reference table for the per-service labels used in Stance 3 above
(see [Isolation — how much a run shares across trials](#isolation--how-much-a-run-shares-across-trials)
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
entry-point; projects reference it by string tag in `compute.provider`.

**Provider-specific configuration.** Each provider declares its own
typed sub-section under `compute.<provider>`. The loader validates
the sub-section against the selected provider's schema; sub-sections
for unselected providers are ignored.

**Deployment/profile layer.** A `deployments/<name>.yaml` layer
above the Project provides values for placeholders in the project
spec — the same project runs on `dev-cluster` under one deployment
and `prod-cluster` under another without changing `project.yaml`.

## Adopting the Project layer

This document is the design; the engine work to implement it lands
in follow-up ADRs and PRs. The harness surfaces that need
refactoring include:

- **Task discovery and the loader** — currently walks
  `evaluation.tasks_glob` from the run config; needs to read
  `project.yaml` at project root, apply `task_defaults` on task load,
  and synthesize a default Project for projects that don't ship one.
- **Adapter interfaces** — adapters receive a fully-resolved
  `TaskDescription`; the loader needs to merge Project defaults
  into task fields before adapter validation, without changing
  `TaskDescription` shape.
- **`RunConfig` composition** — run config fields override
  same-named `project.yaml` fields at load time; the config loader
  grows a small deep-merge pass.
- **CLI and engine defaults** — engine-level defaults for
  `compute` / `storage` / `observability` / `orchestration` move
  from hard-coded to Project-overridable, with run config values
  and CLI flags able to override on top.
- **Legacy field redesign** — `evaluation.task_packs` on the run
  config becomes `evaluation.projects`, and the field turns
  **optional**: when omitted, the loader defaults to the
  enclosing project (the project directory the run config lives
  in). Explicit use of `evaluation.projects` is only needed for
  the rare multi-project run. `task_packs` stays accepted as a
  deprecated alias with a `DeprecationWarning` at config load.
  The older `docs/TASK_PACKS.md` guide is superseded by this
  document and should be removed once no in-tree link still
  points at it. See the "Deprecations and migrations" section
  below for the user-facing migration notes.

Backward-compat is total by design: a project without `project.yaml`
continues to work through a synthesized default Project. Existing
`adapter_settings.*` fields — including any Domain-bundle
references — flow through unchanged.

## Deprecations and migrations

Two changes carry a deprecation window. Both remain accepted for
now; both should be migrated away from.

### 1. `evaluation.task_packs` on the run config

**Old:** `evaluation.task_packs: [<path>, ...]` on
`run_config.yaml`. A required list naming which packs a run
pulls tasks from.

**New:** `evaluation.projects: [<path>, ...]` — same shape, and
now **optional**. When omitted, the loader uses the enclosing
project (the project directory the run config file lives in).
Explicit only for the rare multi-project run.

**Migration path:**

1. If your run config lives under `<project>/run_config/`, delete
   the `evaluation.task_packs` line entirely — the enclosing
   project is now the default.
2. If your run config lives outside a project directory, or
   combines multiple projects, rename `task_packs:` to
   `projects:` and keep the list.
3. `task_packs:` will continue to be accepted as an alias with a
   `DeprecationWarning` at config load, so scripts and CI keep
   working during the migration.

**Timeline:** the alias survives at least one full minor-version
release cycle after the rename lands. Then it goes away.

### 2. `docs/TASK_PACKS.md`

**Old:** the pre-Project authoring guide at `docs/TASK_PACKS.md`.

**New:** this document (`docs/architecture/PROJECTS.md`) covers
the full model — project + run config + task, with the concrete
schema and worked scenarios.

**Migration path:** update any links you own to point at
`docs/architecture/PROJECTS.md`. The old file carries a
deprecation banner and will be removed once no in-tree link
still targets it.

### Where users see the deprecations

- **At config load:** `DeprecationWarning` when a run config
  uses the legacy `task_packs` field. Names the offending file,
  the legacy field, and the recommended replacement.
- **In `docs/TASK_PACKS.md`:** a banner at the top of the file
  points readers here.
- **In this section:** the canonical migration guide.

## Backward compatibility

- **Projects without `project.yaml` work unchanged.** The loader
  synthesises a default Project from the run config +
  discovered tasks. Existing task-level `environment_manifest`
  declarations are honoured as full overrides.
- **Projects with `project.yaml` get inheritance** for tasks that
  don't declare overrides. Tasks that do continue to work exactly
  as before.
- **Adding sections to `project.yaml` doesn't break older
  projects.** Older projects simply don't declare those sections;
  the runtime uses hard-coded defaults.
- **Existing adapter-specific shared-config patterns** — any
  adapter's Domain-bundle conventions carried through
  `adapter_settings` — continue to work unchanged. The Project
  layer sits above them; nothing about the adapter-side merge
  machinery changes.
- **Run configs** continue to work exactly as today.
  Fields declared in `run_config` override same-named fields in
  `project`.
- **Adapters** receive a fully-resolved `TaskDescription` and
  validate it as always. The Project layer does not modify
  `TaskDescription` shape.

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
- **Unknown section in `project.yaml`.** Warn but preserve.
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

- `project.yaml` at project root — every section declared.
- `shared/environment.compose.yaml` + `shared/system_prompt.md` —
  the base compose + project-level default prompt.
- A minimal `run_config/dev.yaml` — invocation-only fields.
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
