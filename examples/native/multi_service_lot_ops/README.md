# Multi-service `lot_ops` — substrate-state grading

The multi-service example that grades a **real database mutation against the
substrate**. A shop-floor operator reports a contamination hit on a production
lot; the agent opens a corrective-action record over a REST API, and grading
verifies the record by reading the postgres table **directly** — not the
agent's own written file, and not the same API the agent just wrote through.

```
agent → FastAPI (app-service:8000) → postgres:16 (app-db)
                                          ▲
                       grading (read-only `grader` role) ┘
```

## What this example demonstrates

- **Substrate-state grading (`state_checks.db_probes`).** Grading connects to
  `app-db` with a task-local DSN, runs an author-written read-only `SELECT`
  against `corrective_actions`, and applies JSONPath assertions to the rows.
  This is an **independent oracle**: it reads the database through a dedicated
  `grader` role that holds `GRANT SELECT` only, so a bug in the agent's API
  path cannot mask a grading miss, and grading can never mutate the substrate.
  See [`docs/GRADING.md`](../../../docs/GRADING.md) § Substrate Grading.
- **Three-way weighted grading.** The final score combines all three grading
  families, each measuring a different thing:

  | Component | Weight | What it checks |
  |---|---|---|
  | `state_checks.db_probes` | 0.5 | the corrective action landed in postgres with the right reason code and `status='open'` |
  | `transcript_rules` | 0.2 | the agent both **read** (`http_request` `GET`) and **mutated** (`http_request` `POST`), within the turn budget |
  | `llm_judge` | 0.3 | the written completion report names the lot, states the reason, and reads as an operator-facing note |

- **A real application service, no custom image.** `app-service` is a small
  FastAPI app (~20 endpoints) run straight from `tolokaforge-runner:local` —
  that image already bundles `fastapi`, `uvicorn`, and `asyncpg`, so the pack
  ships **no Dockerfile**. A compose `command:` override boots `uvicorn`
  against the bind-mounted `app/main.py`; the runner image has no `ENTRYPOINT`,
  so the override takes effect.
- **Distinct method-tagged tool calls.** The agent uses the `http_request`
  builtin (restricted to `app-service:8000`), so its reads and mutations carry
  a `method` argument that `transcript_rules.required_actions` matches on
  directly — cleaner than opaque `bash` invocations.

## The scenario

Lot `LOT-1007` (`lot_id` 7, a batch of sterile vials) came back from QC with a
contamination hit. The agent must:

1. Look up the reason-code catalog (`GET /reason-codes`) and find the
   contamination code (`CAPA-01`).
2. Open a corrective action against lot 7 with that reason code and a note
   (`POST /lots/7/corrective-actions`).
3. Write a short completion report to `submissions/` for the shift lead.

The `corrective_actions` table ships **empty** — the agent's `POST` is the only
thing that can populate it, so grading passing means the mutation really landed.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service_lot_ops/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_lot_ops/run_config.yaml
```

Needs a running Docker daemon and `OPENROUTER_API_KEY` in `.env`. The first run
is slower while postgres initialises and seeds; the runner container joins the
task's docker network, so at grade time it reaches `app-db:5432` directly.

## Layout

```
examples/native/multi_service_lot_ops/
├── run_config.yaml               # haiku agent + user, sonnet judge
├── project.yaml                  # discovery glob + native defaults
├── README.md                     # this file
├── shared/
│   ├── environment.compose.yaml  # 4-service compose (all isolation: shared)
│   ├── app/
│   │   └── main.py               # FastAPI lot-operations API (~20 endpoints)
│   └── app-db/
│       └── init.sql              # schema + seed + app/grader roles
└── dataset/tasks/lot_ops_01/
    ├── task.yaml                 # env manifest + http_request/write_file tools
    └── grading.yaml              # db_probes + transcript_rules + llm_judge
```

## Design notes

- **Two roles, least privilege.** `init.sql` creates the postgres database as
  the read/write `app` role (the FastAPI service connects as this) and a
  separate `grader` role with `GRANT SELECT` only. The db_probe DSN in
  `grading.yaml` authenticates as `grader`, so the oracle is read-only by
  construction. Mirrors the `web_anon` / `authenticator` split in the sibling
  PostgREST packs.
- **All four services `isolation: shared`.** This is the shared-runtime
  reference: the stack is materialised once and shared across trials, so
  `app-db` is up throughout the run and reachable at grade time. The task uses
  a single trial (`repeats: 1`) and does not require a per-trial baseline, so
  no reset recipe is involved.
- **Task-local throwaway credentials.** The DSN in `grading.yaml` and the
  compose passwords are disposable, task-scoped credentials for a local
  container — the same posture as the plaintext compose credentials in the
  sibling packs. Real secrets always go through `SecretManager`.
- **Pinned images only.** `postgres:16`, `tolokaforge-runner:local`, and
  `tolokaforge-db-service:local`. The `:local` alias is valid on a
  non-`runner` service — the manifest validator rejects only floating tags.

## Related

- [`docs/MULTI_CONTAINER_GUIDE.md`](../../../docs/MULTI_CONTAINER_GUIDE.md)
  — authoring guide for multi-container tasks
- [`docs/GRADING.md`](../../../docs/GRADING.md) — grading families, including
  `state_checks.db_probes`
- [`../multi_service_postgres/README.md`](../multi_service_postgres/README.md)
  — a three-tier stack (PostgREST + postgres) that grades the agent's file
- [ADR-0018](../../../docs/adr/0018-multi-container-under-shared-runtime.md)
  — multi-container case matrix
