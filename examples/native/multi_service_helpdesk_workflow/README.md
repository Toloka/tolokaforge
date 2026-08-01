# Multi-service `helpdesk_workflow` — cross-service reasoning + policy correctness

The flagship multi-service example. A temperature-sensitive shipment is running
late and will arrive after the receiving dock closes. The agent must reconcile
data across **four** business services plus an in-container **policy corpus**,
work out the *one* resolution the after-hours policy actually permits, record it
as a CRM case, and annotate the delivery. Grading reads the postgres substrate
**directly** through a read-only role and checks **policy correctness** — the
value the policy selects, not merely that a well-formed row exists.

```
                 ┌── delivery-tracker:8000 ─┐
                 ├── product-catalog:8000  ─┤
agent  ──http──▶ ├── client-locations:8000 ─┤──▶ postgres:16 (app-db)
                 ├── crm:8000              ─┤          ▲
                 └── policy-search:8000 ───┘          │
                                    grading (read-only `grader` role) ┘
```

## What this example demonstrates

- **Cross-service reasoning graded on policy correctness.** Three resolution
  paths are superficially plausible; policy applied to the site's capabilities
  narrows to exactly one. An agent that picks a different path writes a
  well-formed CRM row and still grades down, because `state_checks.db_probes`
  asserts the *policy-correct value* (`reschedule`), not just row existence.

  | path | superficially attractive because… | ruled out by… |
  |---|---|---|
  | `temp_controlled_hold` | product is temp-sensitive → "just refrigerate it" | site `has_temp_storage = false` |
  | `specialist_handoff` | sounds premium / customer-friendly | site `has_specialist = false` |
  | `reschedule` | — | **the surviving, policy-correct path** |

  The `NORTHWIND` site ships with `has_temp_storage = false` and
  `has_specialist = false`, so both alternatives are excluded by policy applied
  to data — reasoning the agent must actually perform, not a keyword lookup.

- **In-container full-text policy search.** `policy-search` exposes the
  `policy_docs` corpus (a generated `tsvector` column + GIN index) via postgres
  `websearch_to_tsquery`. The decisive paragraph states that a temperature-
  sensitive after-hours shipment must be rescheduled when the site has neither
  cold storage nor a specialist; decoy paragraphs describe the other two paths
  in isolation so a keyword-only reader can be misled.

- **A discriminating three-way weighted combine.** The final score combines all
  three grading families, and the `state_checks` component alone is decisive:

  | Component | Weight | What it checks |
  |---|---|---|
  | `state_checks.db_probes` | 0.60 | the CRM case **and** the delivery annotation both carry `reschedule` for the right customer |
  | `trace_checks` | 0.25 | the query rode in the `POST /search` body, policy was read before the case was written, and the delivery was not annotated ahead of that policy read |
  | `llm_judge` | 0.15 | the CRM summary names the customer + delivery and justifies the reschedule from policy |

  Because `state_checks` carries 0.60 against a `0.6` pass threshold, a
  wrong-path run cannot pass even with full `trace_checks` + `llm_judge`
  credit — and a correct run still passes if the `llm_judge` errors and is
  dropped from the combine, since the two deterministic components are then the
  whole surviving weight.

- **The process is graded deterministically, not by rubric.** Every condition on
  *how* the agent worked is a `trace_checks` constraint, so the judge is left the
  one question only a reader can answer — whether the case note reads as a
  justified, customer-facing summary. Each constraint is written so a plausible
  wrong trajectory fails it and the other two pass: writing the CRM case before
  reading policy fails the ordering constraint alone, annotating the delivery
  first fails the scoped-absence constraint alone.

- **Five application services, no custom image.** Each service is a small
  FastAPI app run straight from `tolokaforge-runner:local` — that image already
  bundles `fastapi`, `uvicorn`, and `asyncpg`, so the pack ships **no
  Dockerfile**. A compose `command:` override boots `uvicorn` against each
  bind-mounted `main.py`.

## The scenario

Shipment `DLV-4021` (`delivery_id 4021`) for customer `NORTHWIND` is a
temperature-sensitive reagent kit (`RGT-COLD-12`). It was delayed; the new ETA
(20:00) lands after the site's staffed window closes (17:00). The agent must:

1. Read the delivery (`GET /deliveries/4021`), its product, and the Northwind
   site to establish that the shipment is temp-sensitive, arrives after hours,
   and the site has neither cold storage nor a specialist.
2. Search the policy corpus (`POST /search`) to find the after-hours rule.
3. Derive the only permitted path (`reschedule`), record it as a CRM case
   (`POST /cases`), and annotate the delivery (`PATCH /deliveries/4021`).

`crm_cases` ships **empty**, so the agent's `POST` is the only thing that can
populate it — grading passing means the mutation really landed with the
policy-correct value.

### Why policy search is `POST /search`

The query rides in the JSON body of a fixed-URL `POST /search`, not a
`GET /search?q=…` query string, so the URL is the same on every trial and the
query itself is an argument grading can read. The `policy_query_rides_in_the_body`
constraint asserts exactly that — the nested path `args.json.q` is non-empty on a
call to the fixed URL — which is what makes "the agent actually searched" a check
rather than an assumption:

```yaml
require:
  present:
    match:
      kind: tool_call
      tool: { equals: http_request }
      args:
        url: { equals: "http://policy-search:8000/search" }
        method: { equals: "POST" }
        json.q: { len_gt: 0 }
```

## The user-simulator persona pattern

This pack ships the reusable **specialised-persona** shape for the LLM user
simulator: a named character with a concrete backstory, the facts they know,
reveal-on-ask rules, a "never name the solution" rule, a natural opening, and a
`###STOP###` exit. The point is a user who is *cooperative but not a cheat
sheet* — they confirm the site has no cold storage or specialist **only when
asked**, and never name a resolution path or quote policy. That forces the agent
to do the reconciling. See `dataset/tasks/helpdesk_01/task.yaml` →
`actors.user.backstory`, and `docs/TASKS.md` § User Simulator.

## Isolation: the explicit ephemeral substrate

The postgres `app-db` is declared **`isolation: ephemeral`** explicitly; the
five stateless services are `shared`. Because at least one service is
non-`shared`, the whole run routes to the **per-trial backend**, which
materialises the entire compose stack fresh per trial — so each trial gets a
clean, freshly seeded postgres (reference data reseeded by `init.sql`,
`crm_cases` empty). This is the explicit form of the per-trial default and the
correct substrate posture for a mutation task: it keeps trials independent if
`repeats` is raised. With `repeats: 1` it is a single materialisation. The
stateless API services are labelled `shared` to document that they hold no
cross-trial state the task depends on.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service_helpdesk_workflow/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_helpdesk_workflow/run_configs/dev.yaml
```

Needs a running Docker daemon and `OPENROUTER_API_KEY` in `.env`. The first run
is slower while postgres initialises and seeds; the runner container joins the
task's docker network, so the agent reaches each service by name (e.g.
`policy-search:8000`) and grading reaches `app-db:5432` directly.

## Layout

```
examples/native/multi_service_helpdesk_workflow/
├── run_configs/dev.yaml          # haiku agent + user, sonnet judge, per_trial runtime
├── project.yaml                  # discovery glob + native defaults
├── README.md                     # this file
├── shared/
│   ├── environment.compose.yaml  # 8-service compose (app-db ephemeral, rest shared)
│   ├── delivery-tracker/main.py  # FastAPI: read + PATCH the delivery
│   ├── product-catalog/main.py   # FastAPI: product temp-sensitivity + hold limits
│   ├── client-locations/main.py  # FastAPI: site staffed window + capabilities
│   ├── crm/main.py               # FastAPI: create + read cases
│   ├── policy-search/main.py     # FastAPI: postgres-FTS policy search (POST /search)
│   └── app-db/
│       └── init.sql              # schema + seed + policy corpus + app/grader roles
└── dataset/tasks/helpdesk_01/
    ├── task.yaml                 # env manifest + http_request/write_file tools + persona
    └── grading.yaml              # db_probes + trace_checks + llm_judge
```

## Design notes

- **Two roles, least privilege.** `init.sql` creates the database as the
  read/write `app` role (every FastAPI service connects as this) and a separate
  `grader` role with `GRANT SELECT` only. The db_probe DSN authenticates as
  `grader`, so the oracle is read-only by construction and can never mutate the
  substrate.
- **`db_probes` encode policy correctness.** The probes assert
  `resolution_path = reschedule` — the value policy selects — in both
  `crm_cases` and `deliveries`, not just that a row exists. See
  [`docs/GRADING.md`](../../../docs/GRADING.md) § Substrate Grading.
- **The turn budget is `task.yaml`'s, not grading's.** `max_turns: 18` caps the
  loop, so a nineteenth assistant turn cannot be produced and a grading check on
  the count could never fail. The budget is enforced where it binds.
- **Task-local throwaway credentials.** The DSN in `grading.yaml` and the
  compose passwords are disposable, task-scoped credentials for a local
  container. Real secrets always go through `SecretManager`.
- **Pinned images only.** `postgres:16`, `tolokaforge-runner:local`,
  `tolokaforge-db-service:local`.

## Related

- [`docs/MULTI_CONTAINER_GUIDE.md`](../../../docs/MULTI_CONTAINER_GUIDE.md)
  — authoring guide for multi-container tasks
- [`docs/GRADING.md`](../../../docs/GRADING.md) — grading families, including
  `state_checks.db_probes` and the
  [`trace_checks`](../../../docs/GRADING.md#trace-checks) vocabulary this pack's
  constraints are written in
- [`docs/TASKS.md`](../../../docs/TASKS.md) — task authoring, including the
  user-simulator persona pattern
- [`../multi_service_lot_ops/README.md`](../multi_service_lot_ops/README.md)
  — the single-service substrate-grading sibling this pack extends
