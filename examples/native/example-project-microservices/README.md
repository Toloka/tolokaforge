# example-project-microservices

**Spec-only walkthrough.** This task pack demonstrates the proposed
project-level environment manifest + per-task recipe overlays
described in
[`docs/architecture/proposals/PROJECT_MANIFEST_EXTENSION_PROPOSAL.md`](../../../docs/architecture/proposals/PROJECT_MANIFEST_EXTENSION_PROPOSAL.md).

The current TolokaForge loader does **not** understand `project.yaml`
or `recipe.yaml` yet — the files below are illustrative, not
compile-testable. The purpose is to make the semantics tangible for
review. Colleagues read this README alongside the design doc and
step through the merge that the loader would perform for each task.

## Layout

```
example-project-microservices/
├── project.yaml                       # Shared project config
├── environment/
│   └── project.compose.yaml           # 6-service base compose
├── profiles/
│   ├── metrics.compose.yaml           # prometheus + grafana
│   └── worker.compose.yaml            # background queue worker
├── run_config.yaml                    # unchanged from today's shape
├── README.md                          # this file
└── tasks/
    ├── api_endpoint_add/              # Pattern 1: no recipe
    ├── db_schema_migrate/             # Pattern 2: isolation modifier
    ├── isolated_data_experiment/      # Pattern 3: service replacement
    ├── observability_alert_config/    # Pattern 4: profile selection
    └── legacy_migration_bug/          # Pattern 5: full escape hatch
```

## The five recipe patterns, in order

### Pattern 1 — No recipe (`api_endpoint_add/`)

Task inherits the full project default. No `recipe.yaml` sibling.
Shortest possible task authoring — one `task.yaml` + one
`grading.yaml`, plus per-task fixtures. Every environmental concern
lives in `../../project.yaml`.

**Load-time merge:** loader binds the project inputs to their
defaults (`postgres_version: "16"`, `redis_included: true`,
`default_service_isolation: shared`), materialises the base compose
verbatim, activates zero profiles. The task sees all 6 services.

### Pattern 2 — Isolation modifier (`db_schema_migrate/`)

Task mutates the DB schema; needs per-trial DB reset. Recipe flips
`app-db` from the project's default `shared` isolation to `reset`,
naming the postgres template-DB primitive.

**Load-time merge:** same as Pattern 1, except when the runtime
looks up `services.app-db.isolation`, it sees `reset` instead of
`shared`. Between trials, the runtime invokes the
`postgres_template_db` primitive on that service — schema resets in
~200ms without recreating the postgres process.

**Read this to understand:** why "isolation is per-service, not
per-task" is a load-bearing property of the design.

### Pattern 3 — Full service replacement (`isolated_data_experiment/`)

Task needs an isolated postgres so its synthetic seed data doesn't
leak into other tasks. Recipe REPLACES the entire `app-db` service
definition, keeps the rest of the stack shared.

**Load-time merge:** loader replaces `services.app-db` in the merged
compose with the recipe's block (different image tag, different env
vars, `isolation: ephemeral`). Backend-api's `DATABASE_URL`
automatically rebinds to the new app-db by Docker DNS.

**Read this to understand:** why "replacement, not patching" is the
design principle. Reader sees exactly what runs by reading two
adjacent files.

### Pattern 4 — Profile selection (`observability_alert_config/`)

Task only cares about metrics; doesn't need the frontend or the
queue. Recipe activates the `metrics` profile (adds prometheus +
grafana) and drops `frontend` + `queue` from the base compose.

**Load-time merge:** loader materialises the base compose minus the
`dropped` services, then applies the `metrics` profile's compose
fragment as an overlay. Task sees: runner + db-service + app-db +
backend-api + prometheus + grafana.

**Read this to understand:** why "profile-based composition"
scales better than 20 per-task branches. Any task that wants
observability just activates the profile.

### Pattern 5 — Full escape hatch (`legacy_migration_bug/`)

Task's environment doesn't fit the project — legacy PHP + MySQL
stack, no relation to the microservices project. Task declares its
own `environment_manifest.compose_file` in `task.yaml` and BYPASSES
the project inheritance entirely.

**Load-time merge:** loader detects the task-level manifest and
skips the project layer for this task. Task runs against its own
compose file (legacy-dashboard + legacy-db + engine services). Rest
of the project is untouched.

**Read this to understand:** why backward compatibility is total.
Any existing task that already ships a task-level manifest today
continues to work when placed under a project — the project layer
is opt-in per task.

## Reading the merged manifests

For each of the four tasks that use the project (Patterns 1-4),
here's the effective `EnvironmentManifest` the loader would
produce at task-load time:

### `api_endpoint_add` (Pattern 1)

- Services: `runner`, `db-service`, `app-db`, `backend-api`,
  `frontend`, `queue`
- All services: `isolation: shared` (project default)
- Profiles active: none

### `db_schema_migrate` (Pattern 2)

- Services: same six as above
- `app-db.isolation`: `reset` (recipe override)
- `app-db.reset_primitive`: `postgres_template_db` (recipe override)
- Other services: `shared` (unchanged)
- Profiles active: none

### `isolated_data_experiment` (Pattern 3)

- Services: `runner`, `db-service`, `app-db-EXPERIMENT` (from recipe),
  `backend-api`, `frontend`, `queue`
- `app-db-EXPERIMENT`: `isolation: ephemeral`, task-local seed volumes
- Other services: `shared` (unchanged)
- Profiles active: none

### `observability_alert_config` (Pattern 4)

- Services: `runner`, `db-service`, `app-db`, `backend-api`,
  `prometheus`, `grafana` — **no** frontend, **no** queue (dropped)
- All services: `shared`
- Profiles active: `metrics`

### `legacy_migration_bug` (Pattern 5)

- Services: `runner`, `db-service`, `legacy-db`, `legacy-dashboard`
- Loader ignored `../../project.yaml` — used task-level
  `environment_manifest` verbatim.

## What to review

- **Is the recipe grammar simple enough?** Three operations (typed
  input override, profile activation/drop, service replacement) —
  no free-form patching.
- **Is the escape hatch clean enough?** Pattern 5 keeps 100%
  backward compatibility.
- **Would you rather see anything expressed differently?** File
  names, keywords in the schema, precedence rules — everything is
  up for feedback.

Discussion belongs in the design-doc thread; this pack is the
concrete artefact reviewers can point at.

## What's missing (deliberately)

- **No engine code changes.** The loader doesn't understand these
  files yet.
- **No fixtures.** Every task has only a `task.yaml` +
  `grading.yaml` (+ optional recipe/compose). Real fixtures would
  be added when the tasks become runnable.
- **No prometheus.yml.** Referenced by the `metrics` profile but
  not included — this is spec-only.

## Cross-references

- Design doc: [`../../../docs/architecture/proposals/PROJECT_MANIFEST_EXTENSION_PROPOSAL.md`](../../../docs/architecture/proposals/PROJECT_MANIFEST_EXTENSION_PROPOSAL.md)
- Existing multi-service examples for comparison:
  - [`../multi_service/`](../multi_service/) — smallest task-declared compose
  - [`../multi_service_postgres/`](../multi_service_postgres/) — the postgres pattern this project extends
