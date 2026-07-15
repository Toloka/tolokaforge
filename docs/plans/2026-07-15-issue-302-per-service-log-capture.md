# Plan: per-service log capture on trial failure

Issue: #302 (umbrella #304, Multi-container runtime v1, sub-issue 4/5)
Branch: feat/per-service-log-capture

## Context

When a multi-service trial fails, per-service Docker logs are never captured to
the trial output dir. The two failure surfaces both throw the logs away:

- **Provision-stage failures** — `PerTrialRuntimeBackend.provision` (compose
  `up --wait` healthcheck failure) and its reset-recipe path both call
  `cleanup_partial_materialisation` (`docker compose down -v`) *inside*
  `provision()` before the typed `ProvisionError` is re-raised
  (`tolokaforge/core/per_trial_runtime.py:216`, `:234`). By the time
  `ProvisioningTrialExecutor` sees the error, the containers are gone.
- **Trial-body failures** — `Conductor.run` returns a failed `TrialResult`;
  `ProvisioningTrialExecutor.execute` tears the stack down in its `finally`
  (`tolokaforge/core/trial_executor.py:136`) with no capture in between.

So the exact moment an operator needs to know *why postgres/PostgREST went
sideways*, the evidence is already deleted. This is the observability gap for
multi-container workloads. `compute.log_tail` does not exist yet — this plan
adds it.

## Goal

On **trial failure** on the per-trial backend, for every service the task's
compose stack declares, write `docker compose logs`-style output to
`<output_dir>/trials/<task_id>/<trial_index>/services/<service>.log`, captured
**before** the stack is torn down. Emit a structured summary line naming the
captured services and their byte counts, and record the captured services + byte counts on a durable surface: a
`captured_service_logs` field on the trial's `metrics.yaml` where that file
exists (the trial-body path), and a `services/_capture.yaml` manifest on the
provision-failure path (where `metrics.yaml` is never written). On success, capture
nothing unless a debug flag is set. Raw `docker compose logs` text is the v1
format — no parsing, no structured per-service metrics.

Failure, for capture purposes, means **execution failure**
(`trajectory.status in {ERROR, TIMEOUT}`) or a `ProvisionError` — *not* a clean
run that merely failed grading. A `COMPLETED` trial with `binary_pass=False` is
not a capture trigger (that would capture on most trials of a hard benchmark and
blow the output-dir size budget the issue explicitly wants bounded).

## Non-goals

- No structured per-service metrics (CPU / memory / restart counts).
- No log-format changes — raw `docker compose logs --tail=N` output only.
- No UI, no per-service dashboard — files on disk plus one structured log line.
- No capture on the **shared-stack** backend: its stack is run-wide (not
  trial-scoped) and its per-trial `teardown` is a no-op, so per-trial capture is
  meaningless. `SharedStackRuntimeBackend.capture_service_logs` is a documented
  no-op. The run-level materialise-failure gap is filed as #339.
- `metrics.yaml` gains a `captured_service_logs` field on the trial-body path
  (ship condition #3), but no run-level `aggregate.json` roll-up (filed as
  #337); no full trial bundle for provision-failed trials (filed as #338).

## Stages

### Stage 1: capture primitive + config knobs

- **Contract:**
  - `ComputeConfig` (`tolokaforge/core/models.py`) gains two fields:
    - `log_tail: int = Field(default=500, ge=1)` — tail line count passed to
      `docker compose logs --tail`.
    - `capture_logs_on_success: bool = False` — debug escape hatch; when `True`,
      capture runs for successful trials too.
    `ComputeConfig` (`models.py:672`) declares no `model_config` and inherits
    Pydantic's default `extra="ignore"` — unknown compute keys are silently
    ignored today, not rejected. Both new fields are additive and defaulted, so
    existing configs load unchanged either way; this plan does **not** tighten
    `extra` (out of scope).
  - New frozen value object `LogCaptureConfig` in
    `tolokaforge/core/compose_materialisation.py`
    (`@dataclass(frozen=True)`, internal in-process value — per the type-system
    table): `output_root: Path`, `tail: int`, `on_success: bool`.
  - New helper in the same module:
    `capture_compose_service_logs(compose: DockerCompose | None,
    service_names: Iterable[str], dest_dir: Path, tail: int) -> dict[str, int]`.
    - Writes `dest_dir/<service>.log` for each name; returns
      `{service_name: bytes_written}` (only services that produced output).
    - Honours `--tail=N` by invoking the compose CLI with a tail bound
      (testcontainers `DockerCompose.get_logs(*services)` has **no** tail
      parameter — verified against `testcontainers>=4.14.2`). Build the command
      from `compose.compose_command_property` (the base
      `docker compose -f <file>` list, `compose.py:254`) and append
      `logs --no-color --no-log-prefix --tail=<N> <service>`. The subprocess
      **must run with `cwd=str(compose.context)`** — `compose_command_property`
      carries no `-p/--project-name`, so Docker Compose derives the project from
      the context-dir basename (the per-trial temp dir whose name encodes the
      trial id); a wrong cwd silently targets no project and returns empty logs.
    - **Never raises** (fail-fast rule applies to the trial body, not to
      best-effort diagnostics captured *because* something already failed): a
      per-service fetch error is `logger.debug`-logged and that service is
      omitted from the returned map. `compose is None` → returns `{}` (nothing
      materialised to read).
    - Creates `dest_dir` (parents=True) only when there is at least one service
      to attempt.
- **Behaviour to lock:**
  - unit (`tests/unit/test_compose_log_capture.py`): against a fake object
    exposing `compose_command_property` + `context`, assert one `.log` file per
    service with the expected bytes in the returned map; assert a raising
    service is omitted and no exception propagates; assert `compose=None` → `{}`
    and no dir created. The fake pins the **command shape** (verb order, `--tail`
    value); it cannot verify the live `cwd=compose.context` project-resolution
    invariant — that is locked by the Stage 2 integration test.
  - canonical (extend the `ComputeConfig` schema test): `log_tail` defaults to
    500, rejects `0`/negative (`ge=1`), `capture_logs_on_success` defaults
    `False`.
- **Compatibility:** `ComputeConfig` is a run-config schema surface. Additive,
  both fields defaulted — existing configs load unchanged. Migration note:
  CHANGELOG entry + `docs/CONFIG.md` compute section documents the two fields.
- **Deliverable:** the config fields and the capture helper exist and are
  tested; no backend wires them yet.
- **Validation:** `run_tests` markers `unit` + `canonical`; `lint_check`.
- **Doc updates:** `docs/CONFIG.md` (compute block — add `log_tail`,
  `capture_logs_on_success` with defaults/semantics); `CHANGELOG.md`.

### Stage 2: per-trial capture — provision-failure path + backend Protocol method

- **Contract:**
  - `RuntimeBackend` Protocol (`tolokaforge/core/runtime.py`) gains one method:
    `capture_service_logs(self, handle: EnvHandle, *, failed: bool) -> dict[str, int]`.
    Semantics: when the backend has a live per-trial stack for `handle` and
    (`failed` or the backend's `on_success` policy), write per-service logs to
    the handle-derived trial `services/` dir and return the byte map; otherwise
    return `{}`. Never raises. This is an internal Protocol (not a published
    API) — all three implementations update in this stage, no shim.
  - `PerTrialRuntimeBackend`:
    - New field `log_capture: LogCaptureConfig | None = None` (None = disabled,
      e.g. tests / when construction supplies no output root).
    - `_LocalEnvHandle` gains `service_names: tuple[str, ...]` (snapshot of the
      compose stack's declared services at provision time).
    - `provision()` failure paths (the two `except` blocks at
      `per_trial_runtime.py:216` and `:234`) call the shared helper to capture
      **before** `cleanup_partial_materialisation`, deriving `dest_dir` from
      `log_capture.output_root` + `spec.trial_id` and the manifest's service
      names (`manifest.load_compose()["services"]`). No-op when
      `log_capture is None` or `compose is None`. On this path `metrics.yaml`
      does not exist (the conductor never ran), so the durable record is a
      `services/_capture.yaml` manifest written alongside the `.log` files
      (shape in Stage 3; the writer lives in the shared module).
    - `capture_service_logs(handle, *, failed)`: no-op `{}` when
      `log_capture is None`; else gate on `failed or log_capture.on_success`;
      else capture `handle.service_names` from `handle.compose` into the derived
      trial `services/` dir; return the byte map. The durable record here is the
      `metrics.yaml` amendment written by the executor (Stage 3), which owns the
      failure decision — this method only writes the `.log` files and returns
      the map.
  - `SharedStackRuntimeBackend.capture_service_logs`: documented no-op returning
    `{}` (rationale in the docstring + RUNTIME_BACKENDS.md; run-level gap is
    #339).
  - `InMemoryRuntimeBackend.capture_service_logs`: records the call
    (`RuntimeBackendCallLog` gains `capture_service_logs_calls:
    list[tuple[str, bool]]`) and returns `{}`.
  - Orchestrator wiring: `run()` / `run_worker()` build **one**
    `LogCaptureConfig` from `output_dir` + `compute.log_tail` +
    `compute.capture_logs_on_success` (`output_dir` is in scope at both sites —
    `orchestrator.py:1121` and `:1614`) and pass the same instance to
    `_construct_runtime_backend` (→ `PerTrialRuntimeBackend(seeds=...,
    log_capture=...)`) and to `_build_trial_executor` (Stage 3, for the
    `metrics.yaml` amendment). Both helpers gain a `log_capture:
    LogCaptureConfig | None` parameter. Built-in shared mode passes `None`.
- **Behaviour to lock:**
  - integration (`tests/integration/docker/test_per_trial_log_capture_integration.py`,
    real Docker, no LLM keys — provisioning fails before the agent loop):
    construct a `PerTrialRuntimeBackend` with a `LogCaptureConfig` pointed at a
    `tmp_path`, provision a manifest whose extra service fails its healthcheck
    (or a reset-recipe pointed at a missing seed) → assert `ProvisionError`
    **and** `tmp_path/trials/<task>/<idx>/services/<service>.log` exists with
    size > 0 for the declared services, and `_capture.yaml` byte counts match
    the file sizes. Mirrors the harness in
    `tests/integration/docker/test_per_trial_runtime_backend_integration.py`.
  - canonical (extend `tests/canonical/test_runtime_backend_contract.py`, which
    already owns the `runtime_checkable` isinstance parity check across all three
    backends at lines 51–62 — add cases there, do **not** add a parallel file):
    `InMemoryRuntimeBackend.capture_service_logs(handle, failed=True/False)`
    records the call; `SharedStackRuntimeBackend.capture_service_logs` returns
    `{}`; the existing conformance check still passes with the new method on the
    Protocol.
- **Compatibility:** internal only — `RuntimeBackend` is an in-repo Protocol,
  not a compatibility surface. All implementations land in this stage.
- **Deliverable:** provision-stage failures write per-service logs on the
  per-trial backend end-to-end; the Protocol method exists on every backend.
- **Validation:** `run_tests` markers `canonical` (+ `integration` with Docker);
  `lint_check`.
- **Doc updates:** `docs/architecture/RUNTIME_BACKENDS.md` — new "Per-service
  log capture on failure" section describing the two capture surfaces, the
  failure definition, the shared-stack no-op rationale, and the `compute`
  knobs; update the "Failure modes" table rows for `provision` /
  `reset_recipe` to note logs are captured before teardown. `CHANGELOG.md`.

### Stage 3: executor drives trial-body-failure capture + summary line + `services/` bundle

- **Contract:**
  - `ProvisioningTrialExecutor.execute` (`tolokaforge/core/trial_executor.py`):
    after `conductor.run` returns and **before** the `finally` teardown, compute
    `failed = result.trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT)`
    and call `self.runtime_backend.capture_service_logs(handle, failed=failed)`.
    When the returned map is non-empty: (1) emit
    `self.logger.info("trial.service_logs_captured", task_id=..., trial_index=...,
    services={name: bytes})` — the required aggregate summary line; and (2)
    amend the trial's `metrics.yaml` with a top-level
    `captured_service_logs: {"<service>": <bytes>}` mapping. This literally meets
    ship condition #3 on the surface the issue names ("per-trial `metrics.yaml`
    mentions the captured logs") — `metrics.yaml` is already written by the
    conductor by the time the executor runs on the trial-body path. Capture
    failures never change control flow (the method itself never raises); the
    `metrics.yaml` amendment is a read-add-write of a plain YAML mapping (the
    same file `_collect_existing_cost` reads at `orchestrator.py:350`) and is a
    no-op if the file is unexpectedly absent.
  - `services/_capture.yaml` manifest (provision-failure path only, where no
    `metrics.yaml` exists — #338): written by the shared module, shape
    `{"tail": int, "capture_reason": "provision_error", "services":
    {"<name>": {"bytes": int}}}`. It is the durable record for that surface;
    the trial-body path uses the `metrics.yaml` amendment instead, so the two
    surfaces never both write.
  - The executor owns the `metrics.yaml` amendment and derives the trial dir
    from the backend's `LogCaptureConfig.output_root` + `handle.trial_id`
    (threaded into the executor via `_build_trial_executor`, which reads the
    same `LogCaptureConfig` the backend was constructed with). The per-service
    `.log` files and their destination remain backend-owned via
    `capture_service_logs`.
- **Behaviour to lock:**
  - canonical (`tests/canonical/test_executor_log_capture.py`): inject an
    `InMemoryRuntimeBackend` (with `capture_service_logs` stubbed to return a
    non-empty map and to write nothing) and a fake `Conductor` returning an
    `ERROR` trajectory, with a pre-written `metrics.yaml` in the trial dir →
    assert `capture_service_logs(handle, failed=True)` recorded, a
    `trial.service_logs_captured` line emitted (via an in-memory
    `StructuredLogger`), and the `metrics.yaml` now carries
    `captured_service_logs`; a `COMPLETED` (`binary_pass=False`) trajectory →
    `capture_service_logs(handle, failed=False)` recorded, **no** summary line
    and **no** `metrics.yaml` amendment when the map is empty. Locks both the
    "graded-fail is not a capture trigger" contract and the ship-condition-#3
    `metrics.yaml` surface, without Docker.
  - integration (extend the Stage 2 file): after a *successful* provision, call
    `capture_service_logs(handle, failed=False)` with
    `capture_logs_on_success=False` → assert **no** `services/*.log`; with
    `capture_logs_on_success=True` → assert `services/*.log` present. (The
    `metrics.yaml` amendment is locked at the canonical tier above; the
    integration tier locks the real-Docker per-service `.log` capture.)
- **Compatibility:** `services/<service>.log` + `services/_capture.yaml` are new
  entries in the trial output bundle — an additive `docs/OUTPUT_FORMAT.md`
  surface. Documented, not versioned (additive files don't break readers).
- **Deliverable:** trial-body failures capture per-service logs, emit the
  summary log line, and amend `metrics.yaml` with `captured_service_logs`;
  provision failures record `_capture.yaml`; success skips capture by default.
- **Validation:** `run_tests` markers `canonical` (+ `integration`);
  `lint_check`; `format_check`.
- **Doc updates:** `docs/OUTPUT_FORMAT.md` — add `services/<service>.log` +
  `services/_capture.yaml` to the `trials/{task_id}/{trial_index}/` tree, and
  document the `captured_service_logs` field in the `metrics.yaml` section,
  each with a note on which failure surface produces it;
  `docs/architecture/RUNTIME_BACKENDS.md` cross-link the trial-body path.
  `CHANGELOG.md`.

## Discovered issues

- **Fix in this PR:** none — the neighbourhood is clean; the capture helper's
  broad-but-logged exception handling matches the existing
  `compose_materialisation.py` best-effort teardown idiom (not a fail-fast
  violation, since capture only runs *after* a failure is already decided).
- **Filed as issues:**
  - #337 — surface captured service logs in the run-level `aggregate.json`
    report (per-trial `metrics.yaml.captured_service_logs` and
    `services/_capture.yaml` ship in this PR; the run-level roll-up is deferred).
  - #338 — provision-failed trials get no full trial bundle (`Conductor.run`
    never runs for them; only the synthesized trajectory is aggregated).
  - #339 — capture shared-stack compose logs on run-level materialise failure
    (`_materialise_manifest`'s `connect()`-time `docker compose up` failure).

## Risks / open questions

- **`--tail` mechanism.** testcontainers' `DockerCompose.get_logs` has no tail
  parameter, so the helper drives the compose CLI directly with `--tail=N`. The
  implementer must derive the correct base command (compose file + context dir)
  from the `DockerCompose` instance rather than hardcoding `docker compose`;
  Stage 1's unit test uses a fake to pin the command shape.
- **Failure definition.** Capturing only on `status in {ERROR, TIMEOUT}` (plus
  `ProvisionError`) — not on `binary_pass=False` — is the load-bearing scoping
  decision that keeps the output-dir bounded. If reviewers want graded-fail
  capture too, that is a policy flip on one predicate, but it changes the
  disk-footprint contract and should be a conscious call.
- **Reset-recipe stage.** The `reset_recipe` `except` in `provision()` catches
  broadly (typed `ProvisionError` *and* programming errors) before re-raising;
  the capture call sits before `cleanup_partial_materialisation` in that block
  too, so both stages are covered. The capture must not itself mask the
  original error — it is a separate best-effort statement before the re-raise.
- **Service-name source.** Capture covers every service in the compose stack
  (`manifest.load_compose()["services"]` at provision; `handle.service_names`
  snapshot for the trial-body path), which is a superset-safe reading of "every
  service declared in `EnvironmentManifest.services`" (that map may be empty
  when a task declares no per-service isolation but still runs multiple compose
  services).
