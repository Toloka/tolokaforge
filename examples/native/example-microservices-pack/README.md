# example-microservices-pack

Task pack demonstrating the **Project** as the top-level abstraction:
a `project.yaml` at the pack root declares the shared
`default_environment` + `task_defaults` + `tasks.discovery` (plus
future-reserved sections for compute/storage/observability/orchestration).
Six tasks show the full inheritance + override patterns. Read
alongside [`docs/architecture/PROJECTS.md`](../../../docs/architecture/PROJECTS.md).

The Project model lands in v0.11.0 (see
[`ROADMAP.md`](../../../docs/architecture/ROADMAP.md)); this pack is
the canonical worked example authors reference when they lay out
their own multi-task packs.

## Layout

```
example-microservices-pack/
├── project.yaml                       # top-level Project spec
├── shared/
│   └── environment.compose.yaml       # base 4-service stack referenced by project.default_environment
├── run_config.yaml
├── README.md                          # this file
└── tasks/
    ├── api_endpoint_add/              # full inheritance — no environment_manifest in task.yaml
    ├── db_query_tuning/               # full inheritance — same resolved manifest as above
    ├── postgres_upgrade_test/         # partial override — inputs.postgres_version only
    ├── schema_isolation_migration/    # full override — task-local compose_file
    ├── agent_flow_setup/              # full inheritance — sequence part 1
    └── agent_flow_verify/             # full inheritance — sequence part 2
```

## The Project

Open [`project.yaml`](project.yaml) first. It declares:

- `name`, `version`, `description` — project identity.
- `default_environment` — the shared env manifest every task inherits
  by default (compose file, runner service, typed inputs).
- `task_defaults` — shared task-scoped fields (adapter, grading
  defaults) inherited by every task.
- `tasks.discovery.glob` — how the loader finds the tasks.
- `compute`, `storage`, `observability`, `orchestration` —
  future-reserved sections declared in v0.11.0 but not yet consumed
  by the runtime. Packs can start expressing intent today; the
  transition to full support is a runtime-side change.

## The tasks, by pattern

### Full inheritance (no `environment_manifest` block)

`api_endpoint_add`, `db_query_tuning`, `agent_flow_setup`,
`agent_flow_verify` each ship a `task.yaml` with only `task_id` and
`description`. Their resolved environment is exactly the project's
`default_environment`. All four resolve to byte-identical manifests
and share **stack A** — one running postgres + backend + runner + db-
service, referenced by four tasks.

Reading each `task.yaml` side-by-side makes the inheritance
concrete: there is no environment declaration at all, and yet the
task runs against the shared stack because that's the project
default.

### Partial override — vary one input

[`postgres_upgrade_test/task.yaml`](tasks/postgres_upgrade_test/task.yaml)
declares:

```yaml
environment_manifest:
  inputs:
    postgres_version: "17"
```

Everything else — `compose_file`, `runner_service`, other inputs —
inherits from the project. Deep-typed merge: only the one input
value differs. The compose file's `${postgres_version:-16}` binds
to `"17"`; the resolved manifest is distinct from the project
default; the task gets its own stack (**stack B**). Adding a fourth
task with the same override would automatically share stack B.

### Full override — task-local compose file

[`schema_isolation_migration/task.yaml`](tasks/schema_isolation_migration/task.yaml)
declares:

```yaml
environment_manifest:
  compose_file: "./environment.compose.yaml"
  runner_service: "runner"
```

The task ships its own compose file
([`schema_isolation_migration/environment.compose.yaml`](tasks/schema_isolation_migration/environment.compose.yaml))
with an isolated postgres. The project's
`default_environment.compose_file` is fully replaced. This task
runs on **stack C** and doesn't share with any other task.

Use this pattern when a task genuinely needs a bespoke environment
that doesn't compose cleanly with the project default. Bumping one
input is a partial override; needing a different compose topology
is a full override.

## Semantic sharing — no separate mechanism required

`agent_flow_setup` writes rows to postgres; `agent_flow_verify` is
graded on reading those rows. Both tasks fully inherit the project
default (no `environment_manifest` block on either), so both
resolve to the same manifest, both share stack A, and postgres
persists across their trials (as declared by
`services.postgres.isolation: "shared"` in
`shared/environment.compose.yaml`).

**Caveat.** This only works when both tasks run in the same
`tolokaforge run` invocation and the shared service is declared
`shared`. If postgres were `reset`, the reset primitive would fire
between every trial, wiping A's writes before B reads them. The
compose file's per-service isolation labels encode the sharing
semantics.

## The resolved hash table

Approximate — the actual hash function canonicalises YAML before
hashing.

| Task | Inheritance | `postgres_version` | Resulting stack |
|---|---|---|---|
| `api_endpoint_add` | full inheritance | `16` (project default) | **A** |
| `db_query_tuning` | full inheritance | `16` (project default) | **A** (shared) |
| `postgres_upgrade_test` | partial override — `inputs` | `17` | **B** |
| `schema_isolation_migration` | full override — `compose_file` | `16` (task-local default) | **C** |
| `agent_flow_setup` | full inheritance | `16` (project default) | **A** (shared) |
| `agent_flow_verify` | full inheritance | `16` (project default) | **A** (shared) |

Six tasks → three stacks. Four tasks share stack **A**, one task
each has stack **B** and stack **C**.

## What to review

- **Is the Project schema shape right?** `default_environment`,
  `task_defaults`, `tasks`, plus future-reserved sections
  (`compute`, `storage`, `observability`, `orchestration`) — is
  this the right partitioning, or does it miss a concern?
- **Is the override grammar acceptable?** Task-level
  `environment_manifest` deep-merges into `default_environment`;
  a task changes `compose_file` for full override, a subset of
  `inputs` for partial. Is that clear enough?
- **Are the future-reserved sections carved correctly?** In
  particular, is `compute` the right home for both provider
  selection and resource limits, or should those be separate
  sections?
- **Missing patterns?** Anything the six tasks don't demonstrate
  that a reviewer thinks a real customer scenario would need?

## What's missing (deliberately)

- **No engine implementation.** The v0.11.0 loader will read this
  pack; today's loader does not yet understand `project.yaml`. This
  pack is the specification target, not a runnable pack.
- **No fixtures.** Every task has only a `task.yaml` +
  `grading.yaml` (+ optional task-local compose). Real fixtures
  are added when the tasks become runnable.
- **No populated observability/storage sub-sections.** The
  `project.yaml` shows the shape of these sections but keeps them
  minimal — the sections are declared, not exercised.

## Cross-references

- Architecture doc: [`../../../docs/architecture/PROJECTS.md`](../../../docs/architecture/PROJECTS.md)
- Runtime backends: [`../../../docs/architecture/RUNTIME_BACKENDS.md`](../../../docs/architecture/RUNTIME_BACKENDS.md)
- Roadmap: [`../../../docs/architecture/ROADMAP.md`](../../../docs/architecture/ROADMAP.md)
- Existing multi-service examples for comparison:
  - [`../multi_service/`](../multi_service/) — smallest task-declared compose
  - [`../multi_service_postgres/`](../multi_service_postgres/) — the postgres pattern this pack extends
  - [`../native_shared_domain/`](../native_shared_domain/) — the existing per-category shared-config precedent
