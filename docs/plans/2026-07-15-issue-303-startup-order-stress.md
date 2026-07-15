# Plan: cross-service startup-order stress test

Issue: #303 (umbrella #304, Multi-container runtime v1, sub-issue 5/5 — last before milestone close)
Branch: test/startup-order-stress

## Context

`PerTrialRuntimeBackend.provision` brings each trial's compose stack up with
`docker compose up -d --wait` (`per_trial_runtime.py:224-231`, `wait=True`).
`--wait` blocks until every service's compose `healthcheck:` reports healthy,
and `depends_on: {condition: service_healthy}` gates the start order. The
existing `multi_service_postgres` / `_reset` packs work because postgres +
PostgREST start fast (a few seconds), so the `depends_on` + healthcheck chain
is never exercised under a genuinely slow dependency. The unproven race: if the
chain did *not* hold, `app-service` (PostgREST) would start before `app-db`
(postgres) is accepting TCP connections, and the agent's first tool call
(runner → PostgREST → postgres) would hit `ConnectionRefusedError` / a 500.

This is a **pure test issue**: no orchestrator or backend change. It proves the
*existing* `--wait` + healthcheck + `depends_on` behaviour holds when a
dependency is deliberately slow to become ready.

**How the slow start is produced (the load-bearing design decision).** The
postgres official entrypoint runs `docker-entrypoint-initdb.d/*.sql` on a
*socket-only* temporary server (`listen_addresses=''`) and only starts the real
**TCP** listener after every init script returns. So an init script that blocks
holds the container in a state where **TCP :5432 is genuinely refused** — the
honest failure surface the race would hit. The stress pack drives that window
deterministically with a trailing `SELECT pg_sleep(<N>)` in `init.sql`
(`N≈25`), and forces the app-db healthcheck to probe **TCP** so it correctly
reports "starting" for the whole window (see Stage 1 contract). A small but
real dataset (`generate_series`) is seeded ahead of the sleep so the DB the
agent queries is genuine.

Three slow-start options were weighed (issue body): (a) a large data import,
(b) an artificial init sleep, (c) a healthcheck that lies "starting" for N s.
**(a) is non-deterministic** — import CPU time varies by hardware, so a `≥20 s`
floor is flaky on fast CI and slow elsewhere. **(c) is dishonest** — postgres
would actually be ready, so a broken chain would *not* surface a connection
error; the test would prove nothing about the race. **(b) is the honest,
deterministic choice**: a `pg_sleep` in the init phase makes postgres genuinely
TCP-unreachable for a hardware-independent, wall-clock-bounded window, so the
`depends_on` chain is under real pressure and the `≥20 s` floor is stable. The
plan uses (b) with a real (if small) seeded dataset for realism.

**Correction to the issue's file sketch.** The issue lists
`assets/large_seed.sql`. A *reset asset* runs **after** `up --wait` returns
(`_apply_reset_recipes`, post-healthcheck), so it would not delay the chain
`--wait` gates and would stress nothing. The slow seed must be a
**docker-entrypoint-initdb.d init script** (under the compose context, so
`copy_compose_context` materialises it). The pack therefore has **no**
`assets/` / `assets.seeds` and **no** reset labelling — it is `multi_service_postgres`
with a slow init, not a reset pack.

## Goal

Ship a runnable pack `examples/native/multi_service_slow_start/` whose `app-db`
takes ≥20 s to become healthy, and prove — with tests at the right tiers — that:

1. the pack loads, resolves to a per-trial manifest, and routes to
   `PerTrialRuntimeBackend` (canonical, no Docker/keys);
2. a real trial through the CLI provisions in ≥20 s (slow start fired), emits
   **no** tool-call error, and completes with a valid passing grade (integration,
   real Docker + LLM key).

## Non-goals

- **No new startup mechanism** — uses Docker's existing `depends_on` +
  `healthcheck` + `--wait` only.
- **No orchestrator / backend change** — `provision()`'s `--wait` behaviour is
  exercised as-is. (The absence of a machine-readable provisioning-duration
  metric is filed as #354, not fixed here.)
- **No reset recipe** — this pack is about start *order*, not between-trial
  reset; `app-db` carries no `isolation: reset` and no seed asset.
- **No new `SeedKind` / no `assets.seeds`.**
- **No change to existing packs** — `multi_service_postgres` / `_reset` are
  untouched; the stress pack is additive.

## Stages

### Stage 1: Ship the slow-start pack + canonical wiring test

- **Contract — new files under `examples/native/multi_service_slow_start/`,
  mirroring `multi_service_postgres_reset` minus the reset asset/labels:**
  - `project.yaml` — Project spec (discovered by `find_project_yaml` walking up
    from the run config):
    - `tasks.discovery.glob: "tasks/**/task.yaml"`.
    - `default_environment.stack: {compose_file: ./shared/environment.compose.yaml, runner_service: runner}`.
    - **No** `default_environment.services` block. The loader fills every compose
      service with the `ephemeral` default, so `EnvironmentManifest.requires_per_trial`
      is `True` (`runner/models.py:1062-1064`: true when any service ≠ `shared`),
      routing the run to `PerTrialRuntimeBackend` without any `orchestrator.runtime`
      override and without the `shared`-label wart the #299 plan warned against.
    - **No** `assets:` block.
    - `task_defaults` (adapter `native`, small `max_turns: 8`, cooperative
      `actors.user`) and `run_defaults` (`compute` + `orchestrator` only —
      storage/observability omitted, same loader-discriminator constraint the
      #299 plan documented).
  - `run_config.yaml` at pack root (next to `project.yaml`):
    - `models.agent` + `models.user` = a cheap model (`openrouter`
      `anthropic/claude-haiku-4-5`), mirroring the reset pack.
    - `orchestrator.repeats: 1`, `workers: 1`, `max_turns: 8`. **`repeats: 1`**
      so the single trial produces exactly one `"Provisioning trial env"` /
      `"Trial env provisioned"` log pair — unambiguous for the Stage 2 latency
      parse.
    - `evaluation.projects: [examples/native/multi_service_slow_start/dataset]`
      (canonical field, **not** `task_packs`), `tasks_glob: tasks/startup_probe/task.yaml`,
      `output_dir: results/multi_service_slow_start_example`.
  - `shared/environment.compose.yaml` — the four-service stack copied from the
    reset pack (`runner` `tolokaforge-runner:local`, `db-service`
    `tolokaforge-db-service:local`, `app-service` `postgrest/postgrest:v12.2.0`,
    `app-db` `postgres:16`), real images only, with **two changes on `app-db`**:
    - **Healthcheck probes TCP, not the socket.** The reset pack's
      `pg_isready -U authenticator -d appdb` uses the unix socket, which the
      temp init server *does* answer — it would flap healthy mid-init and defeat
      the slow start. Use `pg_isready -h 127.0.0.1 -U authenticator -d appdb`
      (or `-h app-db`) so the check fails while only the socket-only temp server
      is up, and passes only once the real TCP listener starts (i.e. after
      `init.sql` incl. `pg_sleep` returns).
    - **`start_period` sized above the slow-start window** so failing TCP probes
      during init keep the container `starting` (not `unhealthy`, which would
      fail `--wait`): `start_period: 45s`, `interval: 2s`, `timeout: 3s`,
      `retries: 15` for a `pg_sleep(25)`.
    `app-service.depends_on.app-db.condition: service_healthy` and the `runner`
    `depends_on` block are unchanged from the reset pack — that chain is exactly
    what is under test.
  - `shared/app-db/init.sql` — schema + roles + grants (identical shape to the
    reset pack: `api` schema, `web_anon`/`authenticator` roles, `api.widgets`
    table, `GRANT SELECT`), then a **small real dataset** via
    `INSERT ... SELECT ... FROM generate_series(...)` (a few thousand rows) plus
    the distinctive probe row (`id=1, name='slow_start_ok'`), then a trailing
    **`SELECT pg_sleep(25);`** as the last statement. Because init scripts run on
    the socket-only temp server, this whole file executes before the real TCP
    server starts — the schema/data are fully present by the time PostgREST
    (gated on `app-db` healthy) connects, so no stale-schema-cache miss.
  - `dataset/tasks/startup_probe/task.yaml` — declares **no**
    `environment_manifest` (inherits the project `default_environment` whole).
    Goal-oriented `initial_user_message` (AGENTS.md Task Design Quality Bar #2,
    not a walkthrough): read widget 1's `name` from
    `http://app-service:3000` and write it under `submissions/`. `tools.agent.enabled:
    [bash, write_file]`; cooperative user simulator ending `###STOP###`; the
    same `policies.guidance` note the reset pack uses (reach the API from `bash`
    via `python3 -c urllib`, service-name DNS, no curl/wget).
  - `dataset/tasks/startup_probe/grading.yaml` — deterministic, **no `llm_judge`**:
    `state_checks.jsonpaths` on `/env/fs/agent-visible/submissions/*`
    `contains_ci: "slow_start_ok"` (the agent could only have read this after the
    chain came up cleanly — proof of no premature-connection failure);
    `transcript_rules.required_actions` for `bash` + `write_file`.
  - `README.md` — what the pack demonstrates: the `pg_sleep`-driven slow start,
    the TCP healthcheck, and how a passing grade proves the `depends_on` chain
    held. Written as current-state prose (no migration history).
- **Behaviour to lock (canonical):** `tests/canonical/test_slow_start_pack_wiring.py`
  (no Docker, no keys — pure load/resolve/select), mirroring
  `tests/canonical/test_reset_recipe_pack_wiring.py`. Asserts:
  - `load_project_config(project.yaml)` loads and has **no** `assets.seeds`
    (`project.assets is None` or `.seeds == {}`);
  - `resolve(project.default_environment, None)` yields a manifest whose
    services are all `ephemeral` (no `shared`, no `reset`) and
    `requires_per_trial is True`;
  - `Orchestrator._select_backend_from_tasks() == "per_trial"` and
    `_construct_runtime_backend(...)` returns a `PerTrialRuntimeBackend`.
- **Compatibility:** internal only — new example files + a new canonical test.
  No compatibility surface touched (uses the already-shipped Project schema,
  `default_environment`, and `evaluation.projects`).
- **Deliverable:** the pack exists; the canonical wiring test passes.
- **Validation:**
  - `uv run pytest -m canonical tests/canonical/test_slow_start_pack_wiring.py -v`.
  - `uv run tolokaforge validate --tasks "examples/native/multi_service_slow_start/dataset/tasks/**/task.yaml"`.
  - `uv run ruff check` / `ruff format --check` on the new test.
  - Reviewer checks: no `assets.seeds`; no service labelled `shared`; app-db
    healthcheck uses `-h` (TCP); `start_period` > `pg_sleep`; grading has no
    `llm_judge`; `evaluation.projects` (not `task_packs`); real images only.
- **Doc updates:** `examples/native/multi_service_slow_start/README.md` (new).

### Stage 2: End-to-end slow-start stress integration test + RUNTIME_BACKENDS.md

- **Contract — `tests/integration/test_startup_order_stress.py`:** marked
  `integration` + `docker` + `requires_api` + `llm` + `slow`, mirroring
  `tests/integration/test_reset_recipe_end_to_end.py` (shared auto-skips:
  `requires_api` skips with no LLM key per `conftest.py`; `is_docker_daemon_available()`
  skip covers a missing daemon). Runs
  `uv run tolokaforge run --config examples/native/multi_service_slow_start/run_config.yaml`
  via `subprocess` (env `os.environ.copy()`), one trial, capturing stdout+stderr.
  Asserts all three ship conditions:
  1. **Slow start fired (latency ≥ 20 s).** Parse the single trial's
     `"Provisioning trial env"` and `"Trial env provisioned"` console log lines
     (emitted by `ProvisioningTrialExecutor.execute`, `trial_executor.py:106,139`)
     from the captured output; each carries the `StructuredLogger` console
     prefix `%Y-%m-%d %H:%M:%S` (`logging.py:52-53`). Assert the wall-clock delta
     ≥ 20 s. `repeats: 1` guarantees exactly one such pair, so the parse is
     unambiguous. (Image build happens *before* the first `"Provisioning trial
     env"`, so the delta is provision-only — build time does not inflate it.)
  2. **No tool-call error during the trial.** Assert exit code 0 **and**
     `grade.yaml.components.state_checks == 1.0` — the agent read
     `slow_start_ok` back over PostgREST, which is only possible if the first
     runner → app-service → postgres call succeeded (no `ConnectionRefusedError` /
     500). A premature-connection failure would leave the submission empty and
     `state_checks` at 0.
  3. **Valid grade.** Assert the single `trials/startup_probe/0/grade.yaml`
     exists with `binary_pass: true` (per `docs/OUTPUT_FORMAT.md` §
     `trials/{task_id}/{trial_index}/grade.yaml`).
  Also assert `"runtime.backend.selected"` + `"PerTrialRuntimeBackend"` appear
  in the output (route confirmation), as the reset e2e does. Reuse the reset
  e2e's `output_dir` discovery + cleanup pattern (`before`/`after` glob on the
  timestamped run dir, `shutil.rmtree` in `finally`).
- **Behaviour to lock (integration):** the `depends_on` + healthcheck + `--wait`
  chain holds under a ≥20 s slow dependency — the trial provisions slowly, the
  first tool call succeeds, and grading passes. This is the exact race the issue
  flags, exercised end-to-end via the CLI.
- **Compatibility:** internal only — new test + doc section.
- **Deliverable:** the integration test passes on a real Docker daemon with an
  LLM key; the ship-condition `tolokaforge run` command exits 0 with a slow
  provision and a passing grade.
- **Validation:**
  - `scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service_slow_start/run_config.yaml` — exits 0; provision visibly ≥20 s; grade reads `slow_start_ok`.
  - `scripts/with_env.sh uv run pytest -m integration tests/integration/test_startup_order_stress.py -v`.
  - No new `DeprecationWarning` beyond baseline (uses `evaluation.projects`).
- **Doc updates:** `docs/architecture/RUNTIME_BACKENDS.md` — a brief subsection
  (under "Deep-dive — `provision()`" or "Per-trial isolation") noting that the
  `--wait` + `depends_on: service_healthy` chain is stress-covered against a
  deliberately slow dependency by `multi_service_slow_start`, and that
  `PerTrialRuntimeBackend` blocks on the full chain before the trial's first RPC.
  Current-state prose only. `CHANGELOG.md` entry.

## Discovered issues

- **Fix in this PR:** none — the pack is additive and the neighbourhood is clean.
- **Filed as issues:**
  - **#354** — record per-trial provisioning duration as a first-class metric.
    There is no machine-readable provisioning-latency signal today
    (`TrialResult`/`metrics.yaml` carry none; the executor's two log lines have
    no `duration_s`), so Stage 2 parses console `asctime` (second granularity).
    A `provisioning_duration_s` field would make latency assertions robust and
    feeds the perf-optimisation umbrella (RUNTIME_BACKENDS § Follow-up work).
    Out of scope for this pure-test issue.
- **Documented in-plan (not a separate issue):** the issue's `assets/large_seed.sql`
  sketch is corrected to a `docker-entrypoint-initdb.d` init script (see Context)
  — a reset asset runs post-`--wait` and would stress nothing.

## Risks / open questions

- **Latency-parse brittleness.** The ≥20 s assertion diffs two console
  timestamps at `%H:%M:%S` (second) granularity. Robust enough for a 20 s floor
  and unambiguous at `repeats: 1`, but format-coupled to `logging.py`'s console
  formatter. The clean fix is #354 (a `duration_s` field); until then the parse
  targets the stable event *messages* (`"Provisioning trial env"` /
  `"Trial env provisioned"`), not their positional format. If a reviewer prefers
  zero log-parsing, the fallback is a direct-`provision()` integration test in
  `tests/integration/docker/` timing the call with `time.monotonic()` — but that
  needs the `tolokaforge-runner:local` / `db-service:local` images pre-built
  (the subprocess path builds them itself), so it trades one fragility for a
  heavier fixture. Recommendation: ship the subprocess parse; revisit under #354.
- **Healthcheck must probe TCP.** The single most common way to get this pack
  wrong is to keep the reset pack's socket-based `pg_isready` — the temp init
  server answers the socket, the check flaps healthy mid-`pg_sleep`, and the
  slow start never fires (latency assertion fails, or app-service races postgres
  anyway). The `-h 127.0.0.1` TCP probe + `start_period > pg_sleep` is the
  load-bearing contract; Stage 1's reviewer check and Stage 2's ≥20 s assertion
  both catch a regression here.
- **`pg_sleep` honesty.** `pg_sleep(25)` is a deterministic stand-in for
  genuine cold-start / large-import / index-build latency. It stresses the exact
  same chain (postgres genuinely TCP-unreachable for the window) while keeping
  the `≥20 s` floor hardware-independent. A data-volume-only delay was rejected
  as non-deterministic (see Context). The seeded `generate_series` dataset keeps
  the DB a real one the agent queries.
- **CI cost / flakiness.** One trial, one cheap model, ~8 turns, deterministic
  state-check grading (no judge). Cost ≈ cents. The dominant wall-clock cost is
  the 25 s slow start itself plus first-run image build; the `slow` marker and
  the reset e2e's 1800 s subprocess timeout cover it.
- **`start_period` vs Docker version.** The contract relies on modern Docker
  semantics where failing healthchecks *during* `start_period` keep a container
  `starting` (not `unhealthy`) and `--wait` keeps waiting. `start_period: 45s`
  for `pg_sleep(25)` leaves generous margin; if a very old daemon counted
  start-period failures toward `retries`, `--wait` could fail — acceptable
  because CI/dev run current Docker, and a `ProvisionError` would fail loudly,
  not silently pass.
