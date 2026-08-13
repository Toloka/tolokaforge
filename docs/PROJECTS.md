# Projects — the top-level abstraction

This document describes the **Project** as the top-level abstraction
in TolokaForge: what a project owns, how tasks inherit from it, how
per-invocation settings compose on top, and how every config file
that a project ships fits into the layered model.

> **Status: Shipped.** M2 (loader, base+delta merging) shipped in
> v0.8.3; M3 (per-service isolation, seed-backed reset recipes, backend
> capabilities, environment identity) shipped in v0.8.4. This document
> reflects the current implementation.

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

## Contents

- [Delta from current implementation](#delta-from-current-implementation)
- [Naming — "Project" vs "task pack"](#naming--project-vs-task-pack)
- [The three pieces of a project](#the-three-pieces-of-a-project)
- [What a Project is](#what-a-project-is)
- [What a Task is](#what-a-task-is)
- [What a run config is](#what-a-run-config-is)
- [How Project, Task, and run config compose](#how-project-task-and-run-config-compose)
- [The five config scopes — reference table](#the-five-config-scopes--reference-table)
- [Config file inventory](#config-file-inventory)
- [The Project schema](#the-project-schema)
- [Task override semantics](#task-override-semantics)
- [Sharing task-level config across many tasks](#sharing-task-level-config-across-many-tasks)
- [Shared assets — seeds and other project-level files](#shared-assets--seeds-and-other-project-level-files)
- [Grading model — what the harness owns](#grading-model--what-the-harness-owns)
- [Field ownership](#field-ownership)
- [Worked scenarios](#worked-scenarios)
- [Isolation — how much a run shares across trials](#isolation--how-much-a-run-shares-across-trials)
- [Services and backends — scaffolding](#services-and-backends--scaffolding)

## Delta from current implementation

The Project layer landed across two milestones. M2 delivered the loader
and both base+delta merge chains, the environment-schema restructuring
(`EnvironmentPatch` as the type of both `default_environment` and task
`environment_manifest`, with the `stack` sub-object and post-merge
`resolve()`), strict unknown-key rejection, `evaluation.projects`,
dual-home knob resolution, task-schema relaxation to `task_id` +
`description`, and `${VAR}` run-config interpolation. M3 delivered
per-service isolation (`services.<name>.isolation`:
`shared` / `reset` / `ephemeral`), seed-backed reset recipes,
task-driven backend selection, backend-capability admission, the
shared-assets registry, the grading provider registry, and
`resolve_environment_identity`. See the CHANGELOG for the per-release
detail.

Two ADR-alignment facts carry into the current design: the isolation
default stays `per_trial` per ADR-0009 (unlabelled services default to
`ephemeral`), and the per-service isolation vocabulary amends ADR-0018 —
see the ADR's "Amendment" section. Compose files carry no isolation
semantics; the manifest is the only home for per-service treatment.

## Naming — "Project" vs "task pack"

Two vocabularies live in the codebase side by side, on purpose.

**"Project"** is the *abstraction*: the eval spec at a pack root,
comprising identity, `default_environment`, `task_defaults`,
`run_defaults`, and task discovery. Every user-facing surface —
schemas, docs, error messages, CLI — uses this name. It's the
term to prefer whenever you're talking about what a pack IS.

**"Task pack"** is a *filesystem-layout* term: "a directory that
contains task files." It appears in implementation plumbing
(adapter parameters, Docker-mount helpers, env-vars) where the
concept genuinely is layout-only — the code cares about which
directories to glob and mount, not about the wrapping
abstraction. Those internal names aren't in flight to change
because they describe the layout, not the concept. A future
reader of `mounts.py` or the Docker helper scripts will still
encounter "task pack" and it will still be the right word there.

Three legacy *user-facing* task-pack names survive as deprecated
aliases during the migration window and retire in M5 (#214):

- `evaluation.task_packs` on the run config — canonical field is
  `evaluation.projects`. The alias is coerced by
  `EvaluationConfig`'s `mode="before"` validator with a
  `DeprecationWarning`.
- `docs/TASK_PACKS.md` — the pre-Project authoring guide; carries
  a deprecation banner pointing at this document.
- `TASK_PACKS_DIRS` environment variable — used by Docker flows
  to name container-visible pack roots. Retirement is coordinated
  with the Docker override generator's rename.

After M5, "task pack" is gone from every user-facing surface but
survives in implementation plumbing where it names the filesystem
layout. This split is deliberate: promoting the abstraction to a
better name doesn't force renaming every "directory of files"
mechanism that the abstraction happens to sit on top of.

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
    type, prompt frame, actor personas, tools, timeouts,
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
  type, turn budget, system prompt, actor personas, tools,
  grading combine method, timeouts, stuck-heuristics).
- **`run_defaults`** — the base every run inherits (compute
  provider, storage backends, observability exporters,
  orchestrator run knobs).

One file, one place per shared block. There is exactly one
`project.yaml` per project. A minimal example — the full field list lives in
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
  stack:
    compose_file: "./shared/environment.compose.yaml"

task_defaults:
  adapter_type: "native"
  max_turns: 20
  system_prompt: "./shared/system_prompt.md"
  # interaction_mode: "agent_only"   # optional; leave unset to inherit
                                     # engine default "conversational".
                                     # See ADR-0028.
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
```

### Project = scenario = domain, one-to-one

A project maps to exactly one evaluation scenario (customer-
support triage, backend refactoring, deep-research question-
answering, ...). The Project layer formalises what has always
been implicit: **one project = one scenario = one domain**.
Multi-domain projects and multi-project domains are not
supported: ship two projects for two scenarios; duplicate a
shared domain bundle across projects rather than coupling them.

Cross-scenario runs are a run-config concern — list multiple
projects in `evaluation.projects` (the successor of today's
`evaluation.task_packs`; optional, defaults to the enclosing
project). Each project supplies its own defaults; the
harness composes the run from the projects, not by mixing
scenarios inside one Project. A run config belongs to its
*enclosing* project — the loader walks up from `--config` to the
nearest `project.yaml`. Run-scoped fields resolve against that
project's `run_defaults` alone; each listed project contributes
its tasks resolved against its own `task_defaults` and
environment.

## What a Task is

A **Task** is one concrete objective within the project's
scenario — one thing the agent has to accomplish. A task might be "add a `/health/ready`
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
Project — same environment, same tools, same turn
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
  `cache_images`, `harness_adapter`, `grading_validation`.
  This section has no equivalent in `run_defaults`; it's
  per-invocation only.
- **`engine`** — `presets_file` and other invocation-time engine
  config.

A slim example — a variant of the one that ships with the microservices
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
`environment_manifest.stack.inputs` merges input by input). Full
replacement on certain fields — the trigger is the presence of
the `stack.compose_file` key in the task's patch (never path
identity), which replaces the project's entire `stack`; see
[Task override semantics](#task-override-semantics) for the
precise rule.

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
counterpart (`evaluation.output_dir`, `engine`) come entirely
from the delta file; `models` usually does too, though shared
model settings may sit in `run_defaults.models`.

### Deep-merge, precisely

The same four rules apply at every layer boundary in both
chains. (Seed *overlays* are deliberately not governed by this
algebra — they are data applied onto data, with kind-defined
semantics; see
[Shared assets](#shared-assets--seeds-and-other-project-level-files).)

- **Maps merge key-by-key, recursively**; the higher layer wins
  on scalar conflict.
- **Lists replace wholesale.** No union, no append — a task that
  touches `tools.agent.enabled` supplies the complete list it
  wants. (Union semantics cannot express removal; replacement is
  predictable.)
- **Explicit `null` unsets**: setting a key to `null` in a
  higher layer discards the lower layer's value and falls
  through to the next default below it (adapter then engine on
  the task chain; engine on the run chain).
- **Omission and `{}` mean inherit** — an empty map is not a
  clear; to clear, write `null`.

### Patches and the resolved environment document

Both sides of the environment merge are **patches**, not resolved
documents. `default_environment` and a task's
`environment_manifest` share one patch type (`EnvironmentPatch`):
every field optional, no filesystem access at construction. The
current `EnvironmentManifest` — required `compose_file`,
`extra: forbid`, validators that open the compose file at
construction time — is the *output* type, produced once per task
by the loader:

```
resolve(project.default_environment, task.environment_manifest,
        project_root)  →  resolved environment document
```

`resolve()` deep-merges the patches (with the `stack` atomic-
replacement rule above), anchors every relative path to the file
that declared it, and only then runs the disk-touching
validation — cross-checks (`runner_service` exists in the
compose, `services` and `initial_state` keys name real services)
are meaningful only post-merge, so that is the only place they
run. Since nothing loads these files before M2, this is a clean
restructuring, not a compatibility dance.

The **resolved environment document** is a first-class unit, not
an implementation detail: merged fields + a snapshot of the
effective compose content + resolved `inputs` + pinned seed
digests. It is the single thing that (a) a runtime backend
translates into a running stack, (b) export tooling inlines when
shipping a self-contained task, and (c) any future
content-addressed stack identity hashes. Anything not captured in
the document is, by definition, not part of the environment.

**The two chains don't interact directly.** Each task's resolved
`TaskDescription` runs against the resolved run configuration at
execution time, but they merge on separate chains. A task's
`max_turns` override doesn't change the run's worker count; a run
config's `models.judge` doesn't change any task's tools. The
two named exceptions are deliberate and bounded: the run-side
caps below clamp task-resolved values at execution time, and the
roster ⊆ models rule cross-checks the chains once, at load.

### Knobs that exist in both chains

Four task-shape knobs currently exist on both sides — `max_turns`,
`timeouts`, `stuck_heuristics`, `continue_prompt` appear in
`task_defaults` *and* in `orchestrator`. This proposal makes the
task chain canonical for task shape and redefines the run-side
copies as **caps or removes them**:

- `orchestrator.max_turns` is a run-level hard cap, not a value.
  Today it defaults to `50` — an always-on cap that clamps every
  task's declared `max_turns` down to at most 50. Effective turn
  budget = `min(task-resolved max_turns, orchestrator.max_turns)`;
  a task can never raise itself above the run's cap. To let a task
  ship a higher `max_turns`, raise `orchestrator.max_turns` in the
  run config to match. A future release (tracked in #534) will flip
  the default to unset (opt-in), so this cap will only apply when
  the operator sets it explicitly.
- `orchestrator.timeouts` caps the task-resolved timeouts the
  same way, and is optional the same way. M2 unifies the two
  timeout shapes onto the task-side field names
  (`trial_seconds`, `tool_call_seconds`); the orchestrator
  `TimeoutConfig`'s current `turn_s`/`episode_s` names are
  legacy and retire with the other aliases in M5.
  Only `trial_seconds` is enforced today. `tool_call_seconds` —
  and the `turn_s` it resolves against — reach `TrialRunner` and
  are read by nothing; a per-call budget is the tool's own, and
  making one pack-declarable is tracked in
  [#1147](https://github.com/Toloka/tolokaforge/issues/1147).
  The `tool_call_seconds: 60` lines in the layouts in this
  document are therefore declared, not yet enforced.
- `orchestrator.stuck_heuristics` is deprecated by this design;
  `task_defaults` is its canonical home (legacy alias retired in
  M5, #214). The conductor reads the task-scope block when the
  task declares one and falls back to the orchestrator copy when
  it does not, so the run-side block is still what most trials
  run at. Operator-side runaway protection is already covered by
  `max_budget_usd`, the `max_turns` cap, and the timeout caps.
- `continue_prompt` is currently consumed by **nothing** in
  either home. M2 either wires the conductor to the
  task-resolved value or drops the field entirely; it must not
  survive as dead config in two places.

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

## The five config scopes — reference table

Pulling all of the above together, TolokaForge has five layers of
configuration that combine to determine what actually runs:

| Scope | Owned by | Lifetime | What lives here |
|---|---|---|---|
| **CLI + env** | Operator | This invocation | `--runtime`, `--user-model`, `--judge-model`, `--presets-file` (a `--workers` flag is planned with M2); env vars for API keys and service URLs (the `USER_MODEL`/`JUDGE_MODEL` env vars are retired by this design) |
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
| `task.yaml` | task dir | Task spec (identity, adapter, max_turns, initial_user_message, initial_state, tools, actors, metadata, policies, grading path, system_prompt, adapter_settings, environment_manifest) | Required |
| `grading.yaml` | task dir | Grading rules (combine, state_checks, transcript_rules, trace_checks, llm_judge, custom_checks) | Required |
| `environment.compose.yaml` | task dir | Task-local compose (used when the task overrides the project default) | Optional |
| `initial_state.json` | task dir | Task-local seed payload, or an overlay on a named project seed | Optional |
| `shared/seeds/*` | project root | Named seed baselines declared in `project.yaml`'s `assets` registry | Optional |
| `fixtures/` | task dir | Test data referenced by tools/services/initial_state | Optional |

**Adapter-specific files.** Adapters that group their tasks under a
Domain typically ship their own conventions for further sharing —
a Domain bundle directory next to the tasks, referenced from
`adapter_settings`. Those files are adapter-specific and documented
with the adapter they belong to; they are not part of the Project
schema. The harness passes them through as opaque
`adapter_settings` data.

The Project layer adds one file (`project.yaml`); the schema
changes it entails for existing file kinds (`stack` grouping,
`actors`, seed references, relaxed `task.yaml` requireds) are
enumerated exhaustively in
[Delta from current implementation](#delta-from-current-implementation).

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

# ── Shared assets ───────────────────────────────────────────────
# Named baselines that tasks and service recipes reference by
# name. See "Shared assets — seeds and other project-level files"
# for the full model.
assets:
  seeds:
    app_baseline:
      path: "./shared/seeds/app_baseline.sql"
      kind: "sql_dump"             # bound to a service by the reset recipe below
      digest: "sha256:ea86…"       # stamped via `tolokaforge assets stamp`

# ── Default environment ─────────────────────────────────────────
# Every task inherits unless it declares its own environment_manifest.
# Task-level environment_manifest deep-merges on top per-task.
default_environment:
  # `stack` is the substrate slot: the pointer to the concrete
  # runtime definition plus everything scoped to that specific
  # file. It replaces ATOMICALLY — see Task override semantics.
  # Today it is compose-shaped; a future backend kind adds a
  # sibling shape, not new root fields.
  stack:
    compose_file: "./shared/environment.compose.yaml"
    runner_service: "runner"
    inputs:                        # variables substituted into this compose file
      postgres_version: "16"
  # Everything below is substrate-neutral — no compose-isms.
  # Two classes, though: policy REQUESTS (network_policy,
  # security_context_defaults) survive a stack replacement;
  # service-TREATMENT fields (services, initial_state) are
  # scoped to the reviewed stack and reset with it (see Task
  # override semantics).
  # Per-service isolation lives in `services.<name>`; services
  # without an entry default to `ephemeral` (the overall default
  # per ADR-0009 stays `per_trial`).
  services:                        # per-service semantics live in the manifest,
    postgres:                      # never in the substrate file
      isolation: "reset"
      reset: { seed: "app_baseline" }  # seed-backed reset recipe (see Shared assets)
    db-service:
      isolation: "shared"
    backend-api:
      isolation: "shared"
    # runner: no entry → ephemeral
  network_policy: "no_internet"    # closed enum: no_internet | limited_internet |
                                   # full_internet; parameterisation (e.g. egress
                                   # hosts) is finalised before the first major release
                                   # (uppercase enum names — NO_INTERNET — are accepted
                                   # as a legacy alias with a DeprecationWarning)
  security_context_defaults:
    run_as_user: "toloka"          # username or numeric UID (int | str); substrates
    run_as_group: "toloka"         # that require numeric IDs (k8s runAsUser) get the
                                   # resolved UID at materialisation
                                   # (legacy `user` / `group` keys are accepted as
                                   # aliases for run_as_user / run_as_group with a
                                   # DeprecationWarning)

# ── Task defaults — base for every task ─────────────────────────
# Applied to every task; task.yaml deltas deep-merge on top.
# Includes task-shape properties (timeouts, stuck_heuristics,
# continue_prompt) that describe how the pack's tasks are
# shaped, not how a particular invocation executes.
task_defaults:
  adapter_type: "native"
  max_turns: 20
  system_prompt: "./shared/system_prompt.md"
  actors:
    user:                          # the conventional counterpart actor; more can be added
      mode: "llm"
      persona: "curious engineer"
      backstory: "./shared/user_backstory.md"  # shared user backstory; each task may override
  policies:
    max_tool_calls_per_turn: 10
  metadata: {}
  adapter_settings: {}
  tools: {}
  grading_defaults:
    combine:
      method: "weighted"           # combiner methods: all | weighted | any
      pass_threshold: 0.8
      weights:
        # `state_checks` is illustrative — it names the future
        # provider-registry check kind that lands after the
        # in-process engine cleanup (#217). Today's live check
        # kinds on the runner side are `transcript_rules` and
        # `llm_judge`; a v1 pack that grades today should
        # weight those.
        state_checks: 0.5
        llm_judge: 0.5
    llm_judge:                       # project-default judge customization (no rubric here)
      customization:
        disable_knowledge_search: true  # withhold the judge's KB tools by default;
                                        # a task may override with `false`
  timeouts:
    trial_seconds: 600
    tool_call_seconds: 60
  stuck_heuristics:
    enabled: true
    max_repeated_tool_calls: 5
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
    # max_turns: 60      # run-level hard cap (default 50): effective =
    #                    # min(task, this). Raise above a task's declared
    #                    # max_turns to let it stand uncapped. #534 will
    #                    # flip the default to unset (opt-in cap).
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
  stack:
    inputs:
      postgres_version: "17"        # overrides project default of "16"
```

The task's resolved manifest = resolve(`project.default_environment`,
`task.environment_manifest`, task-wins) — see
[Patches and the resolved environment document](#patches-and-the-resolved-environment-document).
Touching `stack.inputs` alone deep-merges: the task stays on the
project's compose file, with one substituted variable changed.

The same deep-merge governs `grading_defaults.llm_judge.customization`. Its
`disable_knowledge_search` is **tri-state**: unset (the faithful default — the
judge keeps whatever KB tools the agent had), `true`, or `false`. A task's own
`grading.yaml` under `llm_judge.customization` wins field-by-field, and because an
unset task key never overrides a set project key, a task can flip a
project-disabled judge back on with an explicit `false`:

```yaml
# tasks/self_contained_rubric/grading.yaml
llm_judge:
  customization:
    disable_knowledge_search: false   # this task's rubric needs the agent's KB;
                                      # override a project default that disabled it
  rubric:
    criteria:
      - id: cites_policy
        description: "Cites the applicable policy section"
        kind: binary
        weight: 1.0
```

When neither layer sets the field, the config is byte-identical to a task with no
customization block at all. The block is judge-side only — it never changes the
agent's tools (see [GRADING.md](GRADING.md#judge-kb-faithfulness)).

`customization.system_prompt` layers the same way. It is a `str | None` that
replaces the judge's default grading-stance body (the harness always appends the
marker contract). A task-level string overrides a project default; **omitting the
key inherits the project value**, while **`system_prompt: null` (key present,
value null) resets a project-level custom prompt back to the default**. Present-key
`null` overriding and absent-key inheriting is how the merge treats every field,
not a property of this one. An empty string `""` is rejected loudly at load, not
treated as a reset:

```yaml
# tasks/policy_graded/grading.yaml
llm_judge:
  customization:
    system_prompt: null   # revert to the default judge prompt over a project custom one
  rubric:
    criteria:
      - id: cites_policy
        description: "Cites the applicable policy section"
        kind: binary
        weight: 1.0
```

`customization.include_agent_system_prompt` layers the same way. It is a `bool |
None` that controls whether the harness embeds the agent's policy / system prompt
in the judge's opening-message evidence. Unset and `true` both include it (today's
behaviour); `false` omits it so a self-contained rubric grades without the agent's
framing. A task **sets `include_agent_system_prompt: true` or `null` to re-include
over a project `false`**, while **omitting the key inherits the project value** —
the same present-null-resets / absent-inherits merge as every field:

```yaml
# tasks/self_contained_rubric/grading.yaml
llm_judge:
  customization:
    include_agent_system_prompt: false   # grade this rubric without the agent's policy
  rubric:
    criteria:
      - id: cites_policy
        description: "Cites the applicable policy section"
        kind: binary
        weight: 1.0
```

This gates *evidence*, not the judge's wording — distinct from `system_prompt`. It
is judge-side only (see
[GRADING.md](GRADING.md#gating-the-agents-policy-out-of-the-judges-evidence)).

### Full override — replace entirely

Some fields replace instead of merge:

- **`environment_manifest.stack.compose_file`**: the presence of
  the `compose_file` **key** in a task's `stack` patch replaces
  **the whole `stack` object** — the trigger is key presence,
  decidable at parse time, never path identity (a task
  re-declaring what resolves to the same file still replaces; a
  patch touching only `inputs`/`runner_service` deep-merges). The
  new compose file arrives with a clean slate of `inputs` and
  `runner_service` (a foreign file's `${var}` slots must never
  silently capture inherited values). Replacement also resets the
  service-*treatment* fields: the project's service-keyed maps
  (`services` entries, `initial_state`) are discarded — the
  project's per-service opt-outs reviewed the project's services,
  and they must not silently extend to a stack nobody reviewed
  under it. Policy-*request* fields (`network_policy`,
  `security_context_defaults`) are stack-independent and survive.
  A task-local stack declares its own `services` entries if it
  needs them; anything unlisted falls back to `ephemeral`.
  `stack: null` and `stack: {compose_file: null}` are both load
  errors — a task cannot unset the environment (or its substrate
  pointer) out from under a project that declares one, and there
  is no engine-default compose file to fall through to.
- **`system_prompt`**: pointing at a different file (or inline
  text) replaces the project prompt entirely; no merge.

```yaml
# tasks/schema_isolation_migration/task.yaml
task_id: "schema_isolation_migration"
description: "..."

environment_manifest:
  stack:
    compose_file: "./environment.compose.yaml"
    runner_service: "runner"
```

### Fields that cannot be project-scoped

- `task_id`, `description` — inherent identity.
- The task's *effective* initial state — a task may reference a
  [named project seed](#shared-assets--seeds-and-other-project-level-files),
  but the seed+overlay resolution is always per task.
- `grading.yaml`'s `llm_judge.rubric`, golden actions, and
  expected values — the audit trail of what "correct" means for
  that task. (Check *mechanism* defaults — the `combine` block,
  transcript-rule defaults — are shareable via
  `grading_defaults`; see
  [Grading model](#grading-model--what-the-harness-owns).)
- Fixture file contents — per-task data.

## Sharing task-level config across many tasks

The Project's `task_defaults` section is the primary mechanism for
sharing task-level fields across every task in a project:

- `adapter_type` — same for every task in the project (typical
  pattern: every task uses the same adapter).
- `system_prompt` — the shared system-prompt frame; each task can
  add specifics.
- `tools` — the tool set exposed to the agent.
- `actors` — the roster of simulated counterpart actors, each
  with mode, persona, and backstory template (each task
  contributes only per-actor scenarios; see
  [Actor composition](#actor-composition)).
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

### Actor composition

Simulated counterpart actors live under `actors`, keyed by role
name. Today's benchmarks field exactly one — the conventional
`user` — but the structure is a map from day one so that a
multi-actor benchmark (developer, team lead, QA) is an addition,
not a migration. `models.<actor_name>` supplies each actor's
model; `models` is already an open map, not a fixed
agent/user/judge trio. The legacy root-level `user_simulator`
block is read as an alias for `actors.user` until M5 retires it.

An `ActorSpec` carries `mode` (`llm` or `scripted`), `persona`,
`backstory` (a path to a backstory file, or inline text), and
`scripted_flow`. The project declares the shared defaults under
`task_defaults.actors.user`; each task overrides field-by-field
(delta-wins), so a task that adds only a `backstory` inherits the
project's `mode` and `persona`:

```yaml
# project.yaml → task_defaults.actors
actors:
  user:
    mode: "llm"
    persona: "customer"
    backstory: "./shared/user_backstory.md"
```

```yaml
# tasks/MAN-34/task.yaml
actors:
  user:
    backstory: "./tasks/MAN-34/backstory.md"
```

A task's `backstory` replaces the project default wholesale (same
full-replacement rule as `system_prompt`). Template + per-task
`scenario` interpolation — one shared backstory template with a
task-supplied scenario spliced in — is not part of `ActorSpec`
today; it is tracked in #499.

Three shape rules protect the roster's future:

- **`agent` and `judge` are reserved actor names** — they are
  harness roles sharing the `models.*` namespace, and a benchmark
  persona named `judge` (a courtroom, a code review) must not
  collide with the grading judge's model key. The loader rejects
  them as roster keys.
- **Roster ⊆ models, checked at load.** Every *model-backed*
  actor in the resolved roster (`mode: "llm"`) must have a
  `models.<actor_name>` entry in the resolved run config; the
  loader fails loud naming the run config and the missing key.
  Scripted actors (`mode: "scripted"`) need no model and are
  exempt. This is the chains' one *load-time* cross-check (the
  run-side caps clamp at execution time), and it runs at load,
  never as a mid-run KeyError.
- **Reserved sub-fields**: `actors.<name>.tools` (per-actor tool
  sets — today's two-party `tools.agent`/`tools.user` shape
  becomes actor-keyed when a third actor arrives) and, more
  tentatively, `actors.<name>.service` (binding an actor to a
  manifest service for in-sandbox actors — `runner_service` is
  the agent-shaped precursor of this pattern). Neither is
  implemented; both are name-squatted so later additions don't
  restructure the map.

The actor *roster* shape is settled by this document; turn-taking
topology (who speaks when, who sees whom) and per-actor grading
attribution are deliberately left to a future iteration.

## Shared assets — seeds and other project-level files

Tasks reference files as well as fields: seeds, prompts, compose
files. Fields share through `task_defaults`; files share through
the **assets registry**:

```yaml
# project.yaml
assets:
  seeds:
    manufacturing_baseline:
      path: "./shared/seeds/manufacturing_baseline.json"
      kind: "json_db"              # json_db | sql_dump | files
      digest: "sha256:9f2c…"       # stamped; verified at load
```

A bare string is accepted as shorthand for `{path: <s>}` with the
kind inferred from the extension where unambiguous; the struct is
canonical — a seed is a named, *typed* baseline, and the type is
schema, not a comment. Kind determines the default
materialisation target: `json_db` → the JSON-DB service; `files`
→ the agent workspace; `sql_dump` has no default target — it must
be bound to a service, either by the recipe that consumes it
(`services.<name>.reset.seed`) or by an explicit `target:
<service>` at the reference site. The shipped
`EnvironmentManifest.initial_state` service-keyed map is a legacy
lowering of this registry and folds into it at M2.

```yaml
# tasks/MAN-34/task.yaml
initial_state:
  seed: "manufacturing_baseline"          # by name, not by path
  overlay: "./initial_state_delta.json"   # optional; deep-merged on the seed, task wins
```

Rules:

- **Reference by name.** Only `project.yaml` knows paths; task
  files contain no `../..` traversal, and "which tasks use this
  seed" is a grep over one key.
- **Overlays are the task's delta on the seed** — data applied
  onto data, *not* a config merge. The config algebra's
  list-replacement rule would make "add one record" require
  re-declaring the whole table, so overlay application is
  defined by the seed **kind** instead: `json_db` — records
  appended per table (replacing or removing an existing record
  means declaring that table wholesale); `sql_dump` — the
  overlay SQL executes after the baseline; `files` — overlay
  files copy over the baseline tree. The harness applies these
  mechanically and still never interprets record contents. Three
  tasks that share a seed byte-for-byte except one extra record
  declare one seed and one three-line overlay, instead of three
  forked copies.
- **Digest pinning fails loud on silent seed drift.** The digest
  lives on the **registry entry**, so one pin covers every
  reference path — task `initial_state.seed` references *and*
  service reset recipes alike — and the loader verifies file
  content against it before any trial runs. Why it matters:
  seeds, golden actions, and rubric text form a consistency
  triple — the golden hash is recomputed live from the seed at
  grading time, but a seed edit can invalidate recorded
  golden-action arguments and falsify quantities hard-coded in
  rubric prose, and only the first two are machine-checked. A
  digest mismatch names the seed and every task and recipe that
  references it, forcing a human to re-verify all three.
  Re-stamping after a deliberate edit is a CLI verb
  (`tolokaforge assets stamp`; final name settled in M2), not a
  hand computation — hand-authored projects get the same workflow
  as converter output.
- **Self-containedness rule.** A task directory is self-contained
  *except* for declared project assets (`system_prompt`,
  `compose_file`, seeds). Export tooling must inline declared
  assets when shipping a single task. (`system_prompt` and
  `compose_file` predate the registry as bare path fields; they
  fold into `assets` in a later milestone.)

### What a "seed" is for a generic task

A seed is a **named, typed baseline of starting state** — not a
tau-specific JSON DB. Kinds:

| Kind | Payload | Typical project |
|---|---|---|
| `json_db` | tables + records loaded into the JSON DB service | tau-style domains |
| `sql_dump` | SQL applied to a database service at start-up | compose-stack projects |
| `files` | directory/archive materialised into a service volume or agent workspace | coding / SWE-style tasks |

The harness materialises seeds and never interprets their
contents — kind selection decides *where* the bytes go, not what
they mean. The same baseline serves two consumers:

1. **Trial start-up** — materialise the seed (plus overlay).
2. **The `reset` isolation recipe** — a service declared
   `isolation: "reset"` in the manifest names a seed-backed
   recipe that restores it to baseline between trials. The seed
   *is* what "reset" resets to.

This is deliberate: the isolation vocabulary (M3, #212) and the
initial-state design are one mechanism viewed from two sides — a
service's known-good baseline — not two systems to keep in sync.

## Grading model — what the harness owns

The harness owns grading **mechanics**; adapters own grading
**semantics**. Harness core owns:

- **The combine algebra** — `combine.method`: `all` | `weighted`
  | `any`, one closed set both substrates dispatch on. Anything
  else is refused at load — by `tolokaforge validate` and by
  either substrate's config model, naming what may be written
  instead — never silently defaulted.
- **Transcript rules and JSONPath state checks** — generic over
  the transcript and the DB-service state.
- **Golden-action replay** — the expected end state is recomputed
  live each run: reset to the seed, replay the task's
  `golden_actions` through the real agent tools, hash-compare
  against the trial's final state. No stored expected hash is
  consumed — the seed and the golden actions are the ground
  truth.
- **The rubric judge** — behind backend-neutral protocols
  (`DBReader`, `KnowledgeSearch`); backend-specific KB wiring
  (rag-service, TypeSense) stays in the runner, injected as
  opaque read-only tools.

Adapters and backends plug in through exactly two seams: the
**DB-service contract** (all state reads, snapshots, and resets —
the grader never touches an environment directly) and the
**`KnowledgeSearch` protocol** (judge KB access).

**Check kinds are extensible via a provider registry.** A state
check declares a `kind`; the runner routes it to the registered
provider. Provider-specific config flows through opaquely — the
same passthrough rule as `adapter_settings`. Adapters ship
providers via the existing adapter entry-point mechanism, so a
new benchmark family can add a check type without touching
harness core.

**What `grading_defaults` may hold:** the `combine` block,
default transcript rules, provider defaults — the mechanism
boilerplate that today is copy-pasted identically into every
`grading.yaml`. **What must stay per task:** the rubric, the
golden actions, and expected values — the audit trail of what
"correct" means for that specific task.

One `GradingConfig` is canonical: the runner's. "Standalone"
for the runner means *deployable as a separate service on a
remote machine* — orchestrator, conductor, and runners share one
codebase, and importing core modules is fine. The constraint is
the seam, not the imports: everything that crosses the transport
between them (`TrialSpec`, `TaskDescription`, `Grade`,
artifacts) must be a serializable wire type with no live-object
references. That reading unblocks the config unification
directly — one model, imported by both sides (#83, #217). The
legacy in-process grading engine is slated for removal (#217).

## Field ownership

Every field is either task-scoped or run-scoped. The two scopes
compose on independent chains — a task delta never touches a run
field and vice versa.

| Section | Base (project.yaml) | Delta |
|---|---|---|
| Identity (`name`, `version`, `description`) | project.yaml | — |
| `tasks.discovery` | project.yaml | — |
| `default_environment` | project.yaml | task.yaml's `environment_manifest` |
| `task_defaults.*` — task-scoped (adapter, tools, actors, policies, timeouts, stuck_heuristics, max_turns, adapter_settings, metadata, system_prompt) | `project.task_defaults` | `task.yaml` |
| `task_defaults` — project-scoped only (grading_defaults, continue_prompt) | `project.task_defaults` | — (no per-task delta; `grading_defaults` reaches the engine via `NativeAdapter.get_grading_config`, `continue_prompt` via turn logic) |
| `compute` (provider, workers, budget, rate limits, retries) | `project.run_defaults.compute` | `run_configs/<name>.yaml` |
| `storage` (artifacts, logs, queue) | `project.run_defaults.storage` | `run_configs/<name>.yaml` |
| `observability` (tracing, metrics, logging) | `project.run_defaults.observability` | `run_configs/<name>.yaml` |
| `orchestrator` (repeats, max_turns cap, timeouts cap, auto_start_services, shuffle_trials; the lifecycle-axis field is settled in the M3 ADR) | `project.run_defaults.orchestrator` | `run_configs/<name>.yaml` |
| `models` (open map; `agent`/`user`/`judge` conventional, plus one entry per model-backed actor) | `project.run_defaults.models` (optional) | `run_configs/<name>.yaml` |
| `evaluation` (projects, tasks_glob, output_dir, cache_images, harness_adapter, grading_validation) | — | `run_configs/<name>.yaml` |
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

1. **CLI flag** (e.g. `--runtime`, `--user-model`, `--judge-model`).
2. **Environment variable** — infrastructure fields only
   (`DB_SERVICE_URL`, `RAG_SERVICE_URL`, `TYPESENSE_HOST`,
   `TYPESENSE_PORT`, `EXECUTOR_ADDRESS`,
   `TASK_PACKS_DIRS`, provider API keys). Today the CLI also
   honours `USER_MODEL`/`JUDGE_MODEL` in this slot; this design
   retires them — model choices belong in version-controlled run
   configs (see Delta).
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
  stack:
    compose_file: "./shared/environment.compose.yaml"
    runner_service: "runner"

task_defaults:
  adapter_type: "native"
  max_turns: 20
  system_prompt: "./shared/system_prompt.md"
  actors:
    user:
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
  queue:    { backend: "postgres", postgres_dsn: "${QUEUE_PG_DSN}" }

observability:
  tracing: { exporter: "otlp", endpoint: "http://collector:4317" }
  metrics: { exporter: "prometheus", endpoint: "http://prom:9090" }
  logging: { exporter: "otlp", endpoint: "http://collector:4317" }

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
shared local-docker / sqlite-queue boilerplate from
`run_defaults` (and the project's `per_trial` isolation stance,
which lives in `default_environment`) doesn't appear in either
file.

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

Isolation is expressed **per compose service in the manifest**
under `default_environment.services.<name>.isolation` — the
authoritative declaration with vocabulary `shared` / `reset` (with a
seed-backed recipe) / `ephemeral`. Services without a manifest
entry default to `ephemeral`. There is no manifest-wide isolation
field: compose files carry zero isolation semantics and the manifest
is the single authority for how the harness treats each service
between trials.

Backend selection follows the per-service map: any task with a
`reset` or `ephemeral` service routes the run onto
`PerTrialRuntimeBackend`; runs whose every task labels every service
`shared` route onto `SharedStackRuntimeBackend`. Operators do not
set the backend directly — the legacy `orchestrator.runtime` field
survives as a deprecated override with a `DeprecationWarning`.

Two consistency rules run on the resolved document:

- **Label and recipe must agree.** `ServiceSpec` fails loud when
  `isolation: "reset"` has no `reset.seed` pointer, and when any
  other label carries a `reset` sibling — a stale sibling from a
  deep merge would otherwise sit dangling. The legitimate override
  spells the null-unset explicitly: a task flipping an inherited
  `reset` service to a laxer label writes both keys, e.g.
  `{isolation: "shared", reset: null}`.
- **`services` keys must name real services.** Every
  `services.<name>` entry must resolve against the *effective*
  compose file, post-merge; an entry for a service that doesn't
  exist fails loud (the shipped `initial_state` validators are
  the precedent).
- **Reset seeds bind to `assets.seeds`.** A service labelled
  `reset` names a seed in `project.assets.seeds`; the loader
  verifies the seed file's sha256 against the declared digest at
  load time and dispatches through the recipe registry (kind →
  module under `tolokaforge/runtime/reset_recipes/`) at reset
  time.

**Per-service semantics live in the manifest, never in the
substrate file.** The compose file defines what a service *is*;
the manifest declares how the harness *treats* it. Compose labels
are not an input to isolation — no label mechanism exists and
none is introduced — so a k8s or other backend materialises the
same manifest declarations without any substrate-file annotations
existing at all. (See
[Services and backends — scaffolding](#services-and-backends--scaffolding).)

At a glance:

| Stance | Cost between trials | Safety guarantee | How you get it |
|---|---|---|---|
| Completely shared | Lowest — services persist | Weakest — all state carries over | Every service labelled `isolation: "shared"` in the manifest |
| Declared mixed | Middle — seed-backed resets are cheap relative to teardown | Per-service explicit; strong where declared | Per-service labels + reset primitives |
| Total isolation | Highest — unlabelled services rebuilt per trial | Strong; relaxations require an explicit label | **The default** — declare nothing (services default to `ephemeral`) |

### Stance 1 — Completely shared

Every service persists across trials. State carries over between
trials. This never happens by accident: every service the compose
file declares must be listed under `services` with
`isolation: "shared"`. Any missing entry defaults to `ephemeral` and
routes the run onto `PerTrialRuntimeBackend`.

```yaml
# project.yaml — every declared service labelled shared
default_environment:
  stack:
    compose_file: "./shared/environment.compose.yaml"
  services:
    postgres:
      isolation: "shared"
    backend-api:
      isolation: "shared"
```

```yaml
# shared/environment.compose.yaml — the compose file is the
# substrate definition; isolation semantics live in the manifest.
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

Per-service manifest entries decide what happens between trials.
Some services stay as-is, some get reset via a seed-backed
recipe, some are torn down and recreated. Any service without an
entry defaults to `ephemeral`.

```yaml
# project.yaml — per-service isolation mix, declared in the manifest
default_environment:
  stack:
    compose_file: "./shared/environment.compose.yaml"
  services:
    postgres:
      isolation: "reset"
      reset: { seed: "baseline" }   # restore to the named seed between trials
    backend-api:
      isolation: "shared"
    worker:
      isolation: "ephemeral"
```

```yaml
# shared/environment.compose.yaml — defines what the services ARE;
# the manifest above declares how the harness treats them
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
  backend-api:
    image: "myrepo/example-backend:v1.4.0"
  worker:
    image: "myrepo/example-worker:v1.4.0"
```

**Cost.** Middle. Seed-backed resets are cheap compared to full
teardown (a template-database restore is typically sub-second);
`shared` services pay nothing between trials.

**Safety.** Per-service explicit. `reset` services return to
their seed baseline; `shared` services keep their state (the
project author asserts this is safe); `ephemeral` services are
fully rebuilt.

**Pick this when:** the pack has a mix — some services are safe
to share, some need clean state per trial via cheap resets, some
must be rebuilt each time. Most realistic multi-container
workloads land here.

### Stance 3 — Total isolation (default)

The default stance: every service without a manifest entry
resolves to `ephemeral` — torn down and recreated between trials.
A specific service can still opt out with an explicit `shared` or
`reset` entry if the project author has a reason.

```yaml
# project.yaml — undeclared services default to ephemeral
default_environment:
  stack:
    compose_file: "./shared/environment.compose.yaml"
  services:
    immutable-catalog:
      isolation: "shared"         # explicit exception: safe across trials
```

```yaml
# shared/environment.compose.yaml — no isolation semantics here;
# postgres has no manifest entry → ephemeral
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
  immutable-catalog:
    image: "myrepo/catalog:v1.4.0"
```

**Cost.** Highest. Every undeclared service is torn down and
recreated per trial.

**Safety.** Strong by default. Undeclared services can't
accidentally carry state. Any relaxation is an explicit,
reviewable entry for the specific service that needs it.

**Pick this when:** trials mutate state destructively; safety is
paramount; you don't trust unlabelled services to be safe to
share.

### Network partitioning — excluding a service from the shared internal network

`network_policy` governs *public* egress. Every application service
under `no_internet` and `limited_internet` still shares the
harness-injected `tolokaforge_netpolicy_internal` network, so any
sibling can DNS-resolve and dial any other on ports the compose file
exposes. For a stack that includes an untrusted sibling (an
agent-controlled `bash` container whose only intended egress is a
curated tool-bridge service, for example), `services.<name>.network_access`
opts the named service out of the injected shared network.

Two labels:

- **`default`** — the harness auto-joins the service to the injected
  shared internal network under `no_internet` and `limited_internet`,
  and injects `HTTP(S)_PROXY` env under `limited_internet`. Every
  service defaults to this.
- **`restricted`** — the service joins **only** the networks its
  compose entry declares. The harness does *not* attach the injected
  shared internal net and does *not* inject proxy env. Task-declared
  networks the service joins are still forced `internal: true`, so a
  restricted service remains egress-blocked at the docker-network
  level — it just does not gain the harness's inter-service
  reachability layer.

Two invariants fail loud at manifest load:

- The `runner_service` cannot be `restricted` (it must remain
  reachable from application services and keep its injected edge
  network for LLM-judge grading).
- A `restricted` service's compose entry must declare a non-empty
  `networks:` block (a restricted service with nowhere to attach
  would never come up).

```yaml
# environment.compose.yaml — declare the task network on the sibling
services:
  bash:
    image: bash:5.2-alpine3.20
    networks: [tool_bridge]
networks:
  tool_bridge: {}
```

```yaml
# project.yaml — mark the sibling restricted
default_environment:
  services:
    bash:
      isolation: ephemeral
      network_access: restricted
```

See [`docs/MULTI_CONTAINER_GUIDE.md`](MULTI_CONTAINER_GUIDE.md#partitioning-an-untrusted-sibling)
for the walkthrough and [`docs/SECURITY.md`](SECURITY.md#task-declared-stack-case-b--case-c)
for the threat-model treatment.

### Readiness — declaring a client-reachability contract

A container reporting `Healthy` via its docker `healthcheck:` means the
process is up *inside* the container; it does not guarantee a client on
the host can reach the service through its published port.
`services.<name>.readiness` declares a **host-side reachability contract**
by endpoint kind.

```yaml
default_environment:
  services:
    db-service:
      isolation: shared
      readiness:
        kind: grpc        # grpc | http | tcp
```

- **`kind`** — the only field in v1. Vocabulary and their port/path
  conventions:
  - **`grpc`** — reachability is a gRPC channel reaching READY on the
    service's first published port.
  - **`http`** — `GET /health` on the service's first published port; a
    2xx response is reachable.
  - **`tcp`** — a TCP connect to the service's first published port.
- **Omission** means the service declares **no explicit readiness
  contract** — the docker healthcheck remains its only readiness signal.
- The **runner service cannot declare a `readiness` contract** — it is
  always gated by the built-in gRPC probe on its host port
  (`stack.runner_port`); a `readiness` entry on the runner service is
  rejected at load. Declare it on a non-runner sibling instead.
- `readiness` is orthogonal to `isolation`, `reset`, and `network_access`;
  every combination is legal. It deep-merges by name like every other
  `services.<name>` field — a task-side entry overrides the project-side
  `kind`, and omitting it inherits the project-side contract.

## Services and backends — scaffolding

> Per-service isolation, seed-backed reset recipes, and the
> backend-capability registry are shipped. Later lifecycle events
> (`start` / `terminate` recipes) and Kubernetes / other
> substrates remain future work; nothing below promises a
> restructuring of what ships today.

### Services — lifecycle recipes

`default_environment.services.<name>` is the manifest home for
everything the harness needs to know about a sidecar service.
Today it holds `isolation` (and, for `isolation: "reset"`, a
`reset.seed` pointer at an entry in `project.assets.seeds`),
`network_access`, and `readiness`. The intended evolution is a
**recipe per lifecycle event**:

```yaml
# Illustrative future shape — NOT part of the current schema
services:
  postgres:
    isolation: "reset"
    recipes:
      start:  { primitive: "compose_up" }
      reset:  { primitive: "postgres_template_db", seed: "baseline" }
      terminate: { primitive: "compose_down" }
```

Design constraints already settled:

- Recipes are **named primitives from a registry**, not inline
  scripts — the isolation guard must be able to reason about
  whether a reset recipe satisfies `per_trial` semantics, and
  that is undecidable over arbitrary shell.
- The `reset` recipe's data input is a
  [named seed](#what-a-seed-is-for-a-generic-task) — one
  baseline mechanism for trial start-up and between-trial reset,
  not two.
- The isolation labels (`shared` / `reset` / `ephemeral`) are
  shorthand for *which recipe runs between trials*: `shared` =
  none, `reset` = the reset recipe, `ephemeral` = terminate +
  start.
- Reset dispatchers live in
  `tolokaforge/runtime/reset_recipes/`, one module per kind
  (`sql_dump`, `filesystem_dir`, `redis_dump`, `bare`), and
  populate a `RECIPE_REGISTRY` keyed on `SeedKind`.

### Backends — declared capabilities

The manifest is one level above any concrete substrate — a k8s
cluster can itself be *inside* the sandbox. Compose, k8s, and
future runtimes are **backends** that materialise the same
manifest. Three consequences:

- **Backends advertise capabilities; runs declare requirements.**
  The run side declares what it needs on the run config under
  `compute.capabilities`:

  ```yaml
  # run_configs/dev.yaml
  compute:
    capabilities:
      - reset_recipes:sql_dump
      - network_isolation:no_internet
  ```

  Each backend exposes `advertised_capabilities`; the admission
  gate at run start (`tolokaforge/core/backend_capabilities.py`)
  refuses `requested - advertised` non-empty, and refuses names
  absent from the registry outright. A capability entry is
  `string | {name: <params>}` (the bare string is the
  parameterless common case; quotas and budgets arrive as params
  without a list-to-struct migration). Local-docker's baseline
  vocabulary is `per_trial_stack`, `shared_stack`,
  `reset_recipes:{sql_dump,filesystem_dir,redis_dump,bare}`, and
  `network_isolation:no_internet`.

- **Backend selection is derived from the per-service isolation
  map, not read off a root `isolation` field.** Any task with a
  `reset` or `ephemeral` service routes onto
  `PerTrialRuntimeBackend`; runs whose every task labels every
  service `shared` route onto `SharedStackRuntimeBackend`. The
  deprecated `orchestrator.runtime` field survives as an operator
  override with a `DeprecationWarning`; retirement lands with a
  later cleanup milestone.

- **Enforcement can be delegated to a capable backend.** For
  `network_policy`, the manifest declares the *need*; the *grant*
  is operator-side. A backend that manages network policy
  natively (e.g. k8s with Cilium) advertises that capability and
  becomes the grant's enforcement point — the request/grant split
  stays, only the enforcer moves. `network_policy` itself stays a
  closed enum for now; whether parameters (e.g. requested egress
  hosts) extend it to a struct is a decision to finalise before
  the first major release, not folklore to accrete.

### Environment identity

`resolve_environment_identity(env)` returns a `sha256:...` digest
over the canonicalised compose bytes, `stack_inputs`, per-service
isolation map, and referenced seed digests. Two manifests with
identical inputs produce equal identities regardless of YAML
formatting; any change to a covered input flips the digest. The
orchestrator logs it at info level once per task at run start.
Materialisation / dedup consumers land later per the public
roadmap.

### Explicitly future — not defined by this document

- **Task sequences / cross-task stack persistence** (task A
  prepares state that task B consumes). Nothing here defines
  reset boundaries scoped to a task rather than a trial, and
  `shuffle_trials` / `repeats` / parallel workers provide no
  ordering guarantees. Until a future iteration specifies this,
  every task in a project must be independently runnable.
- **Content-addressed materialisation and dedup** (keying
  substrate materialisation on `resolve_environment_identity`).
  The digest is emitted for observability today; consumers land
  later per the public roadmap.

---

*See the [ADR index](adr/) for decision history and
[`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md) for the canonical
runtime-backends documentation. For the example pack, see
[`examples/native/example-microservices-pack/`](../examples/native/example-microservices-pack/).*
