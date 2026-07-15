# CLI reference

This document describes the tolokaforge CLI's shared building blocks. Per-command usage lives in `--help` output.

## Display layer

Every module under `tolokaforge/cli/` renders human-facing output through `tolokaforge.cli._display`. The module owns four public names — `console`, `THEME`, `make_progress`, and `make_live` — and no CLI file may construct its own `rich.Console`. A canonical test (`tests/canonical/test_cli_display_invariants.py`) fails CI if a new module slips in an ad-hoc `Console(...)`.

### Why a shared surface

The CLI splits into five modules (`main.py`, `adapter_commands.py`, `assets_commands.py`, `config_commands.py`, `docker_commands.py`). Routing them all through one `Console` guarantees a single theme, a single stream posture (stderr, soft-wrapped), and one place for the `--display` toggle to mutate progress and Live-panel plumbing (see [§ Display modes](#display-modes)).

### `console`

```python
from tolokaforge.cli._display import console

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
from tolokaforge.cli._display import make_progress

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
from tolokaforge.cli._display import make_live

with make_live(Text("Starting…")) as live:
    live.update(Text("Working…"))
```

Defaults: `refresh_per_second=4.0`, `transient=False`, `screen=False`, `vertical_overflow="ellipsis"`.

### Choosing between `console.print`, `make_progress`, and `make_live`

- **One-shot line** (banner, error, single result): `console.print(...)`.
- **Iteration with a known total** (per-trial grading, batch uploads): `make_progress()`.
- **Persistent status region that repaints in place** (multi-line dashboard, live cost tracker): `make_live()`.

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
| `rich`   | Rich Live panel.                                                                                          |
| `plain`  | Human-readable log-line stream. Default on non-TTY / when `CI` is set.                                    |
| `log`    | Pure log stream — no banners, no progress bars.                                                           |
| `none`   | Silent on stderr on success. `emit_artifact_path` still writes the artifact path to stdout.               |

`--display=rich` renders through the existing per-command `console.print(...)` calls; `--display=full` always falls back to `rich` because textual is not a dependency.

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

- `console.quiet = True` on the shared Rich console (`tolokaforge.cli._display.console`). Every `.print(...)` / `.rule(...)` / `.status(...)` short-circuits at buffer-check time — no bytes reach stderr.
- `handler.level = logging.CRITICAL + 1` on the tolokaforge sentinel-tagged root log handler. Every stdlib log record and every `StructuredLogger` record drops at the handler before formatting.

The single stdout write (`emit_artifact_path` in `tolokaforge.cli._display`) is untouched — `--display=none` silences stderr but preserves the `RUN_DIR=$(tolokaforge run --display=none --config …)` shell idiom.

### Failure paths under `--display=none`

Silencing applies to the shared `console` and the tolokaforge log handler only. The following stderr sources bypass both silencing knobs and continue to write on failure — the operator sees the failure regardless of `--display=none`:

- Click's `UsageError` output (bad flags, bad env-var values).
- Python's uncaught-exception tracebacks.
- `warnings.warn` calls, which write directly via `warnings.showwarning`. A `DeprecationWarning` fired inside `Orchestrator` construction leaks under `--display=none`. Intentional — warnings are diagnostic and should not be swallowed.

### `TOLOKAFORGE_DISPLAY=<invalid>` behaviour

The group callback validates `TOLOKAFORGE_DISPLAY` on every invocation, including subcommand `--help`. A stale export of `TOLOKAFORGE_DISPLAY=wombat` therefore fails with `click.UsageError` even for `tolokaforge run --help`. The fix is `unset TOLOKAFORGE_DISPLAY` (or export a valid value).

### `ctx.obj["display_mode"]`

The group callback stashes the resolved `DisplayMode` on `ctx.obj["display_mode"]` after applying the precedence rules and Textual fallback. The value is a `DisplayMode` enum (not the raw string) so consumers get `if mode is DisplayMode.FULL:` type-safety. Consumer commands read the resolved mode from this single source rather than re-parsing the flag / env var.

## stdout / stderr contract

`tolokaforge` splits streams by purpose: **stdout** carries the machine-parseable artifact identifier; **stderr** carries everything a human reads (progress, banners, log records, error text).

| Command                                      | stdout on success                     | stderr                                                       |
|----------------------------------------------|---------------------------------------|--------------------------------------------------------------|
| `tolokaforge run`                            | Absolute run-dir path (single line).  | Progress, log records, "Run complete" banner, "Results saved to" line. |
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

The single stdout write goes through `emit_artifact_path` in `tolokaforge.cli._display`; a canonical test (`tests/canonical/test_cli_display_invariants.py::test_no_bare_stdout_write_in_cli`) forbids any other `print(` or `sys.stdout.write(` in `tolokaforge/cli/**/*.py`.

`--display=none` silences the shared console and the tolokaforge log handler on success; the stdout artifact-path emission is unaffected — see [§ Display modes](#display-modes).
