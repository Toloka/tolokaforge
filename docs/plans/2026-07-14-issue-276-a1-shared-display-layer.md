# Plan: A1 — Shared display layer for the CLI

Issue: Toloka/tolokaforge#276 (milestone: Terminal DX, umbrella #297)
Branch: `feat/issue-276-a1-shared-display-layer` (branches off `feat/terminal-dx`, PR targets `feat/terminal-dx`)

## Context

Every CLI module today instantiates its own `Console()` — four places today (`main.py:50`, `docker_commands.py:15`, `adapter_commands.py:18`, `config_commands.py:22`) plus a fifth in `assets_commands.py` (a stdout/stderr split with `soft_wrap=True` — `assets_commands.py:27, 34`). Nothing shares a theme; nothing shares progress or Live plumbing (neither exists yet in `tolokaforge/cli/`). This is the foundation issue for the Terminal DX milestone: B1 (Rich Live progress panel), B2 (`--display` toggle), C1 (`runs list` Rich tables), C3 (Textual TUI), D1 (`init` wizard prompts), and D4 (`doctor` health table) all consume the shared surface introduced here. Downstream A4 (#280) turns the stderr-by-default posture into `stdout=artifact, stderr=progress`.

Evidence:

- `grep -Rn "Console()" tolokaforge/cli/` → 4 hits (`main.py:50`, `docker_commands.py:15`, `adapter_commands.py:18`, `config_commands.py:22`).
- `grep -Rn "Console(" tolokaforge/cli/` → 6 hits (adds `assets_commands.py:27` `Console(soft_wrap=True)` and `assets_commands.py:34` `Console(stderr=True, soft_wrap=True)`).
- `grep -Rn "rich.progress\|rich.live\|from rich.progress\|from rich.live" tolokaforge/` → zero hits. Progress and Live are net-new.
- `pyproject.toml`: `rich>=13.0.0` — already present. No new dependency.
- Existing test surface: `tests/unit/test_cli_commands.py`, `tests/unit/test_cli_assets.py`, `tests/unit/test_cli_status.py`, `tests/unit/test_adapter_convert_cli.py`, `tests/unit/test_validate_rubric_migration.py` — Click `CliRunner` invocations that assert on `result.output` / `result.stderr`.

## Goal

One module, `tolokaforge/cli/_display.py`, is the CLI's single source for terminal output primitives:

- `console` — a shared `rich.Console` with `stderr=True`, `soft_wrap=True`, and `THEME` installed. Every CLI module imports it instead of instantiating its own.
- `THEME` — a Rich `Theme` mapping seven semantic tokens (`info`, `warn`, `error`, `success`, `muted`, `cost`, `link`) to concrete Rich styles chosen to read cleanly on dark terminals and remain legible on light ones.
- `make_progress(...)` — factory returning a preconfigured `rich.progress.Progress` bound to the shared console with an opinionated column set for benchmark runs (spinner + description + bar + m/n + elapsed + remaining). Accepts kwargs (`transient`, `disable`, `refresh_per_second`, `columns`, `auto_refresh`, `redirect_stdout`, `redirect_stderr`) that B1/B2 need. `redirect_stdout` / `redirect_stderr` pass through to Rich's defaults (True) — B1 will call with `False` to keep the orchestrator's log stream unbuffered while the panel is live.
- `make_live(...)` — factory returning a preconfigured `rich.live.Live` bound to the shared console. Accepts the kwargs B1's `LiveRunDisplay` will need (`renderable`, `refresh_per_second`, `transient`, `screen`, `auto_refresh`, `vertical_overflow`, `redirect_stdout`, `redirect_stderr`).

After migration, `grep -R "Console(" tolokaforge/cli/` returns exactly one hit — the constructor call inside `_display.py`. A test locks this invariant.

## Non-goals

- **Migrating call-site markup to semantic tokens.** Existing `[bold blue]`, `[cyan]`, `[green]`, `[red]`, `[yellow]` markup stays as-is — those are valid Rich style expressions and remain visually identical whether the console has a theme installed or not. Semantic-token adoption is a separate, incremental follow-up (or done as it becomes natural in B1/D4).
- **`--display` toggle plumbing (B2).** `make_progress(disable=…)` and `make_live(...)` expose the kwargs B2 will call — B2 wires the CLI flag and the auto-selection. A1 does not add a `--display` flag.
- **Structured logging (A3) or `stdout=artifact` (A4).** A1 defaults `console` to stderr because A4 depends on it, but A1 does not touch `core/logging.py` handlers, does not add `--verbose` / `--quiet`, and does not carve out a stdout channel for `validate`/`status`/`run`'s final artifact path. Those are #279 and #280.
- **Textual TUI primitives.** `tolokaforge/tui/` (C3) is a separate package with its own Textual `App`. A1 stays in `rich`.
- **New dependencies.** `rich` is already present. No `questionary`, no `textual`, no anything else.

## Target module surface

```python
# tolokaforge/cli/_display.py

from rich.console import Console
from rich.live import Live
from rich.progress import Progress, ProgressColumn
from rich.theme import Theme

THEME: Theme  # keys: info, warn, error, success, muted, cost, link

console: Console  # stderr=True, soft_wrap=True, theme=THEME

def make_progress(
    *,
    console: Console | None = None,
    transient: bool = False,
    disable: bool = False,
    columns: Sequence[ProgressColumn] | None = None,
    refresh_per_second: float = 10.0,
    auto_refresh: bool = True,
    redirect_stdout: bool = True,
    redirect_stderr: bool = True,
) -> Progress: ...

def make_live(
    renderable: RenderableType | None = None,
    *,
    console: Console | None = None,
    refresh_per_second: float = 4.0,
    transient: bool = False,
    screen: bool = False,
    auto_refresh: bool = True,
    vertical_overflow: str = "ellipsis",
    redirect_stdout: bool = True,
    redirect_stderr: bool = True,
) -> Live: ...
```

Default column set for `make_progress` (locked by test): `SpinnerColumn`, `TextColumn("[progress.description]{task.description}")`, `BarColumn`, `MofNCompleteColumn`, `TaskProgressColumn`, `TimeElapsedColumn`, `TimeRemainingColumn`.

Default theme (locked by test — freeze the palette so drift fails loud):

| Token     | Style              | Rationale                                                        |
|-----------|--------------------|------------------------------------------------------------------|
| `info`    | `cyan`             | Hints and status lines. Matches today's `[cyan]` sites.          |
| `warn`    | `yellow`           | Warnings. Matches today's `[yellow]`.                            |
| `error`   | `bold red`         | Errors. Louder than default red — user must not miss.            |
| `success` | `green`            | ✓ lines. Matches today's `[green]`.                              |
| `muted`   | `dim`              | Secondary detail (paths, ids). Rich `dim` style.                 |
| `cost`    | `bold magenta`     | Money and token counts (B1 status bar).                          |
| `link`    | `underline cyan`   | `file://` URLs (A5 banner, C1 `runs open`).                      |

All styles are Rich standard names (no truecolor hex) so terminals without full-color support render them acceptably.

`console` construction — locked by test — is: `Console(stderr=True, soft_wrap=True, theme=THEME)`. `soft_wrap=True` matches the current `assets_commands.py` posture (prevents mid-word wrapping of paths and digests in narrow CI terminals) and is a no-op for short lines everywhere else.

## Stages

### Stage 1: Introduce `_display.py` + module tests

- **Contract:**
  - New file `tolokaforge/cli/_display.py` exporting the four public names above.
  - Every kwarg listed in the signatures is honoured; `make_progress` / `make_live` default `console` to the module's shared `console` when `None`.
  - `THEME` contains exactly seven keys (`info`, `warn`, `error`, `success`, `muted`, `cost`, `link`) with the styles pinned above.
  - `console.stderr is True`, `console.soft_wrap is True`, and `THEME` is installed (`console.get_style("info")` resolves to a `Style` equivalent to `Style.parse("cyan")`).
- **Behaviour to lock (unit tests in `tests/unit/test_cli_display.py`):**
  - `test_console_writes_to_stderr` — `console.stderr is True` and `console.file is sys.stderr`.
  - `test_console_has_soft_wrap` — `console.soft_wrap is True`.
  - `test_theme_defines_semantic_tokens` — `THEME.styles.keys() >= {"info","warn","error","success","muted","cost","link"}` and every token resolves via `console.get_style(name)`.
  - `test_theme_palette_frozen` — each token's `Style` equals the pinned parse (e.g. `console.get_style("error") == Style.parse("bold red")`). One assertion per token; regression net if someone silently retunes the palette.
  - `test_make_progress_uses_shared_console` — `make_progress().console is console`.
  - `test_make_progress_default_columns` — the returned `Progress.columns` include, in order, `SpinnerColumn`, `TextColumn`, `BarColumn`, `MofNCompleteColumn`, `TaskProgressColumn`, `TimeElapsedColumn`, `TimeRemainingColumn` (identity by class).
  - `test_make_progress_disable_kwarg` — `make_progress(disable=True).disable is True`.
  - `test_make_progress_transient_kwarg` — `make_progress(transient=True).live.transient is True` (or equivalent — Rich exposes it via `Progress.live`).
  - `test_make_progress_custom_columns_override_default` — passing `columns=[SpinnerColumn()]` yields exactly one column.
  - `test_make_live_uses_shared_console` — `make_live().console is console`.
  - `test_make_live_defaults` — `refresh_per_second == 4.0`, `transient is False`, `screen is False`, `auto_refresh is True`, `vertical_overflow == "ellipsis"`.
  - `test_make_live_accepts_renderable` — a `Text("hello")` renderable passed positionally is stored as `Live.renderable`.
- **Compatibility:** internal only. `_display.py` is a private CLI helper module (leading underscore); no user-facing surface changes yet.
- **Deliverable:** `tolokaforge/cli/_display.py` and `tests/unit/test_cli_display.py`. Nothing else edited.
- **Validation:** `uv run pytest tests/unit/test_cli_display.py -v` passes; `uv run ruff check tolokaforge/cli/_display.py tests/unit/test_cli_display.py` clean; `uv run ruff format --check` clean.
- **Doc updates:** none in this stage (module is not yet consumed).

### Stage 2: Migrate every CLI module to the shared console + update existing tests

- **Contract:**
  - No `Console(` instantiation anywhere in `tolokaforge/cli/**/*.py` outside `_display.py`.
  - Every CLI module imports `console` from `tolokaforge.cli._display`.
  - `main.py` keeps `from rich.console import Console` only if the `Console` symbol is still used as a type annotation on `_print_runtime_banner(*, console: Console, ...)`; otherwise drops the import.
  - `assets_commands.py`'s `err_console` is deleted; all its output — success paths and error paths alike — goes through the shared `console`, which now writes to stderr uniformly. The docstring comment block explaining the stdout/stderr split is rewritten to describe the new single-stream posture (A4 will re-carve stdout later; this stage does not).
  - Rich output routing changes from stdout to stderr across the board. Text markup on call sites (`[bold blue]`, `[cyan]`, `[green]`, `[red]`, `[yellow]`, etc.) is untouched — output is byte-for-byte identical when captured with mixed streams.
- **Behaviour to lock (unit tests):**
  - Existing `CliRunner(mix_stderr=False)` tests that currently assert Rich-emitted text on `result.output` are updated to assert on `result.stderr`. Tests that assert Click-emitted text (help output, argument-validation errors) continue to check `result.output`. Concrete migrations:
    - `tests/unit/test_cli_commands.py`:
      - `TestValidateCommand.test_validate_nonexistent_glob` — `"0 valid" in result.stderr`.
      - `TestConfigCommands.test_config_validate_empty_dir` — `"No YAML files found" in result.stderr`.
      - `TestConfigCommands.test_config_validate_non_mapping_yaml` — `"mapping" in result.stderr.lower()`.
    - `tests/unit/test_cli_assets.py` (this is the widest change — assets was the only module that already split streams):
      - Success paths: `"wrote 1 digest"`, `"already current"`, `"stale"`, `"match"`, `"nothing to stamp"`, `"strip on write"`, `"sha256:placeholder → …"` — all migrate from `result.output` to `result.stderr`.
      - Error paths: `"shared/seeds/missing.sql"`, `"does not exist"`, `"must be a mapping"`, `"No project.yaml"` — already on `result.stderr` in current tests, unchanged.
      - `test_fresh_digest_written_and_idempotent`'s mtime idempotency assertion (`project_yaml.stat().st_mtime_ns == mtime_after_first`) is unaffected — no stream involved.
    - `tests/unit/test_validate_rubric_migration.py` — `test_validate_rejects_legacy_rubric_str`, `test_validate_rejects_legacy_model_ref`, `test_validate_accepts_structured_rubric`: `result.output` → `result.stderr` for the Rich-emitted validate output.
    - `tests/unit/test_cli_status.py` — uses `CliRunner()` with mixed streams (`mix_stderr` default True), so `result.output` already contains combined stdout+stderr; **no change needed**. Regression-check as-is.
    - `tests/unit/test_adapter_convert_cli.py` — asserts `result.exit_code == 0, result.output` (uses `.output` only as the failure-diagnostic message, not as behaviour); no functional change needed. Leave as-is.
- **Compatibility:** internal only. This is not a compatibility surface — no task-pack contract, no run-config schema, no gRPC message, no published Python API touched. `AGENTS.md` Core Rule 5 does not gate this.
- **Deliverable:** edits to
  - `tolokaforge/cli/main.py` (line 10 import; line 50 removes `console = Console()`; imports shared `console`).
  - `tolokaforge/cli/docker_commands.py` (lines 12, 15).
  - `tolokaforge/cli/adapter_commands.py` (lines 16, 18).
  - `tolokaforge/cli/config_commands.py` (lines 13, 22).
  - `tolokaforge/cli/assets_commands.py` (lines 19, 21–34: single-console posture, docstring rewrite).
  - `tests/unit/test_cli_commands.py`, `tests/unit/test_cli_assets.py`, `tests/unit/test_validate_rubric_migration.py`.
- **Validation:**
  - `uv run pytest tests/unit/test_cli_commands.py tests/unit/test_cli_assets.py tests/unit/test_cli_status.py tests/unit/test_validate_rubric_migration.py tests/unit/test_adapter_convert_cli.py -v` — all green.
  - `grep -Rn "Console(" tolokaforge/cli/ | grep -v "_display.py"` — zero hits.
  - `uv run ruff check tolokaforge/cli tests/unit` clean; format check clean.
  - Manual visual smoke (implementer runs, quotes output in PR body): `uv run tolokaforge --help`, `uv run tolokaforge run --help`, `uv run tolokaforge config validate --config examples/native/tool_use/run_config.yaml 2>&1 | tee /tmp/a1-smoke.log` — confirm no visible regression versus the pre-migration output.
- **Behaviour change to call out in PR body:** `tolokaforge assets stamp` currently splits streams (success → stdout, errors → stderr). After Stage 2 all its output goes to stderr, uniform with the rest of the CLI. Callers piping success output should switch to `2>&1 | …` until A4 (#280) re-carves a stdout channel for machine-parseable artifact paths. Landed 2026-07-11 in `525a33f` — external usage low, but call it out so downstream consumers can adjust.
- **Doc updates:** none in this stage; documentation ships in Stage 3.

### Stage 3: Grep-guard invariant test + `docs/CLI.md` display-layer section

- **Contract:**
  - A canonical test codifies the "no `Console(` outside `_display.py`" invariant so future regressions fail in CI.
  - `docs/CLI.md` (new) documents the shared display layer as the CLI's public convention for future contributors.
- **Behaviour to lock (canonical test in `tests/canonical/test_cli_display_invariants.py`, `@pytest.mark.canonical`):**
  - `test_no_ad_hoc_console_in_cli` — walk `tolokaforge/cli/**/*.py`; for each file except `_display.py` and `__init__.py`, assert no line matches the regex `\bConsole\s*\(`. Failure message names the offending file and line so a future author sees exactly where they slipped up.
  - `test_display_module_exports_public_surface` — `import tolokaforge.cli._display as d; assert hasattr(d, "console") and hasattr(d, "THEME") and callable(d.make_progress) and callable(d.make_live)`. Pins the surface so a rename fails here rather than at every call site.
- **Compatibility:** internal only.
- **Deliverable:**
  - `tests/canonical/test_cli_display_invariants.py`.
  - `docs/CLI.md` — new file with a `## Display layer` section describing `_display.py`'s purpose, the semantic-token palette, and when to use `make_progress` / `make_live` versus raw `Console.print`. Written as the current state of the CLI (per AGENTS.md Core Rule 8) — no "previously X" framing. Titles the file so that later milestone issues (B1/B2/…) can append their own sections without a rename.
- **Validation:**
  - `uv run pytest tests/canonical/test_cli_display_invariants.py -v` green.
  - `uv run pytest tests/unit -m unit -x` and `uv run pytest tests/canonical -m canonical -x` — full unit + canonical suites green (ensures Stage 2's tests still pass alongside the new guard).
  - `uv run ruff check` clean.
- **Doc updates:** creates `docs/CLI.md`. Also runs `rg "\bConsole\(" tolokaforge/ tests/ docs/` and confirms every remaining hit is either `_display.py` itself, a test that asserts on the invariant, or a non-CLI subsystem (the `RichHandler` reference in `tolokaforge/docker/logging.py:702–705` is unrelated logging config — leave alone).

## Discovered issues

- **Fix in this PR:** None. The CLI modules audited are otherwise clean of A1-adjacent smells.
- **Filed as issues:** None. The one adjacent-but-separate observation — `assets_commands.py`'s stdout/stderr split is a UX pattern the rest of the CLI should adopt for pipeline-friendly output — is already the explicit scope of #280 (A4) in the milestone. No new issue needed.

## Risks / open questions

- **Rich `Progress.live.transient` attribute path.** The Stage 1 test `test_make_progress_transient_kwarg` reaches through Rich's internal `Progress.live` handle. If Rich renames this on a minor bump, the assertion needs to move to a documented public accessor. Mitigation: pin the assertion to whatever public API Rich 15.x (per `uv.lock`) exposes at implementation time — verified `Progress.live.transient` works today; if a future bump ever drops it, fall back to a render-capture behavioural test that asserts the transient behaviour end-to-end. Not a blocker.
- **Mixed-stream Click behaviour under `mix_stderr=False`.** Modern Click (≥8.2) has deprecated the `mix_stderr` parameter; today's tests still use it and it still works on the version pinned in `uv.lock`. If a future Click upgrade drops it, those tests will need to be rewritten independently of this issue. Out of scope for A1; noted so an implementer isn't surprised.
- **Interaction with A4 (#280).** A4 will introduce a dedicated stdout channel for `run`/`prepare`'s final artifact path and for `validate`/`status`'s human-readable output. That will likely add a second export to `_display.py` (`stdout_console`, or a `write_artifact_path()` helper). A1 does not preempt that decision — Stage 2 unifies everything onto stderr so A4 has a single starting point.
