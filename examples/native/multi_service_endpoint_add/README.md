# Multi-service `endpoint_add` — filesystem_dir reset + test-execution grading

A **runnable** auto-dev example that exercises the `filesystem_dir` reset recipe
end-to-end through `tolokaforge run`. A pristine FastAPI source tree is restored
into a shared volume at the start of every trial; the agent reads the code, adds
a missing `GET /orders/{id}/summary` endpoint, and triggers the **real** test
suite over HTTP. The suite's PASS/FAIL result is the deterministic floor of the
grade.

```
   runner:/work ──┐                          ┌── testrunner:/workspace
                  ├── source (named volume) ──┤
   agent edits ───┘   (filesystem_dir seed)   └── POST /run-tests → unittest
                                                        │
                                                        └──▶ app-db:16 (orders + customers)
```

## What this example demonstrates

- **The `filesystem_dir` reset recipe, end-to-end.** `testrunner` is labelled
  `isolation: reset` with `reset.seed: pristine_source` in `project.yaml`. The
  seed (`assets/source/`, kind `filesystem_dir`) is a **directory tree**,
  registered under `assets.seeds` and digested by a deterministic tree hash
  (stamped by `tolokaforge assets stamp`, verified at load). The per-trial
  backend restores it into the `source` volume right after the stack comes up,
  so `repeats: 2` starts each trial from clean source. This is the runnable
  `filesystem_dir` reference (the `sql_dump` reference is
  `multi_service_postgres_reset`; the `redis_dump` reference is
  `multi_service_cache_debug`).

- **A shared volume bridges the agent's `/work` and the reset target.** The
  agent's file tools operate on the runner container's `/work`, while the
  `filesystem_dir` recipe restores a *service* container's `/workspace`. The
  named `source` volume is mounted into **both** — `runner:/work` and
  `testrunner:/workspace` — so the seed lands in the agent's workspace, the
  agent's edits are visible to the test-runner, and the marker the test-runner
  writes syncs back into logical filesystem state for grading.

- **A real failing test, from turn 1.** The seeded `tests/test_summary.py`
  demands `GET /orders/{id}/summary` (an order joined with its customer). The
  pristine `app.py` has the three sibling endpoints (`GET /orders`,
  `GET /orders/{id}`, `GET /customers/{id}`) but not the summary, so the suite is
  red until the agent adds it. `tests/test_existing.py` guards the three existing
  endpoints against regressions.

- **The real test result is the grading floor.** `testrunner`'s
  `POST /run-tests` runs `unittest discover` against the agent's edited source
  and writes `/workspace/test_result.txt` = `PASS` (all tests green) or `FAIL`.
  That marker syncs to `/env/fs/agent-visible/test_result.txt`, which
  `state_checks` reads:

  | Component | Weight | What it checks |
  |---|---|---|
  | `state_checks` | 0.50 | the real suite passed — `test_result.txt` contains `PASS` |
  | `transcript_rules` | 0.20 | the agent edited code (`write_file`) and ran the suite (`http_request` → `/run-tests`), within 25 turns |
  | `llm_judge` | 0.30 | code quality: correct join, follows existing patterns, no unused imports or debug prints |

  Weight `0.5` on `state_checks` is **decisive**: `transcript_rules` +
  `llm_judge` sum to `0.5 < 0.6`, so a run whose tests do not pass cannot reach
  the `0.6` threshold.

- **The unittest runner (not pytest).** The `tolokaforge-runner:local` image
  ships no `pytest`, so the suite uses the stdlib `unittest` runner with
  Starlette's `TestClient` (carried by `fastapi`/`httpx`). The tests run
  in-process against `app-db` over asyncpg — no decorative live app server.

## Why per-trial (not shared)

`testrunner` is `reset` and the other services default to `ephemeral` (fresh per
trial). That mix makes the manifest's `requires_per_trial` true, so backend
selection routes the run onto the per-trial backend — the only backend that
applies reset recipes.

## Agent-attested freshness

`state_checks` reads the marker from the agent's **last** `/run-tests` call. An
edit made after the final run would leave a stale marker, so the task guidance
and a `transcript_rules` action both push the agent to re-run the suite after its
final edit, and the `llm_judge` reviews the final synced `app.py`. This is
inherent to agent-driven verification and acceptable for an example.

## Regenerating the seed

`assets/source/` is the pristine substrate — the **only** copy of the source
tree. After editing it, re-stamp the tree digest (never edit the digest by hand):

```bash
uv run tolokaforge assets stamp examples/native/multi_service_endpoint_add
uv run tolokaforge assets stamp examples/native/multi_service_endpoint_add --check
```

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/multi_service_endpoint_add/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_endpoint_add/run_configs/dev.yaml
```

Needs a running Docker daemon and `OPENROUTER_API_KEY` in `.env`. The first run
is slower while postgres initialises; the runner container joins the task's
docker network, so the agent reaches `testrunner` by name.

## Layout

```
examples/native/multi_service_endpoint_add/
├── project.yaml                        # assets.seeds (filesystem_dir) + default_environment (testrunner: reset)
├── run_configs/dev.yaml                # haiku agent + user, sonnet judge, per_trial, repeats: 2
├── README.md                           # this file
├── assets/
│   └── source/                         # the filesystem_dir seed — pristine FastAPI source (the ONLY copy)
│       ├── app.py                      # 3 working endpoints; NO /orders/{id}/summary (the agent adds it)
│       └── tests/
│           ├── test_existing.py        # regression guard for the 3 existing endpoints
│           └── test_summary.py         # the failing target: GET /orders/{id}/summary
├── shared/
│   ├── environment.compose.yaml        # 4-service compose (testrunner reset, rest ephemeral); shared `source` volume
│   ├── testrunner/main.py              # infra (bind-mounted RO): GET /health + POST /run-tests
│   └── app-db/init.sql                 # orders + customers substrate
└── dataset/tasks/endpoint_add/
    ├── task.yaml                       # inherits the project default_environment whole; no environment_manifest
    └── grading.yaml                    # three-way weighted: state_checks (decisive) + transcript_rules + llm_judge
```

## Related

- [`../multi_service_postgres_reset/README.md`](../multi_service_postgres_reset/README.md)
  — the `sql_dump` reset-recipe reference.
- [`../multi_service_cache_debug/README.md`](../multi_service_cache_debug/README.md)
  — the `redis_dump` reset-recipe reference.
- [`docs/RESET_RECIPES.md`](../../../docs/RESET_RECIPES.md)
  — the reset-recipe reference, including `filesystem_dir`.
- [`docs/MULTI_CONTAINER_GUIDE.md`](../../../docs/MULTI_CONTAINER_GUIDE.md)
  — authoring guide for multi-container tasks.
```
