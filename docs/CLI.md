# CLI reference

This document describes the tolokaforge CLI's shared building blocks. Per-command usage lives in `--help` output.

The CLI is the reference implementation of the `RunDisplayEvents` seam (see [ADR-0019](architecture/adr/0019-front-end-plugin-namespace.md)); it ships under the `tolokaforge.dx` namespace and installs via `pip install 'tolokaforge[dx]'`.

## Display layer

Every module under `tolokaforge/dx/` renders human-facing output through `tolokaforge.dx._display`. The module owns four public names — `console`, `THEME`, `make_progress`, and `make_live` — and no CLI file may construct its own `rich.Console`. A canonical test (`tests/canonical/test_cli_display_invariants.py`) fails CI if a new module slips in an ad-hoc `Console(...)`.

### Why a shared surface

The Click command tree splits across `tolokaforge/dx/cli/main.py` plus per-verb modules `adapter.py`, `assets.py`, `config.py`, `docker.py`. Routing them all through one `Console` guarantees a single theme, a single stream posture (stderr, soft-wrapped), and one place for the `--display` toggle to mutate progress and Live-panel plumbing (see [§ Display modes](#display-modes)).

### `console`

```python
from tolokaforge.dx._display import console

console.print("[success]✓ done[/success]")
```

`console` writes to **stderr** with `soft_wrap=True` and the semantic palette installed. Stderr is the default because stdout is reserved for machine-parseable artifact paths — see [§ stdout / stderr contract](#stdout--stderr-contract). Soft-wrapping preserves long paths and digests in narrow CI terminals.

Use `console.print(...)` for one-shot lines. Use the factories below when you need progress bars or a persistent Live region.

### `THEME` — semantic token palette

`THEME` is a `rich.theme.Theme` mapping seven tokens to Rich styles. Prefer semantic markup (`[success]…[/success]`) over ad-hoc colour markup (`[green]…[/green]`) so future palette tuning propagates automatically.

| Token     | Style              | When to use                                                     |
|-----------|--------------------|-----------------------------------------------------------------|
| `info`    | `cyan`             | Hints and status lines.                                         |
| `warn`    | `yellow`           | Warnings that don't abort the command.                          |
| `error`   | `bold red`         | Errors — louder than default red so the user cannot miss them.  |
| `success` | `green`            | Success markers (`✓`, completion summaries).                    |
| `muted`   | `dim`              | Secondary detail — file paths, digests, ids.                    |
| `cost`    | `bold magenta`     | Money and token counts.                                         |
| `link`    | `underline cyan`   | `file://` URLs and other openable references.                   |

All styles use Rich standard names (no truecolor hex) so terminals without full-colour support degrade cleanly.

### `make_progress(...)`

Factory returning a `rich.progress.Progress` bound to the shared console with the CLI's default column set: `SpinnerColumn`, description, `BarColumn`, `MofNCompleteColumn`, `TaskProgressColumn`, `TimeElapsedColumn`, `TimeRemainingColumn`.

```python
from tolokaforge.dx._display import make_progress

with make_progress() as progress:
    task = progress.add_task("Grading trials", total=42)
    for trial in trials:
        run(trial)
        progress.advance(task)
```

Pass `columns=[...]` to override the default set entirely. Pass `disable=True` to render nothing (useful when a caller multiplexes output via a Live region). `redirect_stdout` / `redirect_stderr` default to `True` — set them to `False` when a caller manages its own stream capture.

### `make_live(...)`

Factory returning a `rich.live.Live` bound to the shared console. Use it when a region of the terminal needs to update in place (dashboards, run summaries).

```python
from rich.text import Text
from tolokaforge.dx._display import make_live

with make_live(Text("Starting…")) as live:
    live.update(Text("Working…"))
```

Defaults: `refresh_per_second=4.0`, `transient=False`, `screen=False`, `vertical_overflow="ellipsis"`.

### Choosing between `console.print`, `make_progress`, and `make_live`

- **One-shot line** (banner, error, single result): `console.print(...)`.
- **Iteration with a known total** (per-trial grading, batch uploads): `make_progress()`.
- **Persistent status region that repaints in place** (multi-line dashboard, live cost tracker): `make_live()`.
- **Run-scoped multi-region panel** (per-trial list + summary + status bar during `tolokaforge run`): `LiveRunDisplay` in `tolokaforge.dx.live_panel`. Wraps `make_live` and consumes a `RunDisplayEvents` Protocol threaded through `OrchestratorDeps.events` — see [§ Display modes](#display-modes).

Nested progress inside a Live region is supported by Rich — pass the same `console` (the default) and Rich cooperates automatically.

## Structured logging

Three flags on the top-level `tolokaforge` group control every log line the CLI emits. `configure_root_logging` runs from the group callback before any subcommand executes, installs a single `StructuredFormatter` handler on the root logger, and writes to `sys.stderr`.

| Flag | Effect |
|------|--------|
| `--verbose` / `-v` | Root level → `DEBUG`. |
| `--quiet` / `-q` | Root level → `WARNING`. |
| `--log-format={pretty,plain,json}` | Line shape. Default: `pretty` when `stderr.isatty()`, `plain` otherwise. |

`-v` and `-q` are mutually exclusive — passing both exits with `UsageError`. Auto-selection considers only `stderr`; `stdout` piping does not switch the mode.

### Line shape

Every line — `pretty` or `plain` — has the shape:

```
HH:MM:SS.mmm | LEVEL | k=v k=v | message
```

Scope pairs come from `LogRecord.__dict__` (typically populated via `logger.info(msg, extra={...})` or `StructuredLogger.info(msg, key=val)`). Keys are alphabetically sorted for deterministic output; values that contain whitespace or `|` render via `repr` (`k='hello world'`). Empty scope preserves the double-space middle (`... | INFO |  | message`) so column grep stays trivial.

`pretty` wraps the whole line in an ANSI level colour matching `_display.THEME` — `INFO`=cyan, `WARNING`=yellow, `ERROR`=bold red, `DEBUG`=dim. `plain` emits no ANSI escapes. `json` emits one JSON object per line with keys `{"ts", "level", "logger", "message", "extra"}`; the schema is locked by `tests/canonical/golden/logging/json__*.log`.

### Precedence with subcommand `--verbose`

`run`, `prepare`, `worker`, and `adapter convert` each carry their own `--verbose` flag. It drives the per-trial `logs.yaml` level (via `Orchestrator(verbose=True)` → `StructuredLogger(level=DEBUG)`) and additionally bumps the root console handler to `DEBUG` unless the root `-q` flag is set. Root `-q` always wins on the console; subcommand `--verbose` still records `DEBUG` in `logs.yaml`.

| Root | Subcommand `--verbose` | Console level | `logs.yaml` level |
|------|------------------------|---------------|-------------------|
| (none) | (none) | INFO | INFO |
| `-v` | (none) | DEBUG | INFO |
| `-q` | (none) | WARNING | INFO |
| (none) | `--verbose` | DEBUG | DEBUG |
| `-v` | `--verbose` | DEBUG | DEBUG |
| `-q` | `--verbose` | WARNING | DEBUG |
| `-v -q` | (any) | `UsageError` — exit 2 | — |

See [`docs/LOGGING.md`](LOGGING.md) for the full API of `StructuredLogger`, `get_logger`, and `init_trial_logger`, and for the `logs.yaml` on-disk shape.

## Display modes

The root flag `--display={full,rich,plain,log,none}` and the equivalent env var `TOLOKAFORGE_DISPLAY=…` pick the overall stderr UI. Orthogonal to `--log-format`, which shapes individual log lines.

| Value    | Behaviour                                                                                                 |
|----------|-----------------------------------------------------------------------------------------------------------|
| `full`   | Textual TUI. Falls back to `rich` when textual is not installed (a WARNING log line notes the fallback).  |
| `rich`   | Rich Live panel during `tolokaforge run` — left-pane trial list (status glyphs), right-pane structured summary of the focused trial (`turn N · in Xk / out Y tok · $Z.ZZ · last: <event_kind>`), bottom bar `{completed}/{total} · {running} running · ${cost} · in {prompt} / out {completion} tok · fail {failed} · eta {eta}`. Log lines from `configure_root_logging` interleave above the panel. Every other subcommand renders through the shared `console.print(...)` calls. |
| `plain`  | Human-readable log-line stream. Default on non-TTY / when `CI` is set.                                    |
| `log`    | Pure log stream — no banners, no progress bars.                                                           |
| `none`   | Silent on stderr on success. `emit_artifact_path` still writes the artifact path to stdout.               |

### Live run panel (`--display=rich` during `tolokaforge run`)

`tolokaforge run` under `--display=rich` renders inside a `rich.Live` region owned by `tolokaforge.dx.live_panel.LiveRunDisplay`. The panel has three regions, plus two conditional ones:

- **Optional banner (top).** Populated on the first auth-shaped `trial_failed` so a bad API key doesn't hide as one row of `fail N` in the bottom bar.
- **Optional services widget (below the banner).** Only visible during the startup window (before the first `run_started`) when `phase_changed(services=…)` has fired. One row per built-in `EngineStack` service: name, lifecycle status, health glyph (`✓` healthy, `⋯` starting/created, `✗` unhealthy, `~` no probe), port summary (`container_port→host_port`). Disappears once trials dispatch.
- **Left pane — trial list.** One line per trial: `<glyph> [N/M] task_id · trial_index` where `<glyph>` is `⏳` for running, `✓` for completed, `✗` for failed and `[N/M]` is the run-wide 1-indexed position, zero-padded to the width of `M`. Running trials always render; completed and failed trials scroll off in `last_update_ts` order once the window (default 20 rows) fills. The main region is height-adaptive — three trials in a tall terminal give a short pane instead of filling the screen.
- **Right pane — focused trial.** Structured summary of the trial that most recently transitioned state: `turn N · in Xk / out Y tok · $Z.ZZ · last: <event_kind>`. Focus follows lifecycle transitions only (`trial_started`, `trial_completed`, `trial_failed`, `judgment_scored`) — per-turn `trial_progress` events mutate counters but leave focus stable, so the pane does not alternate on high-frequency ticks. Once `trial_provisioned` has fired for the focused trial, a compact "Infrastructure" sub-panel appears under the summary listing the per-container health glyphs and published ports for the trial's compose stack.
- **Bottom bar.** One line, format:

  ```
  {completed}/{total} · {running} running · ${cost} · in {prompt} / out {completion} tok · fail {failed} · eta {eta}
  ```

  Concrete example: `142/500 · 12 running · $0.87 · in 41.2k / out 6.8k tok · fail 3 · eta 03:14`. `cost` renders `$<0.01` below one cent and `$0.00` at zero. `prompt` / `completion` render with a `k` suffix at ≥ 5 000 tokens. `eta` is `MM:SS` under one hour, `HH:MM:SS` above, and `n/a` before the first in-run completion. The pinned literal shape lives in `tests/canonical/golden/run_display/panel_{80,120}.svg`.

The orchestrator, conductor, and runner emit lifecycle events into a `RunDisplayEvents` Protocol (`run_started`, `trial_started`, `trial_progress`, `trial_provisioned`, `trial_completed`, `trial_failed`, `judgment_scored`, `run_finished`, `phase_changed`). `LiveRunDisplay` subscribes and repaints at 4 Hz. `_NULL_EVENTS` is the default sink under any non-active mode — the orchestrator / conductor / runner never branch on `events is None`, they just call every method.

Log lines from `configure_root_logging` route through a `_LogSink` installed for the lifetime of the Live context. INFO / DEBUG records land in a bounded 500-record ring buffer inside the panel (available via `LiveRunDisplay.log_records()` for the future Textual log pane) and are otherwise swallowed so the Docker-boot log wall no longer scrolls the panel off-screen; WARNING and above still write to the original stderr the sentinel handler was wired to, so real problems surface above the panel. `__exit__` reinstalls the original sentinel handler.

`--display=full` today collapses to `--display=rich` because `textual` is not a dependency; when the Textual TUI (C3) lands, `--display=full` will render a Textual class that consumes the same `RunDisplayEvents` Protocol.

Under `--display={plain,log,none}` and on non-TTY streams, `LiveRunDisplay.for_mode(...)` returns a no-op context manager and the existing log-line stream is what the operator sees.

### Precedence

1. Explicit `--display=…` flag.
2. `TOLOKAFORGE_DISPLAY=…` env var.
3. `CI` env var truthy → `plain`. Non-empty and not in `{"0", "false", "False", "FALSE", "no", "off", ""}` counts as truthy.
4. `sys.stderr.isatty()` truthy → `rich`.
5. Otherwise → `plain`.

Explicit flag beats env var; env var beats `CI`; `CI` beats isatty. An operator can force `rich` on a piped shell by exporting `TOLOKAFORGE_DISPLAY=rich`, and can force `plain` from a wrapper script by exporting `TOLOKAFORGE_DISPLAY=plain`.

### Composition with `--log-format`

`--display` and `--log-format` are orthogonal axes and may be combined. `--display` picks the overall UI; `--log-format` picks the line shape of any log lines the UI emits. `--display=none` silences the shared `console` (`console.quiet = True`) AND raises the tolokaforge root log handler above CRITICAL so no log record emits — `--log-format` is retained for the shape any escape-hatch line would take but has no observable effect on a success path.

### `--display=none` semantics

Silencing applies to two knobs, both mutated by the group callback when the resolved mode is `none`:

- `console.quiet = True` on the shared Rich console (`tolokaforge.dx._display.console`). Every `.print(...)` / `.rule(...)` / `.status(...)` short-circuits at buffer-check time — no bytes reach stderr.
- `handler.level = logging.CRITICAL + 1` on the tolokaforge sentinel-tagged root log handler. Every stdlib log record and every `StructuredLogger` record drops at the handler before formatting.

The single stdout write (`emit_artifact_path` in `tolokaforge.dx._display`) is untouched — `--display=none` silences stderr but preserves the `RUN_DIR=$(tolokaforge run --display=none --config …)` shell idiom.

### Failure paths under `--display=none`

Silencing applies to the shared `console` and the tolokaforge log handler only. The following stderr sources bypass both silencing knobs and continue to write on failure — the operator sees the failure regardless of `--display=none`:

- Click's `UsageError` output (bad flags, bad env-var values).
- Python's uncaught-exception tracebacks.
- `warnings.warn` calls, which write directly via `warnings.showwarning`. A `DeprecationWarning` fired inside `Orchestrator` construction leaks under `--display=none`. Intentional — warnings are diagnostic and should not be swallowed.

### `TOLOKAFORGE_DISPLAY=<invalid>` behaviour

The group callback validates `TOLOKAFORGE_DISPLAY` on every invocation, including subcommand `--help`. A stale export of `TOLOKAFORGE_DISPLAY=wombat` therefore fails with `click.UsageError` even for `tolokaforge run --help`. The fix is `unset TOLOKAFORGE_DISPLAY` (or export a valid value).

### `ctx.obj["display_mode"]`

The group callback stashes the resolved `DisplayMode` on `ctx.obj["display_mode"]` after applying the precedence rules and Textual fallback. The value is a `DisplayMode` enum (not the raw string) so consumers get `if mode is DisplayMode.FULL:` type-safety. Consumer commands read the resolved mode from this single source rather than re-parsing the flag / env var.

## Interactive shell

Running `tolokaforge` with no subcommand drops into an interactive Click REPL. Every top-level verb is available as a free-form command inside the session — the same argument parsing, the same `--help`, the same subcommands. The explicit form `tolokaforge repl` also enters the shell and is listed under the `Interactive` heading in root `--help`.

```
$ tolokaforge
tolokaforge interactive shell. Type `help` for commands, `exit` to quit.
tolokaforge> run --config examples/native/tool_use/run_config.yaml --dry-run
…
tolokaforge> exit
$
```

Implemented in `tolokaforge.dx.repl.enter_repl` — a thin wrapper around [`click-repl`](https://github.com/click-contrib/click-repl) using `prompt_toolkit` for line editing.

### Session-scoped root flags

Root flags supplied at REPL entry (`-v`, `-q`, `--display`, `--log-format`) apply to every command entered in the session until it exits. The `cli()` group callback runs once at entry, mutates the shared logging + console state (via `configure_root_logging`, `select_display_mode`, `silence_console`), and those mutations stay in effect across REPL invocations.

Subcommand-level `--verbose` inside the REPL still bumps the root console handler to `DEBUG` via `_bump_console_debug_if_allowed` (subject to the root `-q` carveout documented in [§ Precedence with subcommand `--verbose`](#precedence-with-subcommand---verbose)). The bump is not reset when the subcommand returns — a subsequent `run --help` in the same session inherits the elevated level. This matches the operator's explicit request to raise verbosity and mirrors the non-REPL invocation's process-wide semantics.

### Command discovery and completion

Type `--help` inside the REPL for the same grouped listing produced by `tolokaforge --help` outside it, and `<command> --help` for any subcommand's full flag reference. `click-repl` also registers its own `:help` internal command. Tab-completion resolves subcommand names, flag names, and any argument declared as `click.Path` — file paths under the working directory.

### Command history

Line history persists to `~/.tolokaforge_history` via `prompt_toolkit`'s `FileHistory`. Arrow-up / Ctrl-R searches previous invocations across sessions.

### Exit

`exit`, `quit`, or Ctrl-D return the terminal to the parent shell.

### Extras dependency

The REPL lives in the `[dx]` extras alongside Rich panels and banners (see [ADR-0019](architecture/adr/0019-front-end-plugin-namespace.md)). A headless-server install (`pip install tolokaforge`) does not pull `click-repl` or `prompt-toolkit` in; running the `tolokaforge` console script without the extras prints the install hint from the stdlib-only shim at `tolokaforge._entry:main`.

## Dry run

`tolokaforge run --dry-run [--dry-run-samples N]` resolves the run config with full parity to a real run, loads the declared tasks via the adapter, renders the first N samples' first-turn wire requests as Rich panels on stderr, and exits `0` without creating a run directory or issuing a single HTTP call to any provider.

```bash
tolokaforge run --config examples/native/tool_use/run_config.yaml --dry-run
```

### What it does

1. Loads the run config (same `load_effective_run_config` path a real run takes) and applies every CLI override — `--user-model`, `--judge-model`, `--runtime`, `--workers`, `--cost-limit`, `--time-limit`, `--sample-limit`, `--fallback-models`, `--model-cost-config`, `--presets-file` — identically to a real invocation. A malformed `--time-limit=30xyz` fails with the same `click.BadParameter` diagnostic under `--dry-run` as under a real run.
2. Constructs the adapter and enumerates every declared task. **Skips the TypeSense preflight** `Orchestrator.load_tasks()` performs — dry-run never starts Docker.
3. For each of the first N tasks (default N=3), materialises the first-turn wire request: system prompt, first user message, sanitized OpenAI-shape tool spec, and resolved model / judge / runtime identifiers.
4. Renders one `rich.panel.Panel` per sample on stderr through the shared `console`.
5. Returns exit `0`. No run directory is created, no `emit_artifact_path` fires (stdout stays empty), no [start/end banner](#run-banner) renders, and no [Live run panel](#live-run-panel---display-rich-during-tolokaforge-run) opens.

### Flags

| Flag | Argument | Default | Behaviour |
|------|----------|---------|-----------|
| `--dry-run` | boolean | `False` | Activate the dry-run branch. Mutually exclusive with `--resume` — passing both fails with `click.UsageError`. |
| `--dry-run-samples` | `int >= 1` | `3` | Cap on the number of rendered samples. When the cap exceeds the declared task count, every task renders and the preamble line reads `rendering first {N} sample(s) (of {N} task(s) available)`. Requires `--dry-run` — passing `--dry-run-samples` without `--dry-run` fails with `click.UsageError`. |

### Panel shape

Each sample renders a single `rich.panel.Panel` titled `Task <task_id> · Trial 0`. Body order, top to bottom:

1. `System prompt:` label followed by the literal task-scope system prompt the agent's `LLMClient` would receive on turn one.
2. `User prompt:` label followed by either the literal `task.initial_user_message` or, when that field is unset, a placeholder line `<generated at runtime by user simulator — mode={mode}, persona={persona}, backstory={backstory[:120]}…>` naming the user-simulator configuration.
3. `Tools ({count}):` label followed by the sanitized OpenAI-shape tool spec rendered as JSON via `rich.syntax.Syntax`. When the task declares no agent tools, the line becomes `(no agent tools declared)`.
4. `Model:` line — `<agent.provider>/<agent.name> · preset: <effective_preset>` (via `resolve_effective_preset`, the same call `_write_artifacts` uses to persist `task.yaml.model_config.agent.resolved.effective_preset` for a real trial).
5. `Judge:` line — same shape for `models.judge`, or `(none)` when the run config declares no judge.
6. `Runtime:` line — `shared` or `per_trial`, from `orchestrator.runtime`.

A single preamble line renders once before the first panel: `Dry run: rendering first {n_rendered} sample(s) (of {n_available} task(s) available)`. The 80- and 120-column layouts are pinned by `tests/canonical/golden/dry_run/panel_{80,120}.svg`.

### Sample selection

One sample is one `(task_id, trial_index=0)` pair. Every trial of the same task starts from the identical first-turn wire request (prompt assembly is deterministic per task), so trial 0 is representative — rendering additional trials of the same task would repeat the panel with no new information.

### Zero-HTTP guarantee

`materialize_dry_run_sample` reaches the sanitized tool spec by calling `build_capabilities(agent.name, agent.provider, overrides=agent.capabilities).schema_sanitizer.sanitize(...)` directly. No `LLMClient` is constructed, no API key is loaded, no `SecretManager` state is touched, and no socket is opened. The unit test `tests/unit/test_dry_run.py::test_materialize_no_http_via_respx` and the CLI test `tests/unit/test_dry_run_cli.py::test_dry_run_makes_no_http_via_respx_or_monkeypatch` patch both `httpx.Client.send` and `litellm.completion` with raise-on-call sentinels to guard the invariant.

### Composition with other flags

- **`--resume`** — mutually exclusive. Dry-run has no run directory to consult for state; passing both fails with `click.UsageError("--dry-run and --resume are mutually exclusive; --dry-run does not consult run state")`.
- **`--display=none`** — the shared `console` is quieted (`console.quiet = True`); no preamble and no panels reach stderr. Exit code is still `0`.
- **`--display={full,rich,plain,log}`** — panels render through the shared `console` (stderr). No `LiveRunDisplay` opens under any display mode — dry-run is a one-shot render, not an animated region.
- **`--presets-file`** — the overlay is activated identically to a real run; the `preset:` field on the rendered panel reflects the overlay-resolved preset name.
- **`--user-model` / `--judge-model` / `--runtime`** — reflected in the `Model:` / `Judge:` / `Runtime:` lines.
- **`--cost-limit` / `--time-limit` / `--sample-limit` / `--fallback-models`** — parsed and validated identically to a real run; a bad token surfaces the same `click.BadParameter` diagnostic. Not applied, because no trials execute.

### Streams

Stdout stays strictly empty — dry-run produces no artifact, so `emit_artifact_path` is not called (see [§ stdout / stderr contract](#stdout--stderr-contract)). Stderr carries the preamble line and the rendered panels; any log lines from `configure_root_logging` interleave above them under the resolved `--log-format`.

### Typesense / Docker side effects

`load_tasks_for_dry_run` builds the adapter directly and enumerates tasks without the TypeSense preflight `Orchestrator.load_tasks()` runs. A run config that declares `orchestrator.typesense.enabled=true` renders panels showing the search config as authored — `port="auto"` / `api_key=null` fields stay unresolved because no container starts. Operators wanting resolved TypeSense values run a real trial or `tolokaforge prepare`.

## Resume

`tolokaforge run --resume --run-dir <path>` re-runs only the trials that aren't already `completed` (or behavioural-failed) in `<path>/run_state.json`. The two flags are paired — passing either alone is a `click.UsageError`. `--run-dir` must point at an existing directory that carries a `run_state.json` (or an `engine_run_state.json` for the queue-worker path); the CLI reads the canonical `run_id` from those files and reuses `<path>` verbatim, so no fresh timestamped sibling is allocated.

```bash
tolokaforge run --config path/to/run.yaml --resume --run-dir results/coding_20260714_193042
```

Before Docker starts, the CLI opens the run state and prints a one-line summary on stderr:

```
Resuming: 42/50 completed, 8 to retry
```

`completed` counts trials with `status == "completed"` regardless of `binary_pass`. `to_retry` counts trials that are `pending`, `running`, or infrastructure-failed (retryable). Behavioural-failed trials (the agent finished but failed grading) count as already-done and are not retried.

When every trial is already `completed`, the CLI short-circuits before touching Docker or the runtime backend:

```
→ Nothing to do; run already complete (50/50 completed)
```

stdout still carries the absolute run-dir path (`emit_artifact_path`), so the shell-capture idiom `RUN_DIR=$(tolokaforge run --resume --run-dir …)` continues to work on a completed run. Exit code is `0`.

### Resume start banner

On the resume path the [start banner](#start-banner) first line changes from `→ Run: <run-id>` to `→ Resume: <run-id>`; the `→ Report:` line is unchanged. The end banner is identical to a non-resume run — success, failure, and `⏸ Run stopped (<reason>)` variants all apply. The 80- and 120-column resume banner shape is pinned by `tests/canonical/golden/run_banner/banner_start_resume.svg`.

### Composition with other flags

- **`--dry-run`** — mutually exclusive. Passing both fails with `click.UsageError("--dry-run and --resume are mutually exclusive; --dry-run does not consult run state")`.
- **`--cost-limit` / `--time-limit` / `--sample-limit`** — budgets are per-invocation. The cost budget seeds from prior-invocation spend recorded under the run directory, so dollars already burned still count; the time and sample counters restart on each `tolokaforge run --resume` invocation. Cumulative budgets across resumes are tracked in [#352](https://github.com/Toloka/tolokaforge/issues/352). See [§ Cost, time, and sample limits](#cost-time-and-sample-limits).
- **`--presets-file` / `--user-model` / `--judge-model` / `--runtime`** — applied identically to a first-time run.

### `--run-dir` flag

| Flag | Argument | Default | Behaviour |
|------|----------|---------|-----------|
| `--run-dir` | existing directory | `None` | Path to an existing run directory. Requires `--resume` — passing it alone is a `click.UsageError`. The path must exist at flag-parse time (Click validates via `click.Path(exists=True, file_okay=False)`), and must contain either `engine_run_state.json` or `run_state.json`. |

### Queue-worker resume

`tolokaforge prepare` + `tolokaforge worker` are inherently resumable via the durable queue — see [`docs/RUNNER.md` § Resuming a queue-worker run](RUNNER.md#resuming-a-queue-worker-run). No `--resume` flag: restart `worker --run-dir <existing>` and the durable queue delivers the pending attempts. `tolokaforge prepare --reset-queue` explicitly wipes the queue before re-enqueue when the operator wants to start over.

## Root help layout

`tolokaforge --help` groups every top-level command under a fixed-order section heading: **Runs**, **Tasks**, **Docker**, **Config**, **Assets**, **Adapters**. Commands appear alphabetically within each section, and empty sections are omitted. Per-command help (`tolokaforge run --help`, `tolokaforge docker up --help`, …) renders through Click's default formatter — the grouped layout applies to the root `Commands:` block only.

Current mapping:

| Section    | Commands                                     |
|------------|----------------------------------------------|
| Runs       | `analyze`, `prepare`, `run`, `status`, `worker` |
| Tasks      | `validate`                                   |
| Docker     | `docker`                                     |
| Config     | `config`                                     |
| Assets     | `assets`                                     |
| Adapters   | `adapter`                                    |

Abbreviated transcript of the `Commands:` region:

```
Runs:
  analyze  Analyze a single trial trajectory.
  prepare  Prepare a queue-backed run directory for distributed workers.
  run      Run benchmark with specified configuration
  status   Show live/status snapshot for a run directory.
  worker   Run a queue worker process (distributed execution mode).

Tasks:
  validate  Validate task configurations

Docker:
  docker  Manage Docker images and service stacks.

Config:
  config  Configuration management commands.

Assets:
  assets  Manage project-level assets (seeds today).

Adapters:
  adapter  Adapter management commands.
```

### `tolokaforge --version`

`tolokaforge --version` prints `tolokaforge, version <version>` and exits `0`. The version is sourced from installed package metadata via Click's `version_option(package_name="tolokaforge")`, which reads `importlib.metadata.version("tolokaforge")`. The string writes to stdout (Click's default channel for `--version`) and is unaffected by `--display` or `--log-format`.

### Adding a new top-level command

The root group is wired as `@click.group(cls=_GroupedCommandsGroup)`, and `_GroupedCommandsGroup.COMMAND_GROUPS` maps every command name to its section heading. Registering a new top-level command requires adding an entry to that map; a command with no mapping raises `RuntimeError("_GroupedCommandsGroup: no group heading for command '<name>'; add it to COMMAND_GROUPS")` the first time the root `--help` renders. The unit test `tests/unit/test_cli_help_grouping.py::test_every_registered_command_has_a_group` enforces the same invariant at CI time so drift is caught before `--help` is ever invoked.

## Run banner

`tolokaforge run` frames every invocation with two banners on stderr. Both banners write through the shared `console`, so the semantic palette, soft-wrapping, and stream posture from [§ Display layer](#display-layer) apply.

### Start banner

Rendered after the run-id resolves, before the display region opens:

```
→ Run: <run-id>
→ Report: file:///<abs-path>/results/<run-dir>/
```

### End banner

Rendered after the display region closes, on both success and failure, before the stdout artifact-path emission:

```
✓ Run complete in <duration>
→ Report: file:///<abs-path>/results/<run-dir>/
→ Browse: tolokaforge browse <run-id>
```

On failure the outcome line becomes `✗ Run failed in <duration>` (bold red glyph); the underlying exception continues to propagate to Click, which renders its own traceback and exit code. The banner is complementary — it does not swallow the failure.

When a budget cuts the run short (see [§ Cost, time, and sample limits](#cost-time-and-sample-limits)) the outcome line becomes `⏸ Run stopped (<reason>) in <duration>` — yellow glyph via the `warn` theme token. `<reason>` is one of `cost limit`, `time limit`, or `sample limit`, read from `LIMIT_HIT.json` under the run directory. Report and browse lines are unchanged. The stopped variant supersedes the success / failure axis: a run that both hit a budget and raised on drain still renders the stopped shape.

`<duration>` is `MM:SS` under one hour, `HH:MM:SS` above — the same shape the Live run panel's bottom bar uses. It is measured with `time.monotonic()` bracketing the run.

### `file://` URLs

The `→ Report:` URL is canonicalised via `Path.resolve().as_uri() + "/"`: always absolute, always trailing-slashed to mark a directory. Rich wraps it in OSC 8 hyperlink markup (`[link=URL]…[/link]`), so terminals that support OSC 8 render it clickable. Terminals without OSC 8 support show the URL as plain underlined-cyan text (the `link` theme token — see [§ THEME](#theme--semantic-token-palette)) and remain copyable.

### `tolokaforge browse <run-id>`

The `→ Browse:` line is a suggested follow-up command. `tolokaforge browse` is landed by #289; until then the string is copy-paste friendly text but not yet an installed subcommand.

### Display-mode behaviour

| `--display` | Banners on stderr                                                                                                                                                                    |
|-------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `full`      | Visible.                                                                                                                                                                             |
| `rich`      | Visible.                                                                                                                                                                             |
| `plain`     | Visible.                                                                                                                                                                             |
| `log`       | Visible.                                                                                                                                                                             |
| `none`      | Silenced — `silence_console()` sets `console.quiet = True` and every `.print(...)` short-circuits at buffer-check time. The stdout artifact-path emission (on success) still fires. |

`--display=log` shapes the log-line stream only; it does not silence the shared `console`, so the banner still renders under it.

## Cost, time, and sample limits

Three flags on `tolokaforge run` cap what the run may spend. Any single cap crossing triggers a graceful shutdown: `Orchestrator.run()` stops enqueuing new trials, in-flight trials complete, `LIMIT_HIT.json` lands under the run directory, and the [end banner](#end-banner) switches to the `⏸ Run stopped (<reason>)` variant.

| Flag | Argument | Effect |
|------|----------|--------|
| `--cost-limit` | float (USD) | Caps cumulative agent cost. Overrides `compute.max_budget_usd` in the run config (mirrors `--workers`). When set, the `--display=rich` bottom-bar `$cost` segment renders in `warn` (yellow) at ≥ 80 % of the budget and `error` (bold red) at ≥ 100 % — see [§ THEME](#theme--semantic-token-palette). |
| `--time-limit` | duration string | Caps wall-clock execution time. Accepted units: `s`, `m`, `h`, `d`. Compound and fractional forms are accepted — `30m`, `2h`, `1h30m`, `90s`, `1d12h`, `1.5h`. Bare numbers (no unit), empty strings, negatives, and unknown units fail with `click.BadParameter` naming the offending token. The clock starts on the first `record_*` call inside the wait loop — task-loading time (which may be tens of seconds on large projects) does not count against the budget. |
| `--sample-limit` | int | Caps the number of **terminated** trials — trials that reached `mark_completed` OR `mark_failed` after retries exhausted. Distinct from the task-selection cap (`evaluation.tasks_glob`); this applies at the queue-dispatch layer. Because in-flight trials complete after a hit, the effective sample count on shutdown is `n` to `n + workers − 1`. |

Composing multiple flags is expected — `--cost-limit 5 --time-limit 30m --sample-limit 100` builds a composite budget and the first hit wins:

```bash
tolokaforge run --config run.yaml \
    --cost-limit 5 --time-limit 30m --sample-limit 100
```

Resume semantics: on `tolokaforge run --resume` the cost budget seeds from prior-invocation spend (dollars already burned still count), while the time and sample counters restart for each invocation.

### `LIMIT_HIT.json` marker

On any budget hit the orchestrator writes `<run-dir>/LIMIT_HIT.json`:

```json
{
  "which": "cost",
  "threshold": 5.0,
  "value_at_hit": 5.03,
  "timestamp": "2026-07-15T12:34:56Z"
}
```

`which` is `"cost"`, `"time"`, or `"sample"`. `threshold` is the limit as configured; `value_at_hit` is the counter value at the moment of the crossing (may exceed the threshold on the last increment). `timestamp` is ISO 8601 UTC with a trailing `Z`. The file is absent on natural completion. A resumed run that hits a fresh limit overwrites an existing marker.

The full on-disk schema (including field types) is documented in [`docs/OUTPUT_FORMAT.md` § LIMIT_HIT.json](OUTPUT_FORMAT.md#limit_hitjson). After `Orchestrator.run()` returns the CLI reads this file to shape the [end banner](#end-banner).

## Fallback models

`--fallback-models m1,m2,...` accepts a comma-separated ordered chain of fallback agent models. Each trial constructs its own cursor starting at position 0 (the primary agent model from `models.agent`). On a hard `generate()` failure — any exception surfacing from `LLMClient.generate` after its inner tenacity retry (5 attempts) gave up — the cursor advances one step and the current call retries once on the next model. The trial's message history is preserved: subsequent turns for the same trial continue on the fallback model until *its* retry budget is also exhausted, at which point the cursor advances again.

Cursor advancement is at `generate()` granularity, not per-trial. A trial that started on the primary and hit a hard failure on turn 3 continues turns 4+ on the fallback; the mixed-model provenance is recorded per call in `metrics.yaml.usage.calls[].model`. The cursor never rewinds inside a trial — a fallback that started serving turns cannot bounce back to the primary. Each new trial resets the cursor to 0.

Token format per chain entry:

- `<provider>/<name>` — explicit provider (e.g. `openai/gpt-4o-mini`, `anthropic/claude-sonnet-4.6`).
- `<name>` — bare name; provider inferred from the primary agent's provider.

Rate-limit-style transient errors are handled by the inner `LLMClient` retry (5 attempts) and never reach the fallback wrapper. The chain fires only on what the primary itself declared unrecoverable. Chain exhaustion — every model in the chain raised — re-raises the last exception, and the trial fails through the orchestrator's normal path.

Every fallback event emits a structured log line on the `tolokaforge.core.llm.fallback` logger — `"Fallback triggered"` at `WARNING` level with scope pairs `from_provider`, `from_name`, `to_provider`, `to_name`, `cursor`, `error`, `error_type`. The line interleaves above the Rich Live region.

```bash
tolokaforge run --config run.yaml \
    --fallback-models "anthropic/claude-sonnet-4.6,openai/gpt-4o-mini"
```

Fallback is scoped to the **agent** client only. The user simulator and rubric judge run on their configured models without fallback in this release.

## Custom pricing overlay

`--model-cost-config <path>` overlays a JSON or YAML file onto the shipped pricing table (`tolokaforge/core/data/pricing.json`, refreshed from the OpenRouter API by `tools/pricing-updater`). Format is detected by file suffix — `.json` → JSON; `.yaml` / `.yml` → YAML. Both must match the shipped schema: a top-level `models` mapping keyed by `<provider>/<name>` with per-model `{input, output, cache_read?, cache_write?}` in USD per 1 M tokens.

Merge semantics are field-level:

- Overlay entries override matching keys in the shipped table.
- New model ids are additive.
- Fields present on the shipped model but absent from the overlay survive.

The overlay applies to the process-global `MODEL_PRICING` via `tolokaforge.core.pricing.reload_pricing(overlay_path=...)`. Because each CLI invocation is one process, the overlay is scoped to that invocation. Programmatic embedders reading two configs in the same Python process see the LATEST overlay's rates on both configs.

Malformed overlay (unparseable JSON / YAML) → `ValueError` naming the parse-failure location. Unknown suffix → `ValueError` naming the suffix. Non-existent path → Click's own `--model-cost-config` validator surfaces `does not exist` before the run starts.

```bash
tolokaforge run --config run.yaml --model-cost-config prices.yaml
```

## stdout / stderr contract

`tolokaforge` splits streams by purpose: **stdout** carries the machine-parseable artifact identifier; **stderr** carries everything a human reads (progress, banners, log records, error text).

| Command                                      | stdout on success                     | stderr                                                       |
|----------------------------------------------|---------------------------------------|--------------------------------------------------------------|
| `tolokaforge run`                            | Absolute run-dir path (single line).  | Start banner (run-id + `file://` report URL), progress, log records, end banner (outcome + duration + `file://` report URL + browse invocation). See [§ Run banner](#run-banner). Under `--dry-run` stdout stays empty (no run directory is created) and stderr carries the rendered panels instead of the start/end banner + progress log lines — see [§ Dry run](#dry-run). |
| `tolokaforge prepare`                        | Absolute run-dir path (single line).  | Queue summary, log records.                                  |
| `tolokaforge worker`                         | (empty)                               | "Worker complete" summary, log records.                      |
| `tolokaforge status`                         | (empty)                               | Run summary, queue ETA, cost totals.                         |
| `tolokaforge validate`                       | (empty)                               | Per-task validity lines, `N valid, M invalid` summary.       |
| `tolokaforge config validate`                | (empty)                               | Per-config validity lines, error / warning counts.           |
| `tolokaforge assets stamp`                   | (empty)                               | Digest-check summary or `--check` diff output.               |
| `tolokaforge adapter convert`                | (empty)                               | Per-task conversion lines, `Converted N tasks` summary.      |
| `tolokaforge analyze`                        | (empty)                               | Trajectory summary, tool-failure / log-error breakdown.      |
| `tolokaforge docker build` / `up` / `down` / `status` | (empty)                      | Build progress, stack status, error text.                    |

On any failure — bad config, orchestrator raise, zero tasks — stdout stays **empty** and the process exits non-zero. The `tolokaforge run` "no tasks" branch exits with code `1`; other failures propagate whatever exit code the underlying error raises (click `UsageError` → 2, `SystemExit(N)` → N).

The emitted path is `Path.resolve()`'d: symlinks are canonicalised and the line is always absolute, regardless of the caller's cwd or how the config expressed `evaluation.output_dir`.

The shell idiom captures the artifact:

```bash
RUN_DIR=$(tolokaforge run --config path/to/run.yaml)
# stderr still shows progress; RUN_DIR is the absolute run-dir path.
tolokaforge status --run-dir "$RUN_DIR"
```

Adding `2>/dev/null` (or `2>run.log`) drops progress without breaking the capture — the artifact-path emission is independent of `--verbose` / `--quiet` / `--log-format`, which shape stderr only.

The single stdout write goes through `emit_artifact_path` in `tolokaforge.dx._display`; a canonical test (`tests/canonical/test_cli_display_invariants.py::test_no_bare_stdout_write_in_cli`) forbids any other `print(` or `sys.stdout.write(` in `tolokaforge/dx/**/*.py`.

`--display=none` silences the shared console and the tolokaforge log handler on success; the stdout artifact-path emission is unaffected — see [§ Display modes](#display-modes).
