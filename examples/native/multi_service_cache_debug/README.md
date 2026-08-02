# Multi-service `cache_debug` — redis_dump reset + multi-path cache-invalidation diagnosis

A **runnable** debugging example that exercises the `redis_dump` reset recipe
end-to-end through `tolokaforge run`. A real `redis` service is labelled
`isolation: reset` and bound to an RDB seed carrying **poisoned** cache state;
the per-trial backend restores that seed to a fresh stack at the start of every
trial. The agent is asked to diagnose why the orders API serves stale data, and
writes a root-cause note naming the cache-invalidation bug.

It is also the reference for **multi-path deterministic grading**: two genuinely
alternative diagnostic routes, each scored as a whole, behind one shared gate.

```
                 ┌── orders-api:8000 ──┬──▶ redis:7-alpine (cache, reset per trial)
agent  ──http──▶ │                     └──▶ postgres:16   (app-db, source of truth)
                 └── cache-admin:8000 ─────▶ redis:7-alpine (read-only inspector)
```

## What this example demonstrates

- **The `redis_dump` reset recipe, end-to-end.** `redis` is labelled
  `isolation: reset` with `reset.seed: cache_poisoned` in `project.yaml`. The
  seed (`assets/cache_poisoned.rdb`, kind `redis_dump`) is registered under
  `assets.seeds` and restored by the per-trial backend right after the compose
  stack comes up — it copies `dump.rdb` into the redis container and restarts
  it, so redis reloads the poisoned snapshot. This is the runnable `redis_dump`
  reference (the `sql_dump` recipe's reference is `multi_service_postgres_reset`).

- **A real cache-invalidation bug, observable from turn 1.** The seed pre-loads
  `order:4021` in redis with a **stale** value (`status: "processing"`), while
  postgres holds the **fresh** truth (`status: "shipped"`).

  | read | source | value |
  |---|---|---|
  | `GET orders-api:8000/orders/4021` | cache-first (redis) | `processing` (stale) |
  | `GET orders-api:8000/orders/4021/source` | postgres directly | `shipped` (fresh) |
  | `GET cache-admin:8000/cache/order:4021` | raw cached value | `processing` (stale) |

  The bug is real in code, not staged: `orders-api` is cache-first
  (`GET /orders/{id}` returns the cached value when present), and its
  `POST /orders/{id}` status update writes postgres but **never invalidates**
  the `order:<id>` key — so cache-first reads keep serving the stale value. That
  `POST` is reachable with the agent's own `http_request` tool, which is what the
  grading gate below is for: the task is to diagnose the staleness, not to paper
  over it with a status update.

- **Two genuinely alternative diagnostic routes, graded deterministically.** The
  rubric reference names two comparisons as locating the bug, and the task's
  guidance asks the agent to compare the layers without preferring a pair, so
  `trace_checks.alternatives` declares both and the component score is the better
  route's. An agent that compares the served read against the source of truth and
  never opens the cache inspector scores in full, and so does one that reads the
  cached value and compares it against either orders-api endpoint.

  | route | the comparison it grades |
  |---|---|
  | `divergence_between_the_api_layers` | `GET /orders/4021` against `GET /orders/4021/source` |
  | `divergence_against_the_cache` | `GET cache-admin:8000/cache/order:4021` against either orders-api read |

  Each route asks two questions — were both sides read, and did the reads that
  happened happen before the note — so an agent that starts down both routes and
  completes neither has observed no divergence and scores the better of two
  incomplete routes, not the sum of two halves. The ordering question carries
  `on_missing: pass` and is vacuous where neither read happened, so the presence
  question alone charges that case and a missing read costs the agent once.

- **A shared gate: the diagnose-only task cannot be passed by mutating.**
  `POST /orders/4021` exists and updates the order. An agent that "fixes" the
  symptom that way has diagnosed nothing and has mutated data it was never asked
  to touch, so `no_status_was_written` carries `severity: gate` — it is excluded
  from the weighted average and a violation takes the component to `0.0` and fails
  the trial outright, whatever the note said. The gate sits in the **shared**
  `constraints`, not inside a route: "do not mutate on a diagnose-only task" holds
  whichever route the agent took, and a gate inside a path is consulted only on the
  route that won, so an agent could escape it by winning on the other one.

- **A three-way weighted combine, no single decisive check.** The diagnosis is
  natural language, so `llm_judge` is the primary signal and deliberately the
  dominant one:

  | Component | Weight | What it checks |
  |---|---|---|
  | `llm_judge` | 0.50 | the note identifies the stale-cache / missing-invalidation bug, explains the mechanism, and avoids a false fix |
  | `state_checks` | 0.25 | the written note names the cache-invalidation concept (substring `invalidat`) |
  | `trace_checks` | 0.25 | one of the two comparisons was completed before the note was written, and no status update was posted |

  The two routes are not equally probative — the cache inspector shows the stale
  value itself, while the served-vs-source comparison shows only that the read path
  serves something the database disagrees with — so the deterministic components
  must not be able to carry a weak diagnosis. At `0.25` each they sum to `0.5`,
  below the `0.6` threshold. `0.5` judge + `0.25` note clears it, so a correct
  diagnosis is not sunk by the route it chose.

- **The failure → #418 capture path.** A completed-but-red trial captures every
  declared service's logs to `results/<run>/trials/<task>/<idx>/services/<svc>.log`.
  `redis.log` carries the RDB-load signature (`Loading RDB` / `Done loading RDB`
  / `DB loaded from disk`) from the recipe's restart — provable evidence the
  `redis_dump` recipe fired. `run_configs/dev.yaml` sets
  `compute.capture_logs_on_success: true` so these logs land on a green run too.

## Why per-trial (not shared)

`redis` is `reset` and the other services default to `ephemeral` (fresh per
trial). That mix makes the manifest's `requires_per_trial` true, so backend
selection routes the run onto the per-trial backend — the only backend that
applies reset recipes.

## Regenerating the seed

`assets/cache_poisoned.rdb` is a committed binary. To rebuild it (stdlib + a
running Docker daemon only):

```bash
uv run python examples/native/multi_service_cache_debug/assets/build_seed.py
uv run tolokaforge assets stamp examples/native/multi_service_cache_debug
```

`build_seed.py` boots a throwaway `redis:7-alpine`, SETs `order:4021` to the
stale value, `SAVE`s, and copies `dump.rdb` out. `assets stamp` recomputes the
`sha256` digest in `project.yaml` (verified at load) — never edit the digest by
hand.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service_cache_debug/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_cache_debug/run_configs/dev.yaml
```

Needs a running Docker daemon and `OPENROUTER_API_KEY` in `.env`. The first run
is slower while postgres and redis initialise; the runner container joins the
task's docker network, so the agent reaches each service by name.

## Layout

```
examples/native/multi_service_cache_debug/
├── project.yaml                        # assets.seeds (redis_dump) + default_environment (redis: reset)
├── run_configs/dev.yaml                # haiku agent + user, sonnet judge, per_trial, capture on success
├── README.md                           # this file
├── assets/
│   ├── cache_poisoned.rdb              # the redis_dump seed (order:4021 = stale "processing")
│   └── build_seed.py                   # regenerates the seed from a throwaway redis
├── shared/
│   ├── environment.compose.yaml        # 6-service compose (redis reset, rest ephemeral)
│   ├── _lib/redis_mini.py              # bundled read-only RESP client (GET/KEYS over a stdlib socket)
│   ├── orders-api/main.py              # FastAPI: cache-first read + source read + buggy status update
│   ├── cache-admin/main.py             # FastAPI: read-only cache inspector
│   └── app-db/init.sql                 # orders table + fresh-truth seed (order 4021 = shipped)
└── dataset/tasks/cache_debug/
    ├── task.yaml                       # inherits the project default_environment whole; no environment_manifest
    └── grading.yaml                    # three-way weighted: state_checks + trace_checks (two routes, one gate) + llm_judge
```

## Design notes

- **RESP over a stdlib socket, not a redis client.** The runner image ships no
  `redis-cli` and no `redis` Python client, so the FastAPI services read redis
  through `shared/_lib/redis_mini.py` — a minimal RESP reader over a raw socket
  exposing GET and KEYS only. It is **read-only by construction**: nothing in
  the pack writes redis at runtime; the poisoned state comes from the seed. This
  honours the "do not modify the runner image just for this pack" constraint.
- **No `db_probes`.** This pack grades a diagnosis *note*, not a substrate
  mutation, so postgres needs no read-only `grader` role — it is only the
  source-of-truth the stale-cache divergence is measured against. The
  substrate-oracle story is the flagship's (`multi_service_helpdesk_workflow`).
- **Capture on success.** `capture_logs_on_success: true` lands per-service logs
  on a green run, so `redis.log`'s RDB-load signature is available as a witness
  that the `redis_dump` recipe fired without forcing a red grade.
- **RDB-only redis.** `command: ["redis-server","--save","","--appendonly","no"]`
  disables background persistence, so the recipe's `dump.rdb` restore is the
  authoritative state — matching the recipe's own integration fixture.
- **Pinned images only.** `redis:7-alpine`, `postgres:16`; the engine images are
  `tolokaforge-runner:local` / `tolokaforge-db-service:local`.

## Related

- [`../multi_service_postgres_reset/README.md`](../multi_service_postgres_reset/README.md)
  — the `sql_dump` reset-recipe reference (postgres side).
- [`docs/RESET_RECIPES.md`](../../../docs/RESET_RECIPES.md)
  — the reset-recipe reference, including `redis_dump`.
- [`docs/GRADING.md`](../../../docs/GRADING.md#alternative-paths)
  — `alternatives`, `severity: gate`, and when a gate belongs in shared
  `constraints` rather than inside a route.
- [`docs/MULTI_CONTAINER_GUIDE.md`](../../../docs/MULTI_CONTAINER_GUIDE.md)
  — authoring guide for multi-container tasks.
```
