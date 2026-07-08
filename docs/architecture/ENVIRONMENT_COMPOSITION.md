# Environment composition — how multiple tasks share a stack

This document describes the environment composition model: how task
authors declare per-task environments, how the runtime materialises
them, and how multiple tasks share a running stack without any
explicit "shared environment" declaration.

Companion reading:
[`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md) (the `RuntimeBackend`
seam this model lives on),
[`adr/0009-environment-manifest.md`](adr/0009-environment-manifest.md)
(the `EnvironmentManifest` schema this model extends),
[`adr/0018-multi-container-under-shared-runtime.md`](adr/0018-multi-container-under-shared-runtime.md)
(the case matrix this model broadens).

Implementation ships incrementally through v0.11.0 — see
[`ROADMAP.md`](ROADMAP.md) for the release-by-release status of each
mechanism.

## The model in one paragraph

Every task declares its own `environment_manifest` — a pointer to a
compose file plus a small block of typed inputs. At load time the
loader resolves the compose file and substitutes any bound inputs;
the result is the task's *resolved manifest*. At run time the
`RuntimeBackend` hashes each task's resolved manifest and its
per-service isolation modes. Tasks whose hashes match share a
running stack within the run. Content addressing does the work that
a "shared environment" declaration would otherwise do; there is no
project layer, no inheritance, and no free-form deep-merge.

## The four mechanisms

### 1 · Content-addressed stack dedup at the `RuntimeBackend`

The `RuntimeBackend` computes a stable hash over each task's
fully-resolved compose file, its declared per-service isolation
modes, and its bound input values. Tasks whose hashes match share
one running stack within a single run.

```
                          run start
                             │
              ┌──────────────┼──────────────┐
              │              │              │
       task_a (h=x)   task_b (h=x)   task_c (h=y)
              │              │              │
              └──── same ────┘              │
                     │                       │
              ┌──────▼──────┐        ┌──────▼──────┐
              │  stack #1   │        │  stack #2   │
              │  hash=x     │        │  hash=y     │
              └─────────────┘        └─────────────┘
```

**Registry and lifecycle.** The backend maintains an in-run registry
keyed by hash. The first task with a given hash provisions a fresh
stack and takes the initial reference. Every subsequent task with
the same hash increments the reference count and attaches to the
existing stack. When a task finishes its last trial the count
decrements; when it drops to zero the runtime tears the stack down.

**Hash inputs.** The canonicalised YAML representation of the
resolved compose file, the ordered service-name → isolation-mode
map, and the bound input values. Hash function: SHA-256.
Canonicalisation normalises key ordering, quoting, and whitespace
so that syntactically-different-but-semantically-identical compose
files hash identically.

**Refused dedup.** If two tasks would share a hash on the compose
alone but disagree on `services.<name>.isolation` (one declares
`ephemeral`, the other `shared`), the runtime treats them as
distinct hashes. Sharing requires *identical* declared intent, not
compatible intent — the safety contract on isolation is too load-
bearing to reconcile silently.

**Scope.** A single `tolokaforge run` invocation. Two separate runs
never share a stack even if their manifests hash identical. Cross-
run persistence is a distinct concern (the "middle-ground
isolation" arc in [`ROADMAP.md`](ROADMAP.md)) and lives on its own
mechanism.

### 2 · Typed inputs on `EnvironmentManifest`

A task's `environment_manifest` declares typed inputs alongside its
compose file:

```yaml
# task.yaml (excerpt)
environment_manifest:
  compose_file: "../../shared/environment.compose.yaml"
  inputs:
    postgres_version: "17"       # override — hash differs from default
    redis_memory_limit: "256mb"
```

The compose file uses `${...}` substitution bound at load time,
with defaults expressed in the compose file itself:

```yaml
# shared/environment.compose.yaml (excerpt)
services:
  postgres:
    image: "postgres:${postgres_version:-16}"
    environment:
      POSTGRES_MEM_LIMIT: "${redis_memory_limit:-128mb}"
```

**Properties.**

- Inputs are typed at the schema level. Type mismatches fail at
  load time; no coercion.
- Defaults live in the compose file (`${postgres_version:-16}`) so
  the compose file remains a valid Docker Compose file, readable
  outside of TolokaForge tooling.
- Two tasks that resolve to the same input values hash identically
  — automatic dedup. Two tasks that resolve to different values
  hash differently — automatic isolation.
- Input names are validated against the compose file's declared
  `${...}` references at load time. Unknown input names warn but
  do not fail (a pack may legitimately declare inputs consumed
  elsewhere).

### 3 · Fixed reset-primitive enum on services

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

**Isolation modes.**

| Mode | Behaviour | When to pick |
|---|---|---|
| `ephemeral` | Service is torn down and recreated between trials | Safest; correct when trials mutate state destructively |
| `shared` | Service persists across trials on the same stack | Cheapest; safe only when trials don't mutate service state |
| `reset` | Service persists across trials; state resets between trials via a named primitive | Middle-ground; state-mutating tasks with cheap reset semantics |

The default when a service does not declare an isolation mode is
`ephemeral` — the fail-loud choice.

**Reset primitives** are a closed enum:

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
itself — there is no plugin mechanism. The set is small and slow-
changing, and the safety contract on a reset primitive is too load-
bearing to delegate to third parties.

**Interaction with dedup.** The isolation-mode map is part of the
hash input. Two tasks that agree on the compose file but disagree
on `services.postgres.isolation` do not share a stack.

### 4 · Convention: shared compose files under `<pack>/shared/`

Task packs by convention place shared compose files in
`<pack>/shared/`. Tasks reference them via relative path in
`environment_manifest.compose_file`:

```
example-microservices-pack/
├── shared/
│   └── environment.compose.yaml       # the base stack
├── run_config.yaml
└── tasks/
    ├── api_endpoint_add/
    │   ├── task.yaml                   # compose_file: "../../shared/environment.compose.yaml"
    │   └── grading.yaml
    ├── db_query_tuning/
    │   ├── task.yaml                   # same compose_file → dedups
    │   └── grading.yaml
    └── schema_isolation_migration/
        ├── task.yaml                   # compose_file: "./environment.compose.yaml"
        ├── environment.compose.yaml    # task-local; different hash
        └── grading.yaml
```

**Properties.**

- No new file type. No new schema. Just a relative path each task
  declares in its own `environment_manifest.compose_file`.
- The loader's existing path-resolution logic handles this without
  change (same mechanism used by
  [`native_shared_domain`](../../examples/native/native_shared_domain/)
  for per-category shared config).
- Tasks that need a fully task-local compose file declare a task-
  local path and hash differently — the escape hatch is not a
  special case, it's the default behaviour when the path resolves
  to a task-local file.
- The directory name `shared/` is a convention, not a rule; packs
  are free to organise as they like. The loader doesn't care about
  the directory name.

## Worked example

See
[`examples/native/example-microservices-pack/`](../../examples/native/example-microservices-pack/)
for the full spec. Six tasks demonstrating the four mechanisms:

| Task | Mechanism | Resulting stack |
|---|---|---|
| `api_endpoint_add` | shared compose reference, default inputs | **A** |
| `db_query_tuning` | shared compose reference, default inputs | **A** (dedup) |
| `postgres_upgrade_test` | typed input override (`postgres_version: "17"`) | **B** |
| `schema_isolation_migration` | task-local compose file | **C** |
| `agent_flow_setup` | shared compose reference — sequence part 1 | **A** (dedup) |
| `agent_flow_verify` | shared compose reference — sequence part 2 | **A** (dedup) |

Six tasks, three stacks. Four tasks share stack **A**; one task
each has stack **B** and stack **C**. The pack's
[`README.md`](../../examples/native/example-microservices-pack/README.md)
walks each task in order and shows the resolved compose + hash for
each.

## Semantic sharing — the case that requires no separate mechanism

The `agent_flow_setup` / `agent_flow_verify` pair in the example
pack demonstrates the scenario where task B reads state task A
wrote. They must run on the same postgres process for B's grading
to have anything to grade.

Under this model there is no `share_stack_with:` or `stack_group:`
field. Instead: both tasks reference the same shared compose file
with the same input values, so their hashes match, so the runtime
materialises one stack for both. Task A writes; task B reads. The
sharing is a natural consequence of identical environment
declarations, not a separate declared property.

For this to work, `services.postgres.isolation` must be `shared`
(state persists across all trials of all tasks that share the
stack). If it were `reset`, the reset primitive would fire between
every trial, wiping A's writes before B could read them. The task
author's choice of isolation mode encodes the sharing semantics.

## Backward compatibility

Every mechanism is additive.

- Existing task packs work unchanged. A pack with per-task compose
  files continues to function; content-address dedup applies
  opportunistically when compose files hash identical.
- Existing task-level `environment_manifest.compose_file` paths
  work verbatim. The `shared/` convention is a convention, not a
  rule.
- Existing `EnvironmentManifest` fields are preserved. The new
  `inputs:` block is optional.
- `SharedStackRuntimeBackend` and `PerTrialRuntimeBackend` see the
  same manifest interface as today. Dedup sits below the
  `RuntimeBackend` seam.
- The existing
  [`native_shared_domain`](../../examples/native/native_shared_domain/)
  per-category deep-merge (for `tools:` and `system_prompt:`) is
  unchanged — that merge is about task-level concerns and does not
  interact with this environment-side model.

No migration is required. A pack that wants to reduce duplication
moves a compose file into `<pack>/shared/` and points task-level
paths there; that's the full migration.

## Failure modes and how the model handles them

- **Silent cross-trial contamination.** A `shared` service persists
  across trials of a task; a trial mutates state; the next trial
  sees dirty state. Prevention: default isolation for undeclared
  services is `ephemeral` (fail-loud). Authors opt into `shared`
  per service; the isolation decision is visible in the compose
  file.
- **Silent cross-task contamination.** Tasks A and B share a stack
  via dedup; task A's trials mutate a `shared` service; task B's
  trials see the mutation. Prevention: same as above — `shared` is
  opt-in with explicit intent. Authors who declare `shared` accept
  the semantics.
- **Non-canonical YAML causing hash misses.** Two authors write
  semantically-identical-but-syntactically-different compose files;
  the runtime sees different hashes and fails to dedup.
  Prevention: the runtime canonicalises key order, strips comments
  and whitespace, and normalises quoting before hashing.
- **Cross-run assumption.** A user runs task A, then task B in a
  separate invocation, and expects B to see A's state. Prevention:
  the model is documented as within-run only, and there is no
  cross-run persistence surface exposed. Cross-run persistence is
  a separate mechanism, not a consequence of this model.
- **Reset-primitive failure.** A `reset` service's primitive fails
  mid-run. Prevention: primitive failures terminate the affected
  task's remaining trials with an explicit reason (analogous to
  `TerminationReason.PROVISION_ERROR`); the stack itself does not
  tear down, but the task no longer schedules trials against it.
- **Input-override typos.** A task overrides `postgress_version`
  (misspelt); the compose file's `${postgres_version}` binds to
  its default; the author expects a different postgres version and
  doesn't get one. Prevention: input names are validated against
  the compose file's declared `${...}` references at load time;
  unknown input names warn.

## What the model deliberately isn't

- **A pack-level `project.yaml` or `recipe.yaml` file.** The
  authoring model stays flat; every task declares its own
  `environment_manifest`. Sharing emerges from equivalence, not
  from inheritance.
- **A deep-merge across manifests.** No implicit merging anywhere.
- **An inheritance hierarchy.** The mechanism is content-address
  equivalence, not "children inherit from parent."
- **Cross-run stack persistence.** All sharing is within a single
  `tolokaforge run` invocation. Cross-run persistence is a
  separate arc.
- **Cross-pack sharing.** Tasks in different packs never share
  stacks even when hashes match. Runs are scoped to a set of
  packs; sharing is scoped to a run.
- **A plugin registry for service types or reset primitives.** New
  primitives extend the enum via a schema change in tolokaforge
  itself. Third-party extensibility is deferred until a real
  third party asks.
- **In-place editing of compose files.** Compose files are read at
  load time, input-substituted in memory, and hashed. On-disk
  files are never mutated.

---

*Return to [`docs/architecture/`](.) for the ADR index and the
canonical runtime-backends documentation.*
