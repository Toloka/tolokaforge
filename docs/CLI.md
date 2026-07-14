# CLI reference

This document describes the tolokaforge CLI's shared building blocks. Per-command usage lives in `--help` output.

## Display layer

Every module under `tolokaforge/cli/` renders human-facing output through `tolokaforge.cli._display`. The module owns four public names — `console`, `THEME`, `make_progress`, and `make_live` — and no CLI file may construct its own `rich.Console`. A canonical test (`tests/canonical/test_cli_display_invariants.py`) fails CI if a new module slips in an ad-hoc `Console(...)`.

### Why a shared surface

The CLI splits into five modules (`main.py`, `adapter_commands.py`, `assets_commands.py`, `config_commands.py`, `docker_commands.py`). Routing them all through one `Console` guarantees a single theme, a single stream posture (stderr, soft-wrapped), and one place to change progress plumbing when downstream milestone work adds a Live panel, a `--display` toggle, and a `runs list` table.

### `console`

```python
from tolokaforge.cli._display import console

console.print("[success]✓ done[/success]")
```

`console` writes to **stderr** with `soft_wrap=True` and the semantic palette installed. Stderr is the default because the CLI reserves stdout for the machine-parseable artifact path a later stage will introduce; today no CLI command writes to stdout via Rich. Soft-wrapping preserves long paths and digests in narrow CI terminals.

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
