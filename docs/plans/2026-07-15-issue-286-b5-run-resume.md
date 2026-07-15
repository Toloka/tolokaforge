# Plan: B5 — `tolokaforge run --resume` idempotent replay

Issue: [#286](https://github.com/Toloka/tolokaforge/issues/286) (milestone: Terminal DX / umbrella #297)
Base branch: `feat/terminal-dx`
Feature branch: `feat/issue-286-b5-run-resume`

## Context

`--resume` is already declared on `tolokaforge run` (`tolokaforge/cli/main.py:443`) and the underlying persistence layer (`tolokaforge/core/resume.py::RunStateManager`) is complete: it initialises `run_state.json`, marks trials `pending` / `running` / `completed` / `failed`, and knows how to discriminate infra failures from behavioural failures. The orchestrator (`tolokaforge/core/orchestrator.py::Orchestrator.run`) consumes this state on `resume=True`, filters completed trials via `_build_pending_trials(skip_completed=...)`, seeds cost from prior artifacts via `_collect_existing_cost`, and calls `_events.run_started(total_trials, initial_completed)` — the B1 Live panel already accepts an `initial_completed` head-start.

The CLI wiring is broken. `run()` calls `resolve_run_directory(evaluation.output_dir)` **unconditionally**, which allocates a fresh `<basename>_<YYYYMMDD_HHMMSS>` sibling every invocation. Passing `--resume` on the CLI therefore always points `RunStateManager` at an empty new directory, `load_state()` returns `None`, and the orchestrator hits the silent-fallback branch `self.resume = False` with an INFO log. Resume works only from the programmatic API today (`Orchestrator.run(run_id=<existing>, output_dir=<existing>)` — used by tests).

The issue's deliverable — `tolokaforge run --config … --resume [--run-dir <existing>]` — is missing the `--run-dir` flag, missing the resume-summary line (`resuming: X/N completed, K to retry`), missing the idempotent no-op ("nothing to do; run already complete"), and missing prepare/worker documentation.

## Goal

`tolokaforge run --config <path> --resume --run-dir <existing>` reuses `<existing>` verbatim (no fresh timestamp), reloads `run_state.json`, prints a summary of what will be replayed, executes only the trials that aren't already `completed` (or behavioural-failed), and updates `run_state.json` in place. On a fully-completed run it prints a "nothing to do" line and exits 0 without touching Docker or the runtime backend. The queue-worker path (`prepare` + `worker`) remains resumable via the durable queue — a restarted `worker --run-dir <existing>` picks up pending items — and gets a friendly resume-summary log line and a documented recipe.

## Non-goals

- **`runs list` command (C1, #287).** Discovery of resumable runs is C1's job; `--resume` here requires an explicit `--run-dir`.
- **Cumulative budgets across resumes.** Per-invocation is documented in `docs/CLI.md` § Cost, time, and sample limits; the cumulative mode is #352.
- **`--resume` support for `prepare` or `worker`.** Their existing semantics (queue is source of truth, restart == resume) already cover the requirement. No new CLI flag on those commands.
- **Auto-discovery of the newest run-dir when `--run-dir` is omitted.** Rejected: `--resume` without `--run-dir` is a `UsageError`. Rationale: fail-loud (AGENTS.md Core Rule 1), and multi-run directories are ambiguous.
- **Changing the on-disk shape of `run_state.json`.** The current pydantic schema (`RunState` / `TrialState`) is sufficient; no migration.
- **Making the programmatic-API resume path loud-fail on a stateless dir.** Filed as #363; out of scope here.

## Stages

### Stage 1: Resume-directory resolver + resume-plan describer (pure, no CLI)

- **Contract:**
  - New function `resolve_resume_run_directory(run_dir: Path) -> tuple[str, Path]` in `tolokaforge/core/resume.py`:
    - Reads `<run_dir>/engine_run_state.json` for the canonical `run_id` (via existing `read_persisted_run_id`); falls back to `<run_dir>/run_state.json::run_id`; falls back to `run_dir.name`.
    - Returns `(run_id, run_dir)` — `run_dir` is not `.resolve()`'d (parity with `resolve_run_directory`).
    - Raises `RuntimeError` with a message naming the missing file when the directory has neither `engine_run_state.json` nor `run_state.json`. Message: `"{run_dir} is not a resumable run directory: no engine_run_state.json or run_state.json present. Run `tolokaforge run` first (without --resume) to create one."`
  - Extend `RunStateManager` with `describe_resume_plan() -> ResumePlan`:
    - `ResumePlan` is a new frozen dataclass in `tolokaforge/core/resume.py` with fields `run_id: str`, `total: int`, `completed: int`, `already_done: int`, `to_retry: int`, `is_complete: bool`.
    - Semantics: `completed` = trials with `status == "completed"` regardless of `binary_pass`; `already_done` = trials that `is_completed(...)` returns `True` for (completed + behavioural-failed); `to_retry` = `total - already_done` (pending + running + infra-failed); `is_complete` = `to_retry == 0`.
    - Returns `None` when no `run_state.json` on disk (parity with `get_resume_info`).
- **Behaviour to lock:**
  - `resolve_resume_run_directory` returns the canonical `run_id` from `engine_run_state.json` even when the dir basename differs (locks legacy-heal parity with `_canonicalise_resumed_run_id`). **Tier: unit**, in a new `tests/unit/test_resume_resolver.py`.
  - `resolve_resume_run_directory` raises `RuntimeError` matching `not a resumable run directory` when both metadata files are absent. **Tier: unit**, same file.
  - `describe_resume_plan` on a mixed state (some completed / one behavioural-failed / one infra-failed via `_has_infrastructure_error` monkey-patch / some pending) returns `already_done` and `to_retry` matching the `is_completed` semantics. **Tier: unit**, in `tests/unit/test_resume.py` (extends the existing file to keep resume behaviour in one place).
- **Compatibility:** internal only. No CLI, config, or task-pack surface touched.
- **Deliverable:** `tolokaforge/core/resume.py` gains `resolve_resume_run_directory` and `RunStateManager.describe_resume_plan` + `ResumePlan` dataclass. New file `tests/unit/test_resume_resolver.py`; `tests/unit/test_resume.py` gains a `TestDescribeResumePlan` block.
- **Validation:** `uv run pytest tests/unit/test_resume_resolver.py tests/unit/test_resume.py -v -m unit`. Ruff + format clean.
- **Doc updates:** none this stage (helpers are internal until Stage 2 wires them).

### Stage 2: CLI wiring — `--run-dir`, summary line, resume banner, idempotent no-op

- **Contract:**
  - `tolokaforge run` gains `--run-dir <PATH>` (`click.Path(exists=True, file_okay=False)`, default `None`).
  - Flag interactions (validated at click-parse / early command body, before any config load):
    - `--run-dir` **requires** `--resume` — otherwise `click.UsageError("--run-dir requires --resume; use it only to point --resume at an existing run directory")`.
    - `--resume` **requires** `--run-dir` — otherwise `click.UsageError("--resume requires --run-dir <path> pointing at an existing run directory")`.
    - `--resume` + `--dry-run` — existing mutex is unchanged; keep the current `UsageError` message.
    - `--resume` + `--run-dir` + `--reset-queue` — n/a (`--reset-queue` lives on `prepare` only).
  - When `--resume` is set: the CLI **must not** call `resolve_run_directory`. Instead it calls `resolve_resume_run_directory(Path(run_dir))` to get `(run_id, run_dir)`.
  - After `run_id`/`run_dir` are resolved and before Docker starts:
    1. Open `RunStateManager(run_dir)` and call `describe_resume_plan()`. If `None`, raise `click.ClickException("--resume: {run_dir}/run_state.json is missing; --run-dir must point at a completed prepare or a prior run")`.
    2. If `plan.is_complete`: print `[muted]→[/muted] Nothing to do; run already complete ({plan.completed}/{plan.total} completed)`, call `emit_artifact_path(run_dir)`, exit 0. No orchestrator construction, no start/end banner, no Docker.
    3. Otherwise: print `[bold]Resuming:[/bold] {plan.already_done}/{plan.total} completed, {plan.to_retry} to retry`. Then continue to the normal run path — start banner, orchestrator, end banner.
  - Start-banner variant: extend `tolokaforge/cli/_run_banner.py::print_run_start_banner` with an optional keyword-only `resumed: bool = False`. When `resumed=True`, the first line is `[muted]→[/muted] Resume: {run_id}` (instead of `Run: {run_id}`). The `Report:` line is unchanged. The CLI passes `resumed=True` from the resume branch.
  - End banner is unchanged — success / failure / stopped semantics carry across resumes.
- **Behaviour to lock:**
  - **`--resume` without `--run-dir`** exits with the click usage error message. **Tier: unit** (`tests/unit/test_cli_commands.py::TestRunCommand`, new method).
  - **`--run-dir` without `--resume`** exits with the click usage error message. **Tier: unit**, same file.
  - **`--resume --run-dir <existing>` with all trials `completed`** prints the nothing-to-do line, emits the artifact path on stdout, exits 0, and does **not** import / touch the runtime backend. **Tier: unit**, new `tests/unit/test_cli_resume.py` — monkey-patch `Orchestrator` to a sentinel that fails on construction; assert the sentinel is never called on a complete run-dir.
  - **`--resume --run-dir <existing>` with mixed state** prints `Resuming: X/N completed, K to retry` on stderr before the start banner. **Tier: unit**, same file — capture stderr and assert the exact line + ordering relative to `print_run_start_banner`.
  - **Resume start banner** renders `→ Resume: <run-id>` as its first line. **Tier: canonical**, extend `tests/canonical/test_run_banner_goldens.py` with a `test_start_banner_resume_variant_shape` + a new SVG golden under `tests/canonical/golden/run_banner/start_resume_{80,120}.svg`.
  - **End-to-end: partial run → kill → resume completes only remaining trials.** Uses `examples/native/custom_grading` (mock-mode, `$0` — the same target the dev-loop free tier uses). Approach: monkey-patch `TrialExecutor.execute` to raise `KeyboardInterrupt` after the second successful trial, run once (state file records 2 completed), then invoke `tolokaforge run --resume --run-dir <the dir>` in a subprocess without the monkey-patch. Assert (a) previously-completed trials' `trajectory.yaml` mtime is unchanged, (b) post-resume `run_state.completed_trials == run_state.total_trials`. **Tier: integration**, new `tests/integration/test_run_resume.py` (marker: `integration` — needs a real orchestrator round trip; the free mock example keeps it deterministic and free of external services).
  - **Idempotency: resume on complete run** — same integration test file, second case. Run to completion once, then `tolokaforge run --resume --run-dir <dir>` a second time; assert the "nothing to do" line and that trial artifacts are byte-for-byte unchanged.
- **Compatibility:**
  - CLI surface change (new `--run-dir` flag on `run`). Compatibility surface per AGENTS.md Core Rule 5.
  - Migration: none — flag is additive. Existing invocations continue unchanged. Callers that previously passed `--resume` alone will now see a `UsageError` — this is the fix; the previous behaviour was silently starting a fresh run and is documented as a bug in the plan Context.
  - `print_run_start_banner` gains a keyword-only `resumed=False` — kwargs-with-default is source- and call-compatible.
- **Deliverable:**
  - `tolokaforge/cli/main.py::run` has `--run-dir` and the resume-branch guardrails / summary / no-op exit / banner-variant call.
  - `tolokaforge/cli/_run_banner.py::print_run_start_banner` gains `resumed: bool = False`.
  - `tests/unit/test_cli_resume.py`, `tests/unit/test_cli_commands.py` additions, `tests/canonical/test_run_banner_goldens.py` addition + SVG goldens, `tests/integration/test_run_resume.py`.
  - `docs/CLI.md`: new `## Resume` section (see below); update § run flag table with `--run-dir`.
  - `docs/REFERENCE.md`: replace lines 189–201 (§ Orchestrator) — the block hallucinates `Orchestrator.resume(run_id=...)` and a fake constructor shape. Rewrite as the real `Orchestrator(run_config, resume=True)` + `orchestrator.run(run_id=..., output_dir=...)` pattern, matching current source.
  - `CHANGELOG.md`: entry under `Unreleased / Feat` naming `--run-dir` and the summary/no-op behaviour; entry under `Breaking Changes` noting that `--resume` alone now errors (was silently a fresh start).
- **Validation:**
  - Unit: `uv run pytest tests/unit/test_cli_resume.py tests/unit/test_cli_commands.py tests/unit/test_resume_resolver.py -v -m unit`.
  - Canonical: `uv run pytest tests/canonical/test_run_banner_goldens.py -v -m canonical`. New goldens generated via dev MCP `mcp__dev__update_canonical_snapshots` on the first pass.
  - Integration: `scripts/with_env.sh uv run pytest tests/integration/test_run_resume.py -v -m integration`. Reviewer runs this locally.
  - Grep guards: `rg -n "orchestrator\.resume\("` returns nothing (kills the REFERENCE.md hallucination); `rg -n "resolve_run_directory" tolokaforge/` shows the CLI only calls it in the non-resume branch.
- **Doc updates (rewritten, not appended):**
  - `docs/CLI.md`: new top-level `## Resume` section following `## Dry run`. Content — the flag pair, the summary line format, the no-op message, the resume banner variant, the doc-linked mutex with `--dry-run`, the "budget behaviour" cross-ref to § Cost, time, and sample limits (per-invocation semantics unchanged from #283). No "previously X, now Y" language. Also update § run flag table (§ Dry run's neighbour) to list `--run-dir` with its `requires --resume` contract.
  - `docs/REFERENCE.md`: replace the § Orchestrator block with the current interface (see above). No history note — just the current state.
  - `CHANGELOG.md`: as described in Deliverable.

### Stage 3: Queue-worker path — summary log + docs

- **Contract:**
  - `Orchestrator.prepare_run` (existing) already skips enqueue when the queue is populated and emits a WARNING. Refine that log record to include the same counts shape as the CLI resume summary: `pending`, `running`, `leased`, `completed`, `failed`, `total` — plus a boolean `queue_reused=True`. Log level moves from WARNING to INFO (populated queue is the expected state on resume, not an anomaly). The existing `--reset-queue` flag still forces a wipe.
  - `Orchestrator.run_worker` (existing) already leases only `pending` items. Add one INFO log line at worker start that reads `Worker resuming existing run` (with `run_id`, `pending`, `leased`, `running`, `completed`, `failed`, `total`) when it attaches to a populated queue. When it attaches to an empty queue, log `Worker attached to empty queue` and exit cleanly with zero attempts (unchanged from today).
  - No CLI-flag additions. No changes to `prepare` / `worker` command signatures.
- **Behaviour to lock:**
  - **`prepare_run` on populated queue emits the resume-summary INFO record with counts.** **Tier: unit**, new `tests/unit/test_prepare_resume_summary.py`. Approach: seed a `SqliteRunQueue` with mixed statuses via direct SQL, call `orchestrator.prepare_run(output_dir)`, capture logs, assert the expected fields.
  - **`run_worker` prints the resume banner log on populated queue.** **Tier: unit**, same file — mock the runtime backend, seed the queue, call `run_worker(max_attempts=0)`, assert the log line.
  - **Worker skips completed items and processes only pending on resume.** **Tier: integration**, new `tests/integration/test_worker_resumes_pending_only.py` — seed a queue with 2 completed + 3 pending, run `run_worker`, assert 3 processed and the completed 2 are untouched (queue row status stays `completed`).
- **Compatibility:** internal (log-record shape). Queue schema unchanged. No CLI-surface change.
- **Deliverable:** `tolokaforge/core/orchestrator.py` — refined `prepare_run` log record + new `run_worker` resume-banner log. Test files above. `docs/RUNNER.md` new subsection.
- **Validation:**
  - `uv run pytest tests/unit/test_prepare_resume_summary.py -v -m unit`.
  - `scripts/with_env.sh uv run pytest tests/integration/test_worker_resumes_pending_only.py -v -m integration`.
  - `rg -n "queue_reused|Worker resuming existing run" tolokaforge/` shows the emissions.
- **Doc updates:**
  - `docs/RUNNER.md`: new subsection `### Resuming a queue-worker run` under `## Local Queue Run (SQLite)`. Content — restart `worker --run-dir <existing>`, the queue is source of truth, the resume-summary log to look for, and how `--reset-queue` on `prepare` explicitly wipes. Update `## Distributed Run (Postgres)` step 3 to append "workers are inherently resumable — restart the same `worker` command against the shared Postgres queue and pending attempts are picked up".
  - `docs/CLI.md`: cross-link from `## Resume` to `docs/RUNNER.md § Resuming a queue-worker run`.

## Discovered issues

- **Fix in this PR** (Stage 2 doc updates, no separate commit):
  - `docs/REFERENCE.md:189-201` § Orchestrator — the `Orchestrator(config, output_dir, workers)` constructor shape and `orchestrator.resume(run_id="...")` method are fabricated. Rewrite to the real `Orchestrator(run_config, resume=True)` + `Orchestrator.run(run_id=..., output_dir=...)` pattern.
- **Filed as issues:**
  - [#363](https://github.com/Toloka/tolokaforge/issues/363) — `Orchestrator.run(resume=True)` silently disables resume when `run_state.json` is missing (INFO log, dead-branch fallback). After Stage 2 the CLI catches this, but the programmatic API is still lenient. Low priority follow-up.

## Risks / open questions

- **Integration test kill-and-resume determinism.** The plan pins the "kill" point by monkey-patching `TrialExecutor.execute` to raise `KeyboardInterrupt` after N successful calls. If a future refactor moves the executor factory or changes the exception-propagation path, the test will need adjustment. Mitigation: keep the monkey-patch narrow (module-level attribute swap) with a clear docstring naming the seam.
- **Behavioural-vs-infra failure semantics on resume.** `RunStateManager.is_completed` reads `trajectory.yaml` and inspects for `Error code: 429` / `RateLimitError` / `status == "error"` to distinguish infra-failed (retry) from behavioural-failed (skip). The `describe_resume_plan` "to_retry" count relies on the same predicate. Any change to that classifier (e.g. adding a new transient-failure pattern) will change the summary line. Not a blocker — the classifier already ships. Flagging so reviewers who touch `_has_infrastructure_error` know it now feeds a user-facing counter.
- **`Orchestrator.run(resume=True)` when the resolver already validated the dir.** The orchestrator's own `if not run_state: self.resume = False` branch becomes unreachable from the CLI after Stage 2 but is still valid from the programmatic API. Kept as-is; #363 tracks tightening it later.
- **`--run-dir` type**. `click.Path(exists=True, file_okay=False)` requires the directory to exist at flag-parse time. This is desirable (fail-loud on typos) but means a user cannot pass a not-yet-created dir. That is the correct constraint for `--resume` — the dir is by definition an existing run — but if a future need arises (e.g. `--resume-from-manifest`) it would need a different flag.
