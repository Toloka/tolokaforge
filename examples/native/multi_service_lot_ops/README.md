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
  | `trace_checks` | 0.2 | the posted values were **obtained** rather than guessed, and the action was opened exactly once |
  | `llm_judge` | 0.3 | the written completion report names the lot, states the reason, and reads as an operator-facing note |

- **A real application service, no custom image.** `app-service` is a small
  FastAPI app (~20 endpoints) run straight from `tolokaforge-runner:local` —
  that image already bundles `fastapi`, `uvicorn`, and `asyncpg`, so the pack
  ships **no Dockerfile**. A compose `command:` override boots `uvicorn`
  against the bind-mounted `app/main.py`; the runner image has no `ENTRYPOINT`,
  so the override takes effect.
- **Correlated arguments: grading how a value was obtained.** The db_probe reads
  the `corrective_actions` row that exists; it cannot say where the reason code in
  it came from, and the task's own guidance says not to guess it. Two
  `trace_checks` constraints bind a value out of the `POST` and require it to have
  come from something the agent read:

  | constraint | what it binds | the wrong process it catches |
  |---|---|---|
  | `the_reason_code_posted_was_read_from_the_catalog` | `args.json.reason_code` off the POST | the code was written from memory, or fabricated, rather than read out of `GET /reason-codes` |
  | `the_lot_was_read_before_the_action_was_opened` | the lot URL off the POST, by regex capture | the action was opened against a lot the agent never read |
  | `exactly_one_corrective_action_was_opened` | — (`count { max: 1 }`, `severity: gate`) | the action is double-posted, leaving a duplicate to reconcile |

  Because both bind rather than hard-code, neither names a reason code or a lot
  number: a new code in the catalog, or a different lot, needs no edit here. The lot
  correlation captures the **whole** `http://…/lots/<id>` prefix on purpose — a bound
  `"7"` is a substring of `.../lots/1007`, so an agent that read the lot *code* as
  though it were the id would pass a bare-id binding. Both binders draw from the POST,
  so an agent that never opens the action binds nothing and the default `on_unbound`
  charges it: strictly stronger than asking that *a* POST happened. See
  [`docs/GRADING.md`](../../../docs/GRADING.md) § Correlating arguments across
  matchers.

- **Distinct method-tagged tool calls.** The agent uses the `http_request`
  builtin (restricted to `app-service:8000`), so its reads and mutations carry
  `method` and `url` arguments a matcher addresses directly — cleaner than opaque
  `bash` invocations.

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
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_lot_ops/run_configs/dev.yaml
```

Needs a running Docker daemon and `OPENROUTER_API_KEY` in `.env`. The first run
is slower while postgres initialises and seeds; the runner container joins the
task's docker network, so at grade time it reaches `app-db:5432` directly.

### Regenerating the judge-labelled corpus

`run_configs/corpus_generation_haiku.yaml` and `corpus_generation_gpt_4o_mini.yaml`
are the two arms behind
[`tests/data/migration_corpora/lot_ops_names_lot/`](../../../docs/RUBRIC_MIGRATION.md#the-committed-corpora),
the corpus this pack's `names_lot` candidacy is measured against. They are
identical but for the agent model — the arms' only independent variable — at five
repeats each, and neither prompts for the behaviour the candidate constraint
measures.

```bash
scripts/with_env.sh uv run tolokaforge run \
  --config examples/native/multi_service_lot_ops/run_configs/corpus_generation_haiku.yaml \
  --cost-limit 1.50
scripts/with_env.sh uv run tolokaforge run \
  --config examples/native/multi_service_lot_ops/run_configs/corpus_generation_gpt_4o_mini.yaml \
  --cost-limit 1.50
tolokaforge curate --criterion names_lot --source results/<haiku-run> \
  --source results/<gpt-4o-mini-run> \
  --into tests/data/migration_corpora/lot_ops_names_lot --replace
```

An arm is a whole config file because `RunConfig.models` holds one model per role
and `tolokaforge run` has no agent-model override.

## Layout

```
examples/native/multi_service_lot_ops/
├── run_configs/
│   ├── dev.yaml                  # haiku agent + user, sonnet judge
│   ├── corpus_generation_haiku.yaml         # corpus arm one: haiku agent
│   └── corpus_generation_gpt_4o_mini.yaml   # corpus arm two: gpt-4o-mini agent
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
    └── grading.yaml              # db_probes + trace_checks + llm_judge
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
  `app-db` is up throughout the run and reachable at grade time. There is no
  reset recipe, so `corrective_actions` accumulates across the trials of one
  run: `dev.yaml` takes a single trial (`repeats: 1`), while under the
  five-repeat corpus arms every trial after the first sees the rows its
  predecessors wrote and its `row_count` db_probe assertion fails. The trace
  constraints and the judge's rubric read one trial's own transcript and report,
  so both are unaffected — which is why the corpus those arms produce measures
  `names_lot` cleanly.
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
