# Multi-service `postgres` example — Enterprise Support Triage

The **third and most realistic** example in the multi-service series.
Unlike its siblings (`multi_service`, `multi_service_advanced`) which
serve static JSON from nginx, this one runs a genuine three-tier stack:

```
agent → PostgREST (HTTP API) → postgres:16 (real database)
```

The task pack ships **no application code** — PostgREST introspects the
postgres schema and generates REST endpoints automatically. All the task
authoring lives in one file: the SQL that defines the schema and seeds
the data (`app-db/init.sql`).

Case B in [ADR-0018](../../../docs/adr/0018-multi-container-under-shared-runtime.md).

## What this example demonstrates

- **A real database and a real API in the substrate.** `app-db` is
  postgres:16 with schema + fixtures seeded via
  docker-entrypoint-initdb.d. `app-service` is PostgREST auto-serving
  REST endpoints over that schema.
- **Multi-endpoint coordination.** The agent must call two endpoints
  (`/tickets` and `/customers`), join them on `customer_id`, apply two
  filters (`status == "open"` AND `tier == "enterprise"`), sort by
  priority, and take the top-3.
- **Distractors that make filters real signals.** The fixture includes
  priority-1 tickets in wrong tiers (Beta Retail = business, Halcyon
  Media = individual) and priority-1 tickets with wrong status (Acme
  Corp closed). Any filter the agent skips changes the top-3 —
  demonstrated by the `test_filters_actually_matter` unit test.
- **Zero application code shipped in the task pack.** Just SQL + a
  compose file. PostgREST handles the rest.

## Expected top-3

Filtering to `status == "open"` AND `customer.tier == "enterprise"`,
sorted by `priority` ascending:

| Rank | Customer         | Ticket priority | Subject |
|------|------------------|-----------------|---------|
| 1    | Acme Corp        | 1               | Login failures spike |
| 2    | Corex Systems    | 2               | Data export timing out |
| 3    | Enterprise Co    | 3               | SSO integration broken |

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service_postgres/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_postgres/run_config.yaml
```

First run is ~30-45s slower than the sibling examples — postgres has to
initialise and seed, and PostgREST has to introspect the schema. On
subsequent runs the postgres image pull is cached; only the seed step
runs from scratch (compose volumes are removed at teardown).

## Layout

```
examples/native/multi_service_postgres/
├── run_config.yaml                # sonnet-4-6 agent + user
├── README.md                      # this file
└── dataset/tasks/multi_service/
    └── support_triage_01/
        ├── task.yaml              # env_manifest + tool declarations
        ├── environment.compose.yaml # 4-service compose
        ├── grading.yaml           # state checks on the written report
        └── app-db/
            └── init.sql           # schema + seed data (loaded by
                                   # postgres's docker-entrypoint-initdb.d)
```

## Design notes

- **PostgREST config.** PostgREST connects as the `authenticator` role
  (the postgres image's `POSTGRES_USER`) and SET-ROLEs to `web_anon` for
  unauthenticated requests. `web_anon` has SELECT on `api.customers`
  and `api.tickets` only — read-only exposure, no writes.
- **The `db-service` engine backend is unchanged.** `db-service` (in-
  memory sqlite) still stores per-trial state and grading state; it's
  independent of `app-db` (the task's own postgres). This example
  demonstrates that a task's application backend can be a completely
  different technology from the engine's own state backend.
- **`isolation: shared_ok`.** The task's grading inspects the agent's
  written output only; the read-only API doesn't mutate state across
  trials. If the task wrote through PostgREST (which permissions would
  need to allow), `per_trial` isolation would be appropriate to avoid
  cross-trial contamination.
- **Both API endpoints use pinned tags.** `postgrest/postgrest:v12.2.0`
  and `postgres:16` are pinned per the manifest validator's
  non-floating-tag rule.

## Related

- [`../multi_service/README.md`](../multi_service/README.md) — basic Case
  B (nginx + static JSON)
- [`../multi_service_advanced/README.md`](../multi_service_advanced/README.md) —
  multi-endpoint join over two nginx services
- [ADR-0018](../../../docs/adr/0018-multi-container-under-shared-runtime.md) — case matrix + sequence diagrams
