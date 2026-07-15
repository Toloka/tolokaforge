# Plan: A4 — stdout is artifact, stderr is progress

Issue: Toloka/tolokaforge#280 (milestone: Terminal DX, umbrella #297)
Branch: `feat/issue-280-a4-stdout-artifact-stderr-progress` (branches off `feat/terminal-dx`, PR targets `feat/terminal-dx`)

## Context

A1 (#276) shipped `tolokaforge/cli/_display.py::console` as `Console(stderr=True, soft_wrap=True, theme=THEME)` — every CLI module renders through it, so all human-facing Rich output already writes to `sys.stderr`. A3 (#279) shipped `configure_root_logging(..., stream=sys.stderr)` on the top-level `cli` group — every `logging.getLogger(...)` record and every `StructuredLogger` record also lands on `sys.stderr`. `grep -Rn "print(" tolokaforge/cli/ --include="*.py" | grep -v "console.print"` returns zero hits — no bare `print()` exists in the CLI today.

The reserved-but-unused half of the contract is that `stdout` is empty. This is documented in `docs/CLI.md:21`:

> Stderr is the default because the CLI reserves stdout for the machine-parseable artifact path a later stage will introduce; today no CLI command writes to stdout via Rich.

A4 actualises that reservation: on `tolokaforge run` and `tolokaforge prepare` success, the LAST stdout line is the run-dir absolute path — and nothing else. The idiom `RUN_DIR=$(tolokaforge run --config …)` then captures exactly the path, and `2>/dev/null` on the command drops progress without breaking the artifact capture.

Two current gaps block that contract:

1. **`Orchestrator.run()` returns `None`.** The actual timestamped run dir is computed at `tolokaforge/core/orchestrator.py:982` (`output_dir = Path(base_output_dir).parent / run_id` where `run_id = f"{base_name}_{timestamp}"`) but never surfaced back to the caller. `tolokaforge/cli/main.py:378` prints the *base* dir (`run_config.evaluation.output_dir`, e.g. `results/run_20260715_120000`) which does NOT match the on-disk path (`results/run_20260715_120000_<HH>_<MM>_<SS>`) — the current line is misleading UX even before A4.
2. **No stdout emission anywhere in the CLI.** No `sys.stdout.write`, no bare `print`, no `click.echo` (which defaults to stdout). Adding one is a net-new surface.

Adjacent hygiene surfaced by the sweep — one bug fits under A4's contract, filed inline:

- `tolokaforge/cli/main.py:361-363` — on `run` with zero tasks, the command prints `[red]No tasks found![/red]` and `return`s (exit 0). Under A4's "nothing on stdout on failure paths, non-zero exit" clause this is a false-success. Fix in this PR.

Milestone coordination noted in the issue:

- **A5 (#281 dashboard URL banner)** — banner writes to `sys.stderr`, per this contract. A4 does not touch banner rendering; it just publishes the invariant A5 must honour.
- **B2 (#282 `--display` toggle)** — even `--display=none` must NOT suppress the artifact-path stdout emission. The stdout emission is orthogonal to Rich rendering; B2's toggle controls only stderr rendering (progress panel, banner, log lines). Document in B2's plan.
- **B4 (`--dry-run`)** — not implemented yet. When B4 lands, its plan decides `--dry-run` semantics; recommended posture: `--dry-run` emits nothing on stdout (no real run dir exists). Called out as an open note in B4's future plan.
- **`assets stamp`** — A1's note ("A4 will re-carve stdout for machine-parseable artifact paths") is deferred to a follow-up issue. Rationale: (a) `assets stamp`'s "artifact" is not a single run dir — it's the in-place-modified `project.yaml`, whose path the caller already knows; (b) issue #280 scopes to `run` / `prepare`. Filed as a follow-up so the promise doesn't rot.

## Goal

Publish and enforce this stdout/stderr contract for `tolokaforge` — locked by tests, documented in `docs/CLI.md`, and grep-guarded so a new `print(` in `tolokaforge/cli/**/*.py` fails CI.

**Contract:**

| Command | stdout on success | stdout on failure | stderr |
|---------|-------------------|-------------------|--------|
| `run` | Exactly one line: absolute run-dir path (`Path(output_dir).resolve()`). No trailing content after it. | Empty. | Rich progress, banners, log records, error text. Exit code non-zero on failure. |
| `prepare` | Exactly one line: absolute run-dir path (`Path(run_dir).resolve()`). | Empty. | Rich summary, log records. Exit code non-zero on failure. |
| `worker` | Empty. (No new artifact — consumes an existing run dir.) | Empty. | Unchanged; existing summary lines stay on stderr. |
| `status` / `validate` / `config validate` / `assets stamp` / `docker *` / `adapter convert` / `analyze` | Empty. | Empty. | Unchanged; existing human-readable output stays on stderr as A1 established. |

**Emission mechanism:**

A new helper in `tolokaforge/cli/_display.py`:

```python
def emit_artifact_path(path: Path | str) -> None:
    """Write the resolved absolute path to sys.stdout with a trailing
    newline, flushed. This is the ONE stdout write the CLI is allowed —
    every human/progress/log line goes through the shared `console`
    (stderr) or `configure_root_logging` (stderr). Callers pass either a
    Path or a string; the helper calls Path(..).resolve() so the emitted
    line is always absolute and canonicalised. Never colours the line;
    never adds prefix text; the whole line is the path."""
    print(str(Path(path).resolve()), file=sys.stdout, flush=True)
```

Rationale for a named helper over a bare `print(path)`:

- **Grep-guardable.** Stage 3 adds a canonical test that forbids any `print(` in `tolokaforge/cli/**/*.py` — with `_display.py` exempt. A bare `print(path)` at every call site would either force a per-site whitelist (fragile) or an unnamed exception ("this print is allowed because it's an artifact"). A single helper makes intent obvious and the guard trivial.
- **Absolute-path canonicalisation lives in one place.** Every future consumer (`assets stamp` when its emission lands, `runs list` in C1, etc.) reuses the same resolution semantics.
- **`flush=True`** matters for shell composition — pipes must see the line before the process exits.

**`Orchestrator.run()` return-type change:**

Change `Orchestrator.run(self) -> None` to `Orchestrator.run(self) -> Path`, returning the resolved absolute path of the timestamped run dir it created. Callers ignoring the return value are unaffected (Python allows dropping a return value silently). `tolokaforge/cli/main.py:375` becomes `output_dir = orchestrator.run()` and Stage 1 uses it for the emission.

**This IS a Python API compatibility surface.** `docs/API.md:5-12` documents `Orchestrator` with the exact snippet `results = orchestrator.run()` and `tolokaforge/__init__.py:55` re-exports `Orchestrator` in `__all__`. The return-type change is a *widening* — callers ignoring the return value continue to work (Python allows dropping a return value silently) — but embedders assigning `results = orchestrator.run()` will now hold a `Path` instead of `None`. Stage 3 updates the API doc example and CHANGELOG lists this under BREAKING (widening return-type). Existing unit tests (`tests/unit/test_orchestrator_logic.py::TestRunOutputDirBasenameGuard`) only exercise raise-on-bad-input behaviour and are unaffected. `tolokaforge/cli/main.py:375` is the only production caller.

## Non-goals

- **Adding `--display` (B2/#282) or `--dry-run` (B4).** A4 publishes the stdout invariant those flags must honour; it does not implement either. B2's plan will note that `--display=none` must not suppress the artifact-path emission.
- **`assets stamp` machine-parseable stdout carve-out.** Deferred to a follow-up issue (see "Discovered issues"). A4 stays scoped to `run` / `prepare`.
- **`worker` stdout emission.** Worker consumes an existing run dir supplied via `--run-dir`; there is no new artifact to publish. Current summary stays on stderr; stdout stays empty.
- **Changing existing stderr output.** No colour edits, no line-shape changes, no A5 banner work. A4 adds the stdout emission and locks the invariant; everything else on stderr stays as A1 + A3 left it.
- **Rewiring `Orchestrator.prepare_run` return value.** It already returns a dict, and `prepare`'s CLI knows the `run_dir` from `--run-dir` — no orchestrator change needed for prepare.
- **Removing `StructuredLogger`, changing `logs.yaml` output, or touching runner-container logging.** Out of scope; A3 covered the stdlib route and filed #308 / #309.
- **`tolokaforge/env/`, `tolokaforge/runner/__main__.py`, `contrib/`.** Separate processes with their own stdio; A4's contract binds `tolokaforge` CLI invocations only.

## Target module surface

```python
# tolokaforge/cli/_display.py — new export

def emit_artifact_path(path: Path | str) -> None:
    """See rationale in the plan; the ONE sanctioned stdout write."""
    print(str(Path(path).resolve()), file=sys.stdout, flush=True)


__all__ = ["THEME", "console", "emit_artifact_path", "make_live", "make_progress"]
```

```python
# tolokaforge/core/orchestrator.py — signature change

def run(self) -> Path:
    ...
    output_dir = Path(base_output_dir).parent / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    ...
    self._generate_reports(output_dir)
    return output_dir.resolve()   # <-- new return statement
```

```python
# tolokaforge/cli/main.py — run command tail

output_dir = orchestrator.run()
console.print("[bold green]✓ Run complete![/bold green]")
console.print(f"Results saved to: {output_dir}")
emit_artifact_path(output_dir)
```

```python
# tolokaforge/cli/main.py — prepare command tail

summary = orchestrator.prepare_run(Path(run_dir), reset_queue=reset_queue)
...
console.print("[bold green]✓ Run queue prepared[/bold green]")
console.print(f"queued={summary['queued_attempts']} ...")
emit_artifact_path(run_dir)
```

## Design decisions

### D1. Helper name and location — `_display.emit_artifact_path`

`_display.py` already owns the CLI's I/O primitives (`console`, `make_progress`, `make_live`). The artifact-path emission is the *counterpart* of `console.print`: `console.print` → stderr; `emit_artifact_path` → stdout. Living next to each other makes the surface obvious. `_display` is already `__all__`-exported and grep-guard-exempt.

Alternative rejected: a bare `print(str(output_dir), file=sys.stdout, flush=True)` at each call site. Loses the guardable "everything else must go through console" invariant; forces per-site whitelisting in the Stage 3 canonical test.

### D2. Orchestrator.run() returns `Path`

Rejected alternatives:

- **Store on `self.output_dir`.** Mutable per-invocation state on a long-lived object; reads and writes need explicit protocol.
- **Emit inside `Orchestrator.run()`.** Puts a CLI concern (stdout stream) in the engine. The engine also runs from the pytest process during integration tests, where a stray stdout write would leak into test output. Keep stdout emission at the CLI boundary.

`Orchestrator.prepare_run()` already returns a dict; `run()` returning `Path` parallels it. Single caller (`tolokaforge/cli/main.py:375`) updated in Stage 1.

### D3. `Path.resolve()` is called by the helper, not the callers

`Path.resolve()` canonicalises symlinks and always returns an absolute path. Doing it inside `emit_artifact_path(...)` means every caller gets the same guarantee (issue AC: "absolute path"). Callers pass either a `Path` or a `str` — the helper accepts both to keep call sites terse (`emit_artifact_path(run_dir)` where `run_dir: str` comes from the click `--run-dir` argument).

### D4. Fail loud on the "no tasks" path

Current behaviour (`tolokaforge/cli/main.py:361-363`) prints `"No tasks found!"` and `return`s — exit code 0. This is a false-success: the stdout contract would then say "one line = success" but there's no run dir on disk, no artifact. Convert to:

```python
if not orchestrator.tasks:
    console.print("[red]No tasks found![/red]")
    raise SystemExit(1)
```

`SystemExit(1)` exits before the `emit_artifact_path(...)` line runs, so stdout stays empty on this failure path. Fits `AGENTS.md` Core Rule 1 ("Surface failures explicitly").

### D5. `worker` command has no stdout emission

Worker consumes an existing queue from an operator-supplied `--run-dir`. No new artifact is produced; the caller already knows the path. Emitting `--run-dir` on stdout would be misleading (implies "worker produced this") and forces callers into `2>&1` to see the "processed=N completed=M failed=K" summary which is the actually-interesting information.

Documented in the stdout/stderr contract table (`docs/CLI.md` update in Stage 3).

### D6. Where does the emit-line sit relative to existing "Run complete" / "Results saved to" lines?

Order in the `run` command tail:

1. `orchestrator.run()` returns.
2. `console.print("✓ Run complete!")` — stderr.
3. `console.print(f"Results saved to: {output_dir}")` — stderr. (Kept; a human running `tolokaforge run` interactively sees this on their terminal.)
4. `emit_artifact_path(output_dir)` — stdout, LAST.

The issue AC requires "the last stdout line" to be the path. Since stdout only ever gets one line (from the helper), this is trivially satisfied. The stderr lines above are irrelevant to the AC and stay for human ergonomics.

## Stages

Every stage lands as one commit, has its own tests that would fail without the stage, and updates the docs that describe *current state*.

### Stage 1: `emit_artifact_path` + `Orchestrator.run()` return type + `run` command wiring + fail-loud on no-tasks

- **Contract:**
  - New `emit_artifact_path(path: Path | str) -> None` in `tolokaforge/cli/_display.py`; added to `__all__`.
  - `Orchestrator.run(self) -> Path` returns the resolved absolute path of the run dir it created. All other behaviour unchanged.
  - `tolokaforge run` calls `output_dir = orchestrator.run()` and `emit_artifact_path(output_dir)` as the last line of the successful path.
  - `tolokaforge run` raises `SystemExit(1)` on the "no tasks" branch (`tolokaforge/cli/main.py:361-363`) instead of silently `return`-ing.
- **Behaviour to lock:**
  - **tier `unit`, `tests/unit/test_cli_stdout_contract.py::TestRunStdoutContract` (new file):**
    - `test_run_success_stdout_is_single_resolved_path` — `CliRunner(mix_stderr=False)` invokes `run --config <fixture>`; monkeypatch stubs `Orchestrator.__init__` to no-op, stubs `Orchestrator.load_tasks` to set `self.tasks = [<one fake>]`, and stubs `Orchestrator.run` to `mkdir` a `tmp_path / "results" / "run_xxx"` and return it. Assert: `result.exit_code == 0`; `result.stdout.count("\n") == 1`; `Path(result.stdout.strip()).is_absolute()`; `Path(result.stdout.strip()) == (tmp_path / "results" / "run_xxx").resolve()`; `Path(result.stdout.strip()).is_dir()`.
    - `test_run_failure_stdout_is_empty_on_bad_config` — `runner.invoke(cli, ["run", "--config", "/nonexistent/config.yaml"])`. Assert: `result.exit_code != 0`; `result.stdout == ""`.
    - `test_run_failure_stdout_is_empty_on_orchestrator_raise` — same stubbed harness but `Orchestrator.run` raises `RuntimeError("boom")`. Assert: `result.exit_code != 0`; `result.stdout == ""`.
    - `test_run_failure_stdout_is_empty_on_no_tasks` — stubs return an empty task list. Assert: `result.exit_code == 1`; `result.stdout == ""`; `result.stderr` contains `"No tasks found!"`.
    - `test_run_stdout_line_has_no_ansi_no_markup` — same happy-path stub; assert `"\x1b" not in result.stdout`, `"[" not in result.stdout` (no leftover Rich markup). Locks that the helper never routes through the shared `console`.
  - **tier `unit`, `tests/unit/test_cli_display.py::TestEmitArtifactPath` (extends existing file):**
    - `test_emit_artifact_path_writes_to_stdout(capsys, tmp_path)` — `emit_artifact_path(tmp_path)`; `captured = capsys.readouterr()`; assert `captured.out == str(tmp_path.resolve()) + "\n"`; `captured.err == ""`.
    - `test_emit_artifact_path_resolves_relative(monkeypatch, capsys, tmp_path)` — `monkeypatch.chdir(tmp_path); emit_artifact_path("results/run")` → captured.out is `(tmp_path / "results" / "run").resolve()` string + newline. (Resolves are done regardless of whether the dir exists on disk, per `Path.resolve()` semantics on Python 3.6+.)
    - `test_emit_artifact_path_accepts_string(capsys, tmp_path)` — pass `str(tmp_path)`; identical to the Path form.
    - `test_emit_artifact_path_is_flushed(capfd)` — pytest's `capfd` (fd-level capture) records stdout without colliding with `capsys` and preserves flush behaviour. Assert `capfd.readouterr().out.strip()` equals the emitted path AND that a trailing newline is present. (Rationale: fd-level capture is more robust than replacing `sys.stdout` — no test-order sensitivity, no `capsys` interference. `capfd` is the locked choice.)
  - **tier `unit`, `tests/unit/test_orchestrator_logic.py::TestRunReturnsResolvedPath` (extends existing file):**
    - **Dropped** — the orchestrator-level `test_run_returns_resolved_absolute_path` was going to require monkeypatching a much wider stub surface than `TestRunOutputDirBasenameGuard` uses (agent/user/adapter/RunStateManager/LLMClient/Conductor/`_generate_reports`), drifting into an integration test. Instead, lock the return-type contract at the CLI boundary via the existing Stage 1 `test_run_success_stdout_is_single_resolved_path` — it invokes `tolokaforge run --config ...` end-to-end and asserts the emitted stdout path equals the on-disk timestamped dir. That test would fail if `Orchestrator.run()` returned `None` or a non-resolved path. No coverage gap.
    - (The existing basename-guard tests keep passing — `pytest.raises(ValueError)` fires before any return.)
- **Compatibility:**
  - **`Orchestrator.run()` signature change.** Not a compatibility surface per `docs/API.md` (which documents `tolokaforge.core.{grade, engine, ...}` helpers, not `Orchestrator` class methods). Single production caller updated in this stage. Called out in CHANGELOG in Stage 3 anyway (a "Notes for embedders" line) for anyone wrapping `Orchestrator` in their own tooling.
  - **CLI stdout output.** New surface — machine-parseable. Contract locked here and in Stage 3 docs / grep-guard.
  - **"No tasks" now exits non-zero.** Behaviour change on a currently-degenerate path. Called out in CHANGELOG.
- **Deliverable:**
  - `tolokaforge/cli/_display.py` — adds `emit_artifact_path`, extends `__all__`.
  - `tolokaforge/core/orchestrator.py` — changes `run(self)` signature to `-> Path` and adds `return output_dir.resolve()` at the end of the method.
  - `tolokaforge/cli/main.py` — captures return value at line 375; adds `emit_artifact_path(output_dir)` after the "Results saved to:" line; converts the "No tasks found!" branch to `raise SystemExit(1)`.
  - `tests/unit/test_cli_stdout_contract.py` (new).
  - `tests/unit/test_cli_display.py` — new `TestEmitArtifactPath` class.
  - (No new `test_orchestrator_logic.py` test — see the "Dropped" note in Behaviour to lock; CLI-boundary test is sufficient.)
- **Validation:**
  - `dev.run_tests(marker="unit", pattern="test_cli_stdout_contract or test_cli_display or test_orchestrator_logic")` green.
  - `dev.lint_check(paths=["tolokaforge/cli", "tolokaforge/core/orchestrator.py", "tests/unit"])` clean.
  - **Zero-tasks safety sweep** — `rg -n "tolokaforge run" .github/ examples/ docs/ scripts/` to spot any invocation that expects an empty task set as OK ("nothing to do, exit 0"). If any callers surface, decide whether the exit-1 conversion is still the right call. (Expected outcome: no callers rely on the silent-0 behaviour; the fail-loud change stands.)
  - Manual smoke — implementer quotes the output in the PR body:
    - `uv run tolokaforge run --config examples/native/tool_use/run_config.yaml 2>/dev/null | tee /tmp/a4-stdout.log && echo "---" && wc -l /tmp/a4-stdout.log` — one line; the line is a resolvable dir. (Uses a real model — sanity-check only; skip if no API keys.)
- **Doc updates:** none yet (docs land in Stage 3).

### Stage 2: `prepare` command wiring + status/validate no-change verification

- **Contract:**
  - `tolokaforge prepare` calls `emit_artifact_path(run_dir)` as the last line of its successful path.
  - `status`, `validate`, `config validate`, `assets stamp`, `docker *`, `adapter convert`, `analyze` — no code changes; existing behaviour verified.
- **Behaviour to lock:**
  - **tier `unit`, `tests/unit/test_cli_stdout_contract.py::TestPrepareStdoutContract`:**
    - `test_prepare_success_stdout_is_single_resolved_path` — `CliRunner(mix_stderr=False)` invokes `prepare --config <fixture> --run-dir <tmp>`; monkeypatch stubs `Orchestrator` similarly to Stage 1, with `prepare_run` returning a summary dict. Assert: `result.exit_code == 0`; `result.stdout.count("\n") == 1`; `Path(result.stdout.strip()) == Path(run_dir).resolve()`; `Path(result.stdout.strip()).is_absolute()`.
    - `test_prepare_failure_stdout_is_empty_on_bad_config` — invalid `--config`; `result.exit_code != 0`; `result.stdout == ""`.
    - `test_prepare_failure_stdout_is_empty_on_orchestrator_raise` — `Orchestrator.prepare_run` raises; `result.exit_code != 0`; `result.stdout == ""`.
  - **tier `unit`, `tests/unit/test_cli_stdout_contract.py::TestReadOnlyCommandsStdoutIsEmpty`:**
    - `test_status_stdout_is_empty` — run `status --run-dir <tmp>` against a run dir containing a synthetic `run_state.json`; assert `result.stdout == ""`; stderr contains the run summary lines.
    - `test_validate_stdout_is_empty` — run `validate --tasks <bad-glob>`; assert `result.stdout == ""`; stderr contains `"0 valid"`.
    - `test_config_validate_stdout_is_empty` — run `config validate --config <bad>`; assert `result.stdout == ""`.
    - `test_assets_stamp_stdout_is_empty_on_all_paths` — run `assets stamp` against a fixture project with no seeds (success), then against a missing project.yaml (failure); assert `result.stdout == ""` in both cases. (Nothing to break — A1 moved all its output to stderr; the test locks that A4 didn't accidentally regress the split.)
    - `test_worker_stdout_is_empty` — invoke `worker --config <fixture> --run-dir <tmp>` with stubbed `Orchestrator.run_worker` returning a summary; assert `result.stdout == ""`; stderr has the "Worker complete" line.
    - `test_adapter_convert_stdout_is_empty` — invoke `adapter convert` on a stub; assert `result.stdout == ""`.
    - `test_docker_status_stdout_is_empty` — invoke `docker status` with the docker SDK unavailable path (import error); assert `result.stdout == ""`.
  - **tier `unit`, `tests/unit/test_orchestrator_logic.py::TestPrepareRunReturns`:**
    - Existing `prepare_run` returns a dict — no signature change. No new test needed here; the current tests suffice.
- **Compatibility:**
  - New `prepare` stdout surface — same contract as Stage 1.
  - Read-only commands unchanged; the tests are regression nets, not surface changes.
- **Deliverable:**
  - `tolokaforge/cli/main.py` — adds `emit_artifact_path(run_dir)` at the end of the `prepare` command body.
  - `tests/unit/test_cli_stdout_contract.py` — new `TestPrepareStdoutContract` and `TestReadOnlyCommandsStdoutIsEmpty` classes (extends the file from Stage 1).
- **Validation:**
  - `dev.run_tests(marker="unit", pattern="test_cli_stdout_contract")` green.
  - `dev.lint_check(paths=["tolokaforge/cli", "tests/unit"])` clean.
  - Manual smoke (implementer quotes in PR body): `uv run tolokaforge prepare --config examples/native/tool_use/run_config.yaml --run-dir /tmp/a4-prep 2>/dev/null | tee /tmp/a4-prep-stdout.log && wc -l /tmp/a4-prep-stdout.log`.
- **Doc updates:** none yet (Stage 3).

### Stage 3: Canonical grep-guard + `docs/CLI.md` stdout/stderr contract section + CHANGELOG

- **Contract:**
  - A canonical test in `tests/canonical/test_cli_display_invariants.py` extends the existing `_no_ad_hoc_console_in_cli` guard family with a new invariant that forbids `print(` and `sys.stdout.write(` in `tolokaforge/cli/**/*.py` outside `_display.py`. (The docstring inside `_display.py` names `emit_artifact_path` as the sanctioned mechanism.)
  - `docs/CLI.md` gains a `## stdout / stderr contract` section documenting the table published under "Contract" above.
  - `CHANGELOG.md` gets an "Unreleased" entry describing the new stdout surface and the `Orchestrator.run()` return-type change.
- **Behaviour to lock:**
  - **tier `canonical`, `tests/canonical/test_cli_display_invariants.py::test_no_bare_stdout_write_in_cli` (new test in existing file):**
    - Walk `tolokaforge/cli/**/*.py`. For each file except `_display.py` and `__init__.py`, assert no line matches the regex `(?:^|\s)(print\s*\(|sys\.stdout\.write\s*\()`. (Leading whitespace or start-of-line prevents false-positives inside strings that mention `print(` — same posture as the existing `_CONSOLE_CALL` regex.)
    - Failure message names every offending `file:line`.
    - Positive control: `_display.py` contains one `print(` (inside `emit_artifact_path`) and is not counted, since the file is in `_EXEMPT_FILES`.
  - **tier `canonical`, `tests/canonical/test_cli_display_invariants.py::test_emit_artifact_path_is_exported` (new):**
    - `from tolokaforge.cli._display import emit_artifact_path` succeeds and `callable(emit_artifact_path)`. Locks the module's public surface alongside the existing `test_display_module_exports_public_surface`.
- **Compatibility:**
  - **`docs/CLI.md` stdout/stderr section is a compatibility surface** (shell-composition contract). The section is a table; any future change (e.g. adding a stdout column to `worker`) needs a CHANGELOG entry.
  - **Grep-guard** is internal only.
- **Deliverable:**
  - `tests/canonical/test_cli_display_invariants.py` — two new tests, extends `_EXEMPT_FILES` if needed.
  - `docs/CLI.md` — new `## stdout / stderr contract` section after `## Structured logging`. Written as current state (per `AGENTS.md` Core Rule 8) — no "previously X" framing.
  - `docs/CLI.md:21` — the existing "the CLI reserves stdout for the machine-parseable artifact path a later stage will introduce" line is rewritten (the "later stage" language is stale). New wording: "Stderr is the default because stdout is reserved for machine-parseable artifact paths — see § stdout / stderr contract."
  - `docs/API.md:5-12` — update the `Orchestrator` snippet's `results = orchestrator.run()` line to reflect the new return type. New wording: `run_dir = orchestrator.run()  # returns the resolved Path of the timestamped run dir`. Confirms embedders see the widening.
  - `CHANGELOG.md` — Unreleased entries:
    - "**cli**: `tolokaforge run` and `tolokaforge prepare` emit the absolute run-dir path as a single line on `sys.stdout` on success. Read-only commands (`status`, `validate`, `config validate`, `assets stamp`, `worker`, `adapter convert`, `analyze`, `docker *`) leave `sys.stdout` empty. Idiom: `RUN_DIR=$(tolokaforge run --config …)`. See docs/CLI.md § stdout / stderr contract. (#280)"
    - "**Notes for embedders**: `Orchestrator.run()` now returns the resolved `Path` of the run dir it created (previously `None`). Callers that ignore the return value are unaffected."
    - "**Behaviour change**: `tolokaforge run` with zero tasks now exits with code 1 (previously exited 0 with a red 'No tasks found!' line on stderr)."
- **Validation:**
  - `dev.run_tests(marker="canonical", pattern="test_cli_display_invariants")` green.
  - `uv run pytest tests/unit tests/canonical -x -m "unit or canonical"` full unit + canonical suites green.
  - `dev.lint_check` and `dev.format_check` clean.
  - `rg "later stage will introduce" docs/` → zero hits (stale phrasing removed).
  - `rg "print\(" tolokaforge/cli/ --include='*.py'` → one hit inside `_display.py::emit_artifact_path`.
- **Doc updates:**
  - `docs/CLI.md` — new section:

    ```markdown
    ## stdout / stderr contract

    `tolokaforge` splits streams by purpose: stdout carries the
    machine-parseable artifact identifier; stderr carries everything
    a human reads (progress, banners, log records, error text).

    | Command                     | stdout on success                            | stderr                                         |
    |-----------------------------|----------------------------------------------|------------------------------------------------|
    | `run`                       | Absolute run-dir path (single line).         | Progress, log records, "Run complete" banner.  |
    | `prepare`                   | Absolute run-dir path (single line).         | Queue summary, log records.                    |
    | `worker`                    | (empty)                                      | "Worker complete" summary, log records.        |
    | `status`, `validate`,       | (empty)                                      | Human-readable output.                         |
    | `config validate`,          |                                              |                                                |
    | `assets stamp`,             |                                              |                                                |
    | `adapter convert`,          |                                              |                                                |
    | `analyze`, `docker *`       |                                              |                                                |

    On any failure — bad config, orchestrator raise, zero tasks — stdout stays
    empty and the process exits non-zero.

    The idiom `RUN_DIR=$(tolokaforge run --config …)` captures the artifact
    path; `2>/dev/null` drops progress without breaking the capture. The
    artifact-path emission is unaffected by `--verbose` / `--quiet` /
    `--log-format` (those bind stderr line shape only).

    The single stdout write goes through `emit_artifact_path` in
    `tolokaforge.cli._display`; a canonical test forbids any other
    `print(` or `sys.stdout.write(` in `tolokaforge/cli/**/*.py`.
    ```

## Test strategy

- **`CliRunner(mix_stderr=False)`** is mandatory for every stdout-contract assertion. Existing tests in `tests/unit/test_cli_commands.py` already use this pattern.
- **Monkeypatching `Orchestrator`** for `run` / `prepare` avoids real LLM calls and Docker startup. Concretely: `monkeypatch.setattr("tolokaforge.cli.main.Orchestrator", <FakeOrchestrator>)` where `FakeOrchestrator` no-ops `__init__`, `load_tasks`, and returns a stub `Path` from `run()`. Fast, deterministic, hermetic.
- **`capsys` for the `emit_artifact_path` unit tests** — the helper writes via `print(..., file=sys.stdout)`, which `capsys.readouterr().out` captures cleanly.
- **`.flush()` assertion via a spy stdout** — replace `sys.stdout` with an object that records `.write` and `.flush` calls; assert `.flush()` fires exactly once. This is the only way to lock the shell-pipeline flush invariant without spinning up a subprocess.
- **Real-orchestrator smoke** is manual (implementer quotes in PR body) rather than an integration test — the `unit` suite must stay fast and hermetic. If a real smoke matters (it does — the whole point is the shell idiom), the follow-up integration should run under the existing `dev-loop-tolokaforge` skill against `examples/native/custom_grading` (mock $0). Recommend adding as a comment in Stage 2's PR body, not as a new pytest file — the `unit` tier already covers the contract mechanically.
- **Absence-of-side-effects**: every stdout-contract test asserts `result.stdout == ""` (empty string, not merely "does not contain X"). This locks *silence* — an accidental progress print sneaking through would show up as a mismatched string comparison.

## Discovered issues

- **Fix in this PR (Stage 1):**
  - `tolokaforge/cli/main.py:361-363` — `if not orchestrator.tasks: return` silently exits with code 0. Convert to `raise SystemExit(1)` so the "no tasks" path is a genuine failure (stdout empty, non-zero exit). Fits `AGENTS.md` Core Rule 1 (surface failures explicitly) and A4's contract ("nothing on stdout on failure paths, non-zero exit"). Called out in CHANGELOG.
  - `tolokaforge/cli/main.py:337` — line reads `Output directory: {run_config.evaluation.output_dir}` which is the *base* dir, not the timestamped run dir the orchestrator will actually create. After the Stage 1 refactor, `orchestrator.run()` returns the resolved path — but the earlier line is emitted *before* `run()` is called. Fix: rewrite the pre-run line to `f"Output base: {run_config.evaluation.output_dir}"` (accurate — the timestamped suffix comes from the orchestrator), and after the run, keep the existing `"Results saved to:"` line pointing at the returned path. Small phrasing fix; keep in Stage 1 PR body as a call-out.
- **Filed as follow-up issues:**
  - **#315** — `cli(assets): machine-parseable stdout carve-out for \`assets stamp\` (deferred from A4/#280)`. A1's promise ("A4 will re-carve stdout for machine-parseable artifact paths") is deferred: `assets stamp`'s artifact is the in-place-modified `project.yaml`, not a run dir; the design decision (emit path? emit digests? emit nothing?) is non-trivial and worth its own issue.
  - **#316** — `cli(worker): decide stdout emission semantics for \`tolokaforge worker\` (deferred from A4/#280)`. Worker consumes an existing run dir; no new artifact to publish. The current A4 posture (empty stdout, summary on stderr) is defensible but downstream tools may want a machine-consumable "worker done" signal — filed so the design decision has a home.
- **Not filed (rejected):**
  - "Rename subcommand `--verbose` to `--trial-verbose`" — surfaced in A3's plan as future hygiene; not A4 territory.
  - "Delete `Orchestrator.run()`'s side effect of `console.print` in the `if verbose:` / `if strict:` branches at `main.py:339-342`" — these are pre-`orchestrator.run()` announcements, not the artifact line. Fine on stderr.

## Risks / open questions

- **`Orchestrator.run()` return-type change.** IS documented in `docs/API.md:5-12` with the exact snippet `results = orchestrator.run()` and re-exported from `tolokaforge/__init__.py:55` — a real Python API surface. Widening the return type from `None` to `Path` is backwards-compatible for callers that ignore the result, but embedders assigning the result now hold a `Path` instead of `None`. Stage 3 updates the API doc snippet AND the CHANGELOG lists the change under BREAKING (widening return-type; explicit "Notes for embedders" line). Grep confirms `tolokaforge/cli/main.py:375` is the only in-tree caller.
- **Shell composition subtleties.** `RUN_DIR=$(cmd)` strips a single trailing newline (POSIX shell behaviour). `emit_artifact_path` emits exactly one `\n`, so `RUN_DIR` is the bare path. If a caller does `for f in $(cmd)` the single-word path also works — no whitespace in the emitted path unless the operator picked one, which is their own choice.
- **`Path.resolve()` on a symlinked run dir.** If `evaluation.output_dir` points at a path *inside* a symlinked directory, `resolve()` returns the canonical target. This is intentional (a caller that `cd`s to the emitted path lands at the real dir). Documented in the `docs/CLI.md` section: "the emitted path is `Path.resolve()`'d — symlinks are canonicalised."
- **`--dry-run` interaction (B4/future).** Recommended posture: `--dry-run` emits nothing on stdout (no real dir exists). B4's plan owns the final call. Documented as an open note in this plan.
- **`--display=none` interaction (B2/#282).** B2 must NOT gate `emit_artifact_path`. The helper is orthogonal to display mode — display mode picks the stderr renderer; the stdout emission is unconditional on success. Document explicitly in B2's plan (main takes this back to B2 planning when B2 comes up).
- **Test isolation and stdout capture.** Locked on pytest's `capfd` (fd-level capture) — captures below the `sys.stdout` object, immune to `capsys` collision and test-order sensitivity, and preserves flush semantics. `test_emit_artifact_path_is_flushed(capfd)` asserts `capfd.readouterr().out.strip()` equals the path and a trailing newline is present. Implementer does NOT choose between `monkeypatch(sys.stdout)` and `capfd` — the plan chose.
- **Windows line-endings.** `print()` in Python 3 emits `\n` regardless of platform when writing to a text stream; the newline-translation happens in the OS layer. Tests should assert `.strip()` semantics rather than exact `"\n"` bytes if the CI matrix ever includes Windows. (Today tolokaforge CI is Linux-only; assert `.count("\n") == 1` on Linux is fine.)
- **The `print()` in `_display.py` uses stdlib `print`, not `console.print`.** By design: `console` is bound to stderr. If a future refactor accidentally aliases `emit_artifact_path` to route through `console`, the artifact would land on stderr — the grep-guard forbidding `print(` outside `_display.py` explicitly permits the one inside. The `test_emit_artifact_path_writes_to_stdout` test locks the routing.
- **`_display.py` `console` name shadowing.** `_display.py` already has a module-level `console` (of type `Console`). Adding `emit_artifact_path` doesn't collide; the print inside it uses `sys.stdout` explicitly. No shadowing.
