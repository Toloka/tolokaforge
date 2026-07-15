# Plan: B2 — `--display={full,rich,plain,log,none}` toggle

Issue: Toloka/tolokaforge#282 (milestone: Terminal DX, umbrella #297)
Branch: `feat/issue-282-b2-display-toggle` (branches off `feat/terminal-dx`; PR targets `feat/terminal-dx`)

## Context

Milestone Terminal DX progress:

- **A1 (#276)** shipped [`tolokaforge/cli/_display.py`](../../tolokaforge/cli/_display.py): the shared `console` (stderr, `soft_wrap=True`, `THEME` installed), `make_progress`, `make_live`. Every CLI module renders through `console`; the canonical grep-guard `tests/canonical/test_cli_display_invariants.py::test_no_ad_hoc_console_in_cli` forbids any new `rich.Console(...)` outside `_display.py`.
- **A3 (#279)** shipped the `--verbose / -v`, `--quiet / -q`, and `--log-format={pretty,plain,json}` root flags on `tolokaforge/cli/main.py::cli()`; every stdlib and `StructuredLogger` record renders through `configure_root_logging(...)` on stderr with the shape `HH:MM:SS.mmm | LEVEL | k=v | message`. The group callback stashes `ctx.obj["log_format"]` / `ctx.obj["root_quiet"]` / `ctx.obj["root_verbose"]` so subcommands can honour them.
- **A4 (#280)** shipped `emit_artifact_path(path)` in `_display.py`: the single sanctioned stdout write. `tolokaforge run` and `tolokaforge prepare` end their success paths with it, so `RUN_DIR=$(tolokaforge run --config …)` captures the run dir. A canonical guard `test_no_bare_stdout_write_in_cli` forbids any other `print(` / `sys.stdout.write(` in `tolokaforge/cli/**/*.py`.

The A4 plan's `Discovered issues / Risks` section explicitly parked one coordination note for B2 (`docs/plans/2026-07-15-issue-280-a4-stdout-artifact-stderr-progress.md:346`):

> **`--display=none` interaction (B2/#282).** B2 must NOT gate `emit_artifact_path`. The helper is orthogonal to display mode — display mode picks the stderr renderer; the stdout emission is unconditional on success. Document explicitly in B2's plan.

The A3 plan's `Non-goals` section explicitly deferred env-var log-format overrides to B2's env-var protocol (`docs/plans/2026-07-14-issue-279-a3-structured-log-format.md:52`):

> **Env-var overrides for log format** (`TOLOKAFORGE_LOG_FORMAT=…`). B2 (#282) owns the `--display` env-var protocol and can extend the same protocol to `--log-format` in a follow-up.

B2 adds the *display-mode selector*: which overall UI to run under. B1 (#285, not yet shipped) will consume the selected mode to pick between renderings (Rich Live panel vs plain log stream vs Textual TUI vs silent). B2 only picks the mode and plumbs it into `ctx.obj["display_mode"]`; it does not render anything new.

Grep confirms the surface is net-new: `rg -n "TOLOKAFORGE_DISPLAY|display_mode|--display" tolokaforge/ tests/ docs/` returns zero hits, and `rg -n '"CI"\s*(in|,|:)|os\.getenv\(\s*"CI"' tolokaforge/ tests/` also returns zero — no existing `CI` detection to conflict with.

Reproduced current state via `run_python` (fresh session, `feat/issue-282-b2-display-toggle` clean):

- `uv run tolokaforge --help` — the current top-level flags are `--verbose/-v`, `--quiet/-q`, `--log-format`. No `--display`.
- `uv run tolokaforge run --config examples/native/tool_use/run_config.yaml 2>&1 | head -5` — banner + `Loading configuration` lines emit on stderr; stdout carries the run-dir path at end (A4 contract). No display-mode plumbing anywhere in the callback.

## Goal

Publish a display-mode selector on the `tolokaforge` root command:

- **Root flag** `--display={full,rich,plain,log,none}` on the `cli()` group.
- **Env var** `TOLOKAFORGE_DISPLAY={full,rich,plain,log,none}` — the equivalent operator surface. Rejected values raise the same `click.UsageError` as the flag.
- **Auto-selection**: on non-TTY (piped, redirected) OR when `CI` is set in the environment → `plain`. Otherwise → `rich`.
- **Precedence** (highest to lowest): explicit `--display` flag > `TOLOKAFORGE_DISPLAY` env var > auto-selection.
- **Textual fallback** at selection time: `--display=full` rewrites to `rich` when `import textual` raises `ImportError`, with a `WARNING` log line naming the fallback. Textual is NOT added as a dependency in B2 (C3 territory), so today the fallback fires whenever `full` is chosen.
- **`--display=none` silences the shared `console` AND the tolokaforge root log handler** so `tolokaforge run --display=none` produces empty stderr on success. The single stdout write (`emit_artifact_path`) is untouched — that's the whole point of `--display=none`.
- **Stash on `ctx.obj["display_mode"]`** so B1 (#285) and any future consumer read from a single source rather than re-parsing the flag.

This is the *selector*. Rich Live rendering lands in B1; Textual TUI lands in C3. Both consume `ctx.obj["display_mode"]`.

**Composition with A3's `--log-format`** — orthogonal axes, both may be passed. `--log-format` shapes individual log lines; `--display` shapes the overall stderr UI. Table locked in `docs/CLI.md § Display modes`:

| `--display` | `--log-format` | Behaviour |
|-------------|----------------|-----------|
| `full`      | (any)          | Textual TUI when installed (C3); today falls back to `rich`. Log lines that leak into the TUI follow `--log-format`. |
| `rich`      | (any)          | Rich Live panel (B1 will land it). Log lines outside the panel follow `--log-format`. |
| `plain`     | (any)          | Human-readable log-line stream. `--log-format` picks the line shape (`pretty`/`plain`/`json`). |
| `log`       | (any)          | Pure log stream, no banners, no progress bars. `--log-format` picks the line shape. |
| `none`      | (any)          | Silent on stderr on success. `--log-format` is retained for the shape any escape-hatch log line would take, but no lines emit under normal operation. `emit_artifact_path` still writes stdout. |

## Non-goals

- **Do NOT implement B1's Rich Live panel** (#285). B2 makes `rich` PICKABLE but does not add any new rendering — the current `console.print(...)` calls stay as-is for `rich`. B1 will replace them with a Live panel when it lands.
- **Do NOT add Textual as a dependency** — C3 (TUI) is a separate milestone item. B2 wires the *selection* of `full`; the Textual-fallback branch fires unconditionally today because `import textual` fails.
- **Do NOT touch `--log-format`** (A3). It is the orthogonal axis. The `TOLOKAFORGE_LOG_FORMAT` env var equivalent — parked in A3's Non-goals — is filed as a follow-up issue (see Discovered issues); this PR only ships `TOLOKAFORGE_DISPLAY`.
- **Do NOT preempt A2 (#278 `--version` + grouped `--help`)** or **A5 (#281 dashboard-URL banner)**. Both compose with B2 later: A5 will honour `--display=none` when it lands (banner only prints when mode ∈ {full, rich, plain}); A2 is `--help`-adjacent and doesn't overlap.
- **Do NOT gate `emit_artifact_path` on display mode.** Even `--display=none` must emit the artifact path on stdout — that's what makes the mode useful in `RUN_DIR=$(tolokaforge run --display=none --config …)`.
- **Do NOT change existing `console.print(...)` call sites in `run`, `prepare`, `worker`, etc.** Under `--display=none` they are silenced by `console.quiet = True`; under any other mode they emit as today. The re-carve happens in B1 (Rich Live) and C3 (Textual).
- **Do NOT change `--verbose` / `--quiet` semantics.** Root `-q` still forces WARNING level; `--display=none` is stricter (silences below CRITICAL+1) but does not touch A3's precedence.

## Target module surface

### `tolokaforge/cli/_display.py` — new exports

```python
class DisplayMode(str, Enum):
    """Overall stderr UI selection for a tolokaforge invocation.

    Selection lives in `_display` alongside `LogFormat` (line shape) so
    every consumer imports both from one place. The values match the CLI
    flag literals — `full/rich/plain/log/none` — and the enum inherits
    from `str` so `click.Choice([m.value for m in DisplayMode])` works
    without a coercion adapter.
    """

    FULL = "full"      # Textual TUI (falls back to RICH if textual not installed)
    RICH = "rich"      # Rich Live panel (B1 will land it)
    PLAIN = "plain"    # Human-readable log-line stream (default on non-TTY / CI)
    LOG = "log"        # Pure log stream, no banners, no progress bar
    NONE = "none"      # Silent — only the artifact path on stdout


def select_display_mode(
    *,
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> DisplayMode:
    """Resolve the effective display mode.

    Precedence (highest to lowest):

    1. `explicit` — the value of `--display` on the current invocation.
    2. `env["TOLOKAFORGE_DISPLAY"]` — the env-var override.
    3. `env["CI"]` truthy → `PLAIN` (per issue AC).
    4. `stream.isatty()` truthy → `RICH`.
    5. Otherwise → `PLAIN`.

    Unrecognised values in `explicit` or the env var raise `ValueError`
    with a message naming the accepted set — the CLI wraps that in a
    `click.UsageError` at the flag boundary.

    Does NOT apply the Textual fallback (that is caller territory so the
    fallback log line can render under the active `LogFormat`).
    """


def silence_console() -> None:
    """Set `console.quiet = True` on the shared console.

    Called only by the CLI group callback when the resolved display mode
    is `DisplayMode.NONE`. Rich's `Console.quiet` short-circuits every
    `.print(...)` at buffer-check time — no output reaches the wrapped
    stream. Idempotent.
    """


def _textual_available() -> bool:
    """Return True iff `import textual` would succeed. Cheap probe used
    by the CLI to decide the `full`→`rich` fallback. Kept in `_display`
    so a future test can monkeypatch it in one place."""


__all__ = [
    "DisplayMode",
    "THEME",
    "console",
    "emit_artifact_path",
    "make_live",
    "make_progress",
    "select_display_mode",
    "silence_console",
]
```

### `tolokaforge/core/logging.py` — new helper

```python
def silence_root_logging() -> None:
    """Raise the tolokaforge root log handler above CRITICAL so no log
    record emits.

    Locates the handler tagged with `_TOLOKAFORGE_ROOT_HANDLER_SENTINEL`
    and sets `handler.level = logging.CRITICAL + 1`. Root logger level
    also bumped so origination-side filtering matches. Idempotent.

    Called only by the CLI when `--display=none` resolves.
    """
```

### `tolokaforge/cli/main.py::cli()` — new flag + wiring

```python
@click.group()
@click.option("--verbose", "-v", ...)         # A3
@click.option("--quiet", "-q", ...)            # A3
@click.option("--log-format", ...)             # A3
@click.option(
    "--display",
    "display",
    type=click.Choice([m.value for m in DisplayMode], case_sensitive=False),
    default=None,
    help=(
        "Overall stderr UI. 'full' = Textual TUI (falls back to 'rich' "
        "if textual is not installed); 'rich' = Rich Live panel; "
        "'plain' = log-line stream (default on non-TTY / CI); 'log' = "
        "pure log stream, no banners; 'none' = silent on stderr, only "
        "the artifact path on stdout. Env override: TOLOKAFORGE_DISPLAY. "
        "Orthogonal to --log-format."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: bool,
    quiet: bool,
    log_format: str | None,
    display: str | None,
) -> None:
    ...
    # After configure_root_logging(...) — A3 unchanged.
    display_mode = _resolve_display_mode(explicit=display, env=os.environ)
    ctx.obj["display_mode"] = display_mode
    if display_mode is DisplayMode.NONE:
        silence_console()
        silence_root_logging()
```

Where `_resolve_display_mode` (a private helper in `main.py`) wraps `select_display_mode(...)` with the Textual-fallback log line:

```python
def _resolve_display_mode(
    *,
    explicit: str | None,
    env: Mapping[str, str],
) -> DisplayMode:
    try:
        mode = select_display_mode(explicit=explicit, env=env)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    if mode is DisplayMode.FULL and not _textual_available():
        logging.getLogger("tolokaforge.cli").warning(
            "textual is not installed; falling back from --display=full to --display=rich"
        )
        return DisplayMode.RICH

    return mode
```

## Design decisions

### D1. Selection-time Textual fallback

Two options:

- **(a) Selection-time**: in the CLI group callback, if the resolved mode is `FULL` and `import textual` fails, rewrite to `RICH` and emit a WARNING log line. B1 / C3 consumers trust `ctx.obj["display_mode"]` blindly.
- **(b) Consumer-time**: leave the mode as `FULL`; each consumer probes for textual before entering the TUI and falls back mid-flow.

**Decision: (a) selection-time.** Rationale:

1. The fallback is a *global* decision, not a per-consumer one. If any consumer sees `FULL` in the context, it must be honoured. Selection-time keeps `ctx.obj["display_mode"]` truthful.
2. The WARNING log line lands once, at the top of the run, with the active `--log-format` already in effect (A3 configures logging before B2 resolves display). Consumer-time fallback would emit the warning mid-flow, competing with the display renderer for stderr real estate.
3. `import textual` on absence is O(1) and safe — `ImportError` at module-lookup time. The probe cost is negligible.
4. Aligns with the "fail early" posture of `AGENTS.md` Core Rule 1: the user sees the mode they actually got, at the top of the run, in the same log format they picked.

Downside: the CLI callback probes textual even for invocations that don't ask for `full`. Mitigated by the `mode is DisplayMode.FULL` guard — the probe only runs when the user actually requested `full`.

### D2. `--display=none` silencing mechanism

Three options considered:

- **(a) `console.quiet = True` alone.** Silences `console.print(...)` calls. Log lines from `configure_root_logging` still emit at INFO+ → fails AC "zero stderr output on success".
- **(b) Root handler level to CRITICAL+1 alone.** Silences log lines. `console.print(...)` calls still emit → fails AC.
- **(c) Both.** Silences everything on the shared stderr paths.

**Decision: (c) both.** Concretely, `silence_console()` (sets `console.quiet = True`) AND `silence_root_logging()` (bumps the tolokaforge sentinel handler level to `logging.CRITICAL + 1` and sets root logger level accordingly).

`console.quiet` is a documented Rich API (`.venv/lib/python3.12/site-packages/rich/console.py:704, 2050`) — `_check_buffer` short-circuits on `self.quiet` before any write, so both `.print(...)` and `.rule(...)` / `.status(...)` etc. are silenced.

`logging.CRITICAL + 1` (rather than `logging.CRITICAL`) is the belt-and-braces choice: even a CRITICAL log record (which by convention indicates unrecoverable failure) is dropped under `--display=none`. On a *success* path, no CRITICAL records emit anyway (that would be self-contradictory), so this is defensive. On a *failure* path, exit-non-zero + click's error handling + Python's uncaught traceback all bypass the tolokaforge log handler entirely — they write directly to `sys.stderr`, so the operator sees the failure regardless of `--display=none`.

**Alternative rejected: `logging.disable(logging.CRITICAL)`** — this is a process-global lever that affects every logger, including any embedder that imports `tolokaforge` as a library and installs its own handlers. `silence_root_logging()` only touches the sentinel-tagged handler, preserving embedder isolation.

### D3. Auto-selection precedence

Locked at:

1. `explicit` — from `--display` CLI flag.
2. `env["TOLOKAFORGE_DISPLAY"]` — env override.
3. `env["CI"]` truthy → `PLAIN` (issue AC).
4. `stream.isatty()` truthy → `RICH`.
5. Fallback → `PLAIN`.

**"Truthy" for `CI`**: the string is non-empty and not one of `{"0", "false", "False", "FALSE", "no", "off", ""}`. Rationale: GitHub Actions / GitLab CI / CircleCI / Buildkite / Jenkins all set `CI=true`; some scripts may explicitly export `CI=""` (empty) or `CI=0` to disable CI-mode inside a nested shell. Mirrors the convention used by `npm`, `yarn`, `cargo`, `python -m rich.pretty` and every mainstream tool.

Ordering rationale: **env var beats auto-detect** so an operator can force `--display=rich` on a piped shell by exporting `TOLOKAFORGE_DISPLAY=rich`, and can force `--display=plain` from a wrapper script by exporting `TOLOKAFORGE_DISPLAY=plain` without a CLI arg. `CI=1` is the "am I in a CI runner?" hint — subordinate to explicit env intent.

### D4. Env-var name — `TOLOKAFORGE_DISPLAY`

Matches the `TOLOKAFORGE_*` prefix already established (`TOLOKAFORGE_SECRETS_JSON`, `TOLOKAFORGE_LLM_API_CALL_TIMEOUT_S`, `TOLOKAFORGE_LLM_API_CALL_RETRIES`, `TOLOKAFORGE_OPENROUTER_REFERER`, `TOLOKAFORGE_OPENROUTER_TITLE`, `TOLOKAFORGE_OPENROUTER_OPT_OUT`). No new namespace to teach.

### D5. Selector lives in `_display.py`, not `core/logging.py` or `cli/main.py`

- `DisplayMode` is a display concern — parallel to `LogFormat` which lives in `core/logging.py` because it's a line-shape enum consumed by the formatter. `DisplayMode` is consumed by CLI callback + B1 / C3 renderers — display concerns.
- Keeping the selector next to `console` / `emit_artifact_path` / `make_live` groups every display primitive in one file. The canonical grep-guard already exempts `_display.py`; no test extension needed for the new exports.
- `silence_console()` also lives in `_display.py` since it mutates the module-level `console`. `silence_root_logging()` lives in `core/logging.py` since it mutates the log handler chain — same-neighbourhood as `configure_root_logging`.

### D6. `ctx.obj["display_mode"]` is the single-source consumers read

B1 (Rich Live panel), C3 (Textual TUI), and A5 (dashboard-URL banner) all need the resolved mode. A single `ctx.obj` key is:

- Discoverable via grep (`rg 'ctx.obj\["display_mode"\]'`).
- Parallel to A3's `ctx.obj["log_format"]` / `ctx.obj["root_verbose"]` / `ctx.obj["root_quiet"]` (locked pattern).
- Testable — the composition-matrix tests can inspect `ctx.obj` post-invocation.

The value stored is a `DisplayMode` enum, not the raw string, so consumers get `if mode is DisplayMode.FULL:` type-safety.

### D7. `--display=none` does NOT re-plumb `emit_artifact_path`

`emit_artifact_path` writes to `sys.stdout` directly via `print(str(Path(path).resolve()), file=sys.stdout, flush=True)`. It never touches the shared `console` (which is on stderr) and never uses `logging`. Both silencing knobs (`console.quiet`, root handler level) leave `sys.stdout` untouched. Existing A4 tests (`tests/unit/test_cli_display.py::TestEmitArtifactPath`) confirm the routing.

The B2 CLI integration tests re-lock this at the mode-selector level: `--display=none run` still emits exactly one line on stdout.

### D8. Textual is NOT a dependency in B2

The Textual-fallback branch fires today for any `--display=full` because `import textual` fails. This is intentional — the AC says "wire the flag; fall back if Textual not installed" — and matches the C3 boundary: C3 will `uv add textual`, and then `--display=full` will get the TUI without any B2 code change (the fallback branch stops firing because `import textual` succeeds).

## Stages

Every stage lands as one commit, has its own tests that fail without the stage, and updates the docs that describe *current state*.

### Stage 1: `DisplayMode` enum + `select_display_mode` selector + `silence_*` helpers + unit tests

- **Contract:**
  - `tolokaforge/cli/_display.py` gains `DisplayMode` (`str, Enum`) with values `FULL/RICH/PLAIN/LOG/NONE`; `select_display_mode(*, explicit, env, stream)`; `silence_console()`; `_textual_available()`. All added to `__all__`.
  - `tolokaforge/core/logging.py` gains `silence_root_logging()`.
  - No CLI wiring yet — the selector and silencers are pure functions.
- **Behaviour to lock (tier: `unit`, `tests/unit/test_display_mode.py` — new file):**
  - **Precedence — explicit wins:**
    - `select_display_mode(explicit="none", env={"CI": "1", "TOLOKAFORGE_DISPLAY": "rich"}) == DisplayMode.NONE`.
    - `select_display_mode(explicit="log", env={}) == DisplayMode.LOG`.
    - Explicit value is case-insensitive: `explicit="FULL"` → `FULL`.
  - **Precedence — env beats CI beats auto:**
    - `select_display_mode(explicit=None, env={"TOLOKAFORGE_DISPLAY": "log", "CI": "1"}) == DisplayMode.LOG` (env var wins over CI).
    - `select_display_mode(explicit=None, env={"CI": "1"}) == DisplayMode.PLAIN` (CI wins over isatty when env var absent).
    - `select_display_mode(explicit=None, env={"CI": "true"}) == DisplayMode.PLAIN`.
    - `select_display_mode(explicit=None, env={"CI": "0"}, stream=_fake_tty()) == DisplayMode.RICH` (`CI=0` is not truthy — falls through to isatty branch).
    - `select_display_mode(explicit=None, env={"CI": ""}, stream=_fake_tty()) == DisplayMode.RICH` (empty string not truthy).
    - `select_display_mode(explicit=None, env={"CI": "false"}, stream=_fake_tty()) == DisplayMode.RICH`.
    - `select_display_mode(explicit=None, env={"CI": "no"}, stream=_fake_pipe()) == DisplayMode.PLAIN` (isatty=False path).
  - **Precedence — isatty branch:**
    - `select_display_mode(explicit=None, env={}, stream=_fake_tty()) == DisplayMode.RICH`.
    - `select_display_mode(explicit=None, env={}, stream=_fake_pipe()) == DisplayMode.PLAIN`.
  - **Invalid values raise `ValueError`:**
    - `select_display_mode(explicit="wombat", env={})` raises `ValueError` with message naming the accepted set.
    - `select_display_mode(explicit=None, env={"TOLOKAFORGE_DISPLAY": "wombat"})` raises `ValueError`.
  - **Defaults:**
    - Called with no args → uses `os.environ` and `sys.stderr` implicitly. Assert that behavior via a monkeypatch: `monkeypatch.setattr(os, "environ", {"CI": "1"})` + call → returns `PLAIN`. (Locks that the defaults resolve to real process globals.)
  - **`_textual_available` probe:**
    - `_textual_available() is False` today (textual not in the venv). Called cheaply — `import` under `try/except ImportError`. Test asserts current return, and asserts a monkeypatched `sys.modules["textual"] = <fake>` flips it to True.
  - **`silence_console` mutates the shared console:**
    - Before call: `console.quiet is False`. After `silence_console()`: `console.quiet is True`. Reset via a fixture that restores `console.quiet = False` in teardown so tests don't leak state.
    - Idempotent: calling twice keeps `console.quiet is True`.
  - **`silence_root_logging` mutates the tolokaforge sentinel handler:**
    - Setup: `configure_root_logging(level=logging.INFO, log_format=LogFormat.PLAIN, stream=StringIO())`. Verify the sentinel handler has `handler.level == logging.INFO`.
    - Call `silence_root_logging()`. Assert the sentinel handler has `handler.level == logging.CRITICAL + 1`, and `logging.getLogger().level == logging.CRITICAL + 1`.
    - Emit an ERROR log line → the StringIO stream is empty.
    - Emit a CRITICAL log line → the StringIO stream is empty (CRITICAL+1 filters out CRITICAL).
    - Idempotent: calling twice keeps the level at CRITICAL+1.
  - **`DisplayMode` enum shape:**
    - `list(DisplayMode) == [DisplayMode.FULL, DisplayMode.RICH, DisplayMode.PLAIN, DisplayMode.LOG, DisplayMode.NONE]` — order matters for `--help` rendering.
    - `DisplayMode.FULL.value == "full"` (and so on for each member).
    - `DisplayMode("rich") is DisplayMode.RICH` — string constructor works (needed for `click.Choice([m.value for m in DisplayMode])`).
- **Compatibility:** internal only — no CLI or public API changes yet. New symbols are additive; `__all__` extension is a widening.
- **Deliverable:**
  - `tolokaforge/cli/_display.py` — `DisplayMode`, `select_display_mode`, `silence_console`, `_textual_available` added; `__all__` extended.
  - `tolokaforge/core/logging.py` — `silence_root_logging` added.
  - `tests/unit/test_display_mode.py` (new file).
- **Validation:**
  - `dev.run_tests(marker="unit", pattern="test_display_mode")` green.
  - `dev.lint_check(paths=["tolokaforge/cli", "tolokaforge/core", "tests/unit"])` clean.
- **Doc updates:** none yet (docs land in Stage 3 with the CLI flag).

### Stage 2: `--display` CLI flag + env-var wiring + `ctx.obj["display_mode"]` + `--display=none` silencing + CLI-level tests

- **Contract:**
  - `tolokaforge/cli/main.py::cli()` gains `--display` (`click.Choice`, `default=None`, `case_sensitive=False`).
  - The group callback:
    1. Runs `configure_root_logging(...)` — A3 unchanged.
    2. Calls `_resolve_display_mode(explicit=display, env=os.environ)` — a private helper wrapping `select_display_mode` with the Textual fallback + `click.UsageError` wrapping.
    3. Stashes the resolved mode on `ctx.obj["display_mode"]`.
    4. If mode is `DisplayMode.NONE`: calls `silence_console()` and `silence_root_logging()`.
    5. If mode is `DisplayMode.FULL` and `not _textual_available()`: logs a WARNING via `logging.getLogger("tolokaforge.cli")` naming the fallback, rewrites the mode to `RICH`, and stashes RICH.
  - Invalid env-var value raises `click.UsageError` at group-callback time (same rendering as invalid `--display` flag).
- **Behaviour to lock (tier: `unit`, `tests/unit/test_display_cli_flag.py` — new file):**
  - **Flag surface:**
    - `runner.invoke(cli, ["--display", "none", "run", "--help"])` — exits 0 (click parses; --help returns before subcommand executes).
    - `runner.invoke(cli, ["--display", "wombat", "run", "--help"])` — exits 2, stderr names accepted set.
  - **Ctx propagation (parametrised over the five modes):**
    - Inject a probe subcommand at test-import time (or reuse `run` with the stub orchestrator from `test_cli_stdout_contract.py`) that reads `ctx.obj["display_mode"]` and writes it to a side channel. Assert the stored value matches the explicit flag (for `plain/log/none`) or matches the post-fallback value (for `full` → `rich`, since textual not installed) or matches the explicit (for `rich`).
  - **Env var precedence via `CliRunner` with `env=` kwarg:**
    - `runner.invoke(cli, ["run", "--config", str(valid_config)], env={"TOLOKAFORGE_DISPLAY": "log"})` — `ctx.obj["display_mode"] == DisplayMode.LOG`.
    - `runner.invoke(cli, ["--display", "none", "run", ...], env={"TOLOKAFORGE_DISPLAY": "rich"})` — explicit beats env, mode is `NONE`.
    - `runner.invoke(cli, ["run", ...], env={"CI": "1"})` — auto-selects `PLAIN`.
    - `runner.invoke(cli, ["run", ...], env={"CI": "1", "TOLOKAFORGE_DISPLAY": "rich"})` — env beats `CI`, mode is `RICH`.
    - `runner.invoke(cli, ["run", ...], env={"TOLOKAFORGE_DISPLAY": "wombat"})` — exits 2, `click.UsageError`.
  - **Textual fallback:**
    - `runner.invoke(cli, ["--display", "full", "run", ...])` — no textual installed → `ctx.obj["display_mode"] == DisplayMode.RICH`; a WARNING log line is captured in `result.stderr` containing "textual is not installed" and "falling back". Under `--log-format=json`, the same line parses via `json.loads` and its `level` key is `"WARNING"`.
    - `runner.invoke(cli, ["--display", "full", "run", ...])` with `monkeypatch.setitem(sys.modules, "textual", <fake module>)` → `ctx.obj["display_mode"] == DisplayMode.FULL`; no fallback warning.
  - **`--display=none` silencing (this is the AC-critical test):**
    - `runner.invoke(cli, ["--display", "none", "run", "--config", str(valid_config)])` under the stub `Orchestrator` — assert `result.exit_code == 0`; `result.stderr == ""`; `result.stdout.count("\n") == 1` and is a resolvable Path (the artifact path).
    - Repeated with `TOLOKAFORGE_DISPLAY=none` env var (no CLI flag) — same assertion.
    - `--display=none` on a failure path: stub orchestrator raises → `result.exit_code != 0`; `result.stdout == ""`; the traceback / click error text still lands on `result.stderr` (Python + click write directly, not through the log handler). Assert `result.stderr != ""` on failure.
    - **Composition with `-v` / `-q` (round-1 critic addition):**
      - `runner.invoke(cli, ["--display", "none", "-v", "run", ...])` — `result.stderr == ""` (silencer wins even with `-v` requesting DEBUG).
      - `runner.invoke(cli, ["--display", "none", "-q", "run", ...])` — `result.stderr == ""` (both silence; `--display=none` wins on the composed matrix).
  - **`--display=none` does NOT gate `emit_artifact_path`:**
    - Specific regression assertion (parked from A4's plan): `runner.invoke(cli, ["--display", "none", "run", ...])` — the stdout emission fires exactly once, matching the stub orchestrator's returned Path. Locks the A4 coordination note.
  - **Composition with `--log-format`:**
    - `runner.invoke(cli, ["--display", "log", "--log-format", "json", "run", ...])` — `ctx.obj["display_mode"] == DisplayMode.LOG` AND `ctx.obj["log_format"] == LogFormat.JSON`. Both axes stored independently; neither disturbs the other.
    - `runner.invoke(cli, ["--display", "none", "--log-format", "json", "run", ...])` — `result.stderr == ""` (silencer wins even when JSON was requested).
  - **Isolation from A3:**
    - `--display=none` does NOT prevent `emit_artifact_path` from firing.
    - `--display=none` does NOT prevent click's `UsageError` on invalid inputs (`--display=none --config /nonexistent`) — the error still reaches stderr because click writes it directly.
- **Compatibility:**
  - **Root CLI flag surface** is a compatibility surface: `--display` is additive. `TOLOKAFORGE_DISPLAY` is a new env var surface. Both documented in `docs/CLI.md § Display modes` (Stage 3).
  - **`ctx.obj["display_mode"]` key** is a new consumer contract for B1 / C3. Documented in `docs/CLI.md` (Stage 3) and stable across future minor versions.
  - **`--display=none` behaviour** is new — no prior stdout/stderr contract deviated on it. Locked in Stage 3 canonical + docs.
- **Deliverable:**
  - `tolokaforge/cli/main.py` — new `--display` click option, new private helper `_resolve_display_mode`, wiring in the group callback body.
  - `tests/unit/test_display_cli_flag.py` (new file).
- **Validation:**
  - `dev.run_tests(marker="unit", pattern="test_display_cli_flag or test_display_mode")` green.
  - `dev.run_tests(marker="unit")` full suite green — regression sweep for the A3 composition-matrix tests (they should not care about `--display` being present).
  - `dev.lint_check(paths=["tolokaforge/cli", "tests/unit"])` clean.
  - Manual smoke (quote in PR body):
    - `uv run tolokaforge --help 2>&1 | grep -F -- "--display"` shows the flag.
    - `TOLOKAFORGE_DISPLAY=log uv run tolokaforge --help` exits 0 (env-var doesn't break --help).
    - `CI=1 uv run tolokaforge run --config examples/native/tool_use/run_config.yaml 2>/dev/null` prints a resolvable dir. (Uses a real model — sanity-check only; skip if no API keys.)
- **Doc updates:** none yet (Stage 3).

### Stage 3: Canonical enum surface lock + `docs/CLI.md § Display modes` + CHANGELOG

- **Contract:**
  - `tests/canonical/test_display_mode_surface.py` — new canonical test locking the `DisplayMode` enum ordering, values, and importable-from-`_display.py` surface. Parallel to the A3-era `test_display_module_exports_public_surface` in `tests/canonical/test_cli_display_invariants.py` (see if it makes more sense to *extend* that file — it already exempts `_display.py` and pins the shared surface; recommend extending, single canonical file for the display module).
  - `docs/CLI.md` gains a new `## Display modes` section after `## Structured logging` and before `## stdout / stderr contract`. Documents the flag, env var, precedence, and composition with `--log-format`. Written as current state (per `AGENTS.md` Core Rule 8) — no "previously X, now Y" framing.
  - `docs/CLI.md § stdout / stderr contract` gets one sentence added at the end noting that `--display=none` silences stderr on success but preserves the stdout artifact emission.
  - `CHANGELOG.md` — "Unreleased / Feat" entries listing the new flag, env var, and `--display=none` silence semantics.
- **Behaviour to lock (tier: `canonical`):**
  - `tests/canonical/test_cli_display_invariants.py` extended:
    - `test_display_mode_enum_surface` — asserts `list(DisplayMode.__members__)` matches the expected member names (`["FULL", "RICH", "PLAIN", "LOG", "NONE"]` in that order) and that each member's `.value` matches the expected CLI literal. Locks the flag choices against silent reordering / value drift.
    - `test_select_display_mode_is_exported` — asserts `select_display_mode` and `silence_console` are exported from `tolokaforge.cli._display`.
    - `test_silence_root_logging_is_exported` — asserts `silence_root_logging` is exported from `tolokaforge.core.logging`.
    - `test_display_env_var_literal_is_TOLOKAFORGE_DISPLAY` (round-1 critic addition) — canonical lock on the env-var name (compatibility surface). Calls `select_display_mode(explicit=None, env={"TOLOKAFORGE_DISPLAY": "log"})` and asserts the result is `DisplayMode.LOG`. A silent rename to `TOLOKAFORGE_DISPLAY_MODE` or similar would fail here.
- **Compatibility:**
  - **`docs/CLI.md § Display modes` section is a compatibility surface.** Machine consumers reading `TOLOKAFORGE_DISPLAY` documentation will grep. Any future flag change (adding a new mode, removing one, changing precedence) needs a CHANGELOG entry.
  - **`DisplayMode` enum order / values are locked** by the canonical test. A silent rename or reorder fails CI.
- **Deliverable:**
  - `tests/canonical/test_cli_display_invariants.py` — three new test functions.
  - `docs/CLI.md` — new `## Display modes` section (between `## Structured logging` and `## stdout / stderr contract`) + one added sentence in `## stdout / stderr contract`.
  - `CHANGELOG.md` — new "Feat" bullets under "Unreleased" (see below).
- **Validation:**
  - `dev.run_tests(marker="canonical", pattern="test_cli_display_invariants")` green.
  - `uv run pytest tests/unit tests/canonical -x -m "unit or canonical"` full suites green.
  - `dev.lint_check` and `dev.format_check` clean.
  - `rg "\-\-display" docs/` returns a hit only in `docs/CLI.md` (verify the section renders correctly).
- **Doc updates:**
  - `docs/CLI.md` — new section (proposed content):

    ```markdown
    ## Display modes

    The root flag `--display={full,rich,plain,log,none}` and the equivalent env var `TOLOKAFORGE_DISPLAY=…` pick the overall stderr UI. Orthogonal to `--log-format`, which shapes individual log lines.

    | Value    | Behaviour                                                                                                 |
    |----------|-----------------------------------------------------------------------------------------------------------|
    | `full`   | Textual TUI. Falls back to `rich` when textual is not installed (a WARNING log line notes the fallback).  |
    | `rich`   | Rich Live panel.                                                                                          |
    | `plain`  | Human-readable log-line stream. Default on non-TTY / when `CI` is set.                                    |
    | `log`    | Pure log stream — no banners, no progress bars.                                                           |
    | `none`   | Silent on stderr on success. `emit_artifact_path` still writes the artifact path to stdout.               |

    ### Precedence

    1. Explicit `--display=…` flag.
    2. `TOLOKAFORGE_DISPLAY=…` env var.
    3. `CI` env var truthy → `plain`.
    4. `sys.stderr.isatty()` truthy → `rich`.
    5. Otherwise → `plain`.

    ### Composition with `--log-format`

    `--display` and `--log-format` are orthogonal axes and may be combined. `--display` picks the overall UI; `--log-format` picks the line shape of any log lines the UI emits. `--display=none` silences the shared `console` (`console.quiet = True`) AND raises the tolokaforge root log handler above CRITICAL so no log record emits — `--log-format` is retained for the shape any escape-hatch line would take but has no observable effect on a success path.

    ### Failure paths under `--display=none`

    Silencing applies to the shared `console` and the tolokaforge log handler only. The following stderr sources bypass both silencing knobs and continue to write on failure — the operator sees the failure regardless of `--display=none`:

    - Click's `UsageError` output (bad flags, bad env-var values).
    - Python's uncaught-exception tracebacks.
    - `warnings.warn` calls, which write directly via `warnings.showwarning`. A `DeprecationWarning` fired inside `Orchestrator` construction leaks under `--display=none`. Intentional — warnings are diagnostic and should not be swallowed.

    ### `TOLOKAFORGE_DISPLAY=<invalid> <cmd> --help` behaviour

    The group callback validates `TOLOKAFORGE_DISPLAY` on every invocation, including subcommand `--help`. A stale export of `TOLOKAFORGE_DISPLAY=wombat` therefore fails with `click.UsageError` even for `tolokaforge run --help`. Intentional — fail-loud on operator misconfiguration matches AGENTS.md Core Rule 1; the fix is `unset TOLOKAFORGE_DISPLAY` (or export a valid value).

    ### `ctx.obj["display_mode"]`

    The group callback stashes the resolved `DisplayMode` on `ctx.obj["display_mode"]` after applying the precedence rules and Textual fallback. Consumer commands (`run`, `prepare`, and — in later milestone work — B1's Rich Live panel and C3's Textual TUI) read from this single source rather than re-parsing the flag / env var.
    ```

  - `docs/CLI.md § stdout / stderr contract` — appended sentence:

    > `--display=none` silences the shared console and the tolokaforge log handler on success; the stdout artifact-path emission is unaffected — see § Display modes.

  - `CHANGELOG.md` — under "Unreleased / Feat":

    ```markdown
    - **cli**: root flag `--display={full,rich,plain,log,none}` and env var `TOLOKAFORGE_DISPLAY=…` pick the overall stderr UI. Auto-selects `plain` when `CI` is set or when `sys.stderr` is not a TTY; auto-selects `rich` on a TTY. `--display=none` silences stderr on success while preserving the stdout artifact-path emission. `--display=full` falls back to `rich` when textual is not installed. Orthogonal to `--log-format`. See [docs/CLI.md](docs/CLI.md) § Display modes. (#282)
    ```

## Test strategy

- **Unit tier for the selector** — `test_display_mode.py`: injects `env` (plain dict), `stream` (lightweight stub with `isatty=lambda: True/False`), and `explicit` (str or None). No `sys.stderr` monkeypatching, no process-global state. Deterministic in parallel test runs.
- **Unit tier for the silencers** — `test_display_mode.py`: `silence_console` asserted against the module-level `console` with a fixture that restores `console.quiet = False` in teardown; `silence_root_logging` asserted against a `StringIO` stream passed to `configure_root_logging(stream=…)`, log record emitted, stream buffer empty.
- **Unit tier for the CLI flag** — `test_display_cli_flag.py`: `CliRunner(mix_stderr=False)` with the stub orchestrator pattern from `tests/unit/test_cli_stdout_contract.py` (`_make_stub_orchestrator`) — no LLM / Docker calls. `env=` kwarg on `runner.invoke(...)` locks env-var precedence deterministically (independent of the ambient shell). The `ctx.obj["display_mode"]` propagation is asserted via a probe: attach a `click.pass_context`-decorated inspector to the `run` command's front (via monkeypatch on `cli_main.run.callback`) OR by extending the existing stub orchestrator to record `ctx.obj["display_mode"]` in its `__init__`. Recommend the latter — one stub, both A3 and B2 tests reuse it.
- **Textual fallback** — `_textual_available()` monkeypatched via `sys.modules["textual"] = <fake>` in the "textual installed" case; native (absent) in the "not installed" case. Locks both branches of the fallback.
- **Cross-composition with `--log-format`** — the same `test_display_cli_flag.py` fixture invokes with `["--log-format", "json", "--display", "log", ...]` and asserts both `ctx.obj["log_format"]` and `ctx.obj["display_mode"]` post-invocation. Orthogonality guarantee.
- **Failure-path silencing** — the `--display=none` on failure test uses `_make_stub_orchestrator(run_raises=RuntimeError("boom"))` and asserts `result.stderr != ""` (Python traceback reaches stderr) even with `--display=none` active. Locks that silencing does NOT swallow errors.
- **Canonical tier is minimal** — one extension to `test_cli_display_invariants.py` locking the enum surface, selector export, and silence-helper export. No golden files (renderings are B1 / C3 territory; B2 is a selector).
- **No integration tier** — every branch resolves at the CLI-callback level; no real LLM / Docker interaction changes. The manual smoke line in Stage 2 covers end-to-end sanity.

## Discovered issues

- **Fix in this PR (Stage 3):**
  - `docs/CLI.md § stdout / stderr contract` currently says nothing about `--display=none`. Append one sentence pointing at `§ Display modes` so the two sections cross-reference. Cheap, in the neighbourhood.
- **Filed as follow-up issues (via `gh issue create`, `Toloka/tolokaforge`):**
  - **#319** — `cli(env): TOLOKAFORGE_LOG_FORMAT env-var equivalent for --log-format (deferred from A3/#279)`. A3's plan explicitly parked env-var log-format overrides for B2. B2's scope is `--display` + `TOLOKAFORGE_DISPLAY` only — adding a second env var to the same PR would broaden the compatibility surface without a design reason. The follow-up mirrors B2's env-var precedence (explicit > env > auto) but for `--log-format`.
  - **#320** — `cli(display): --display=full integration test once Textual is a dependency (C3 coordination)`. Currently the fallback branch fires 100% of the time in B2 tests because textual is not a dependency. Once C3 lands `uv add textual`, the "full-with-textual" branch needs a real integration test to lock the TUI-init boundary.
  - **#321** — `cli(display): --display=log banner-suppression scope — coordination with B1 (#285)`. B1 will introduce a Rich Live panel and A5 (#281) will introduce a dashboard-URL banner. The issue AC says `log` mode has "no banners, no progress bar" — B1 and A5 must read `ctx.obj["display_mode"]` and honour the invariant. Filed as a coordination note for B1's plan.
- **Not filed (rejected):**
  - "Add `--display` to `run` / `prepare` / `worker` subcommands too" — the root-only flag is intentional (mirrors `--log-format`). Subcommands inherit via `ctx.obj["display_mode"]`.
  - "Deprecate `--verbose` / `--quiet` in favour of `--display=log` / `--display=none`" — the two axes are orthogonal (verbosity level × display UI). No overlap; keep both.
  - "Add a `--display=quiet` alias for `--display=none`" — `none` is unambiguous; aliases dilute the flag surface. Reject.

## Risks / open questions

- **Textual absence today.** Textual is not a dependency; the fallback branch fires for every `--display=full`. That's the intended B2 behaviour — the issue AC calls out "fall back to `rich` if Textual not installed" explicitly. When C3 lands `uv add textual`, the fallback stops firing and `--display=full` gets the TUI without a B2 code change. Locked by the two Textual-fallback test branches (Stage 2).
- **`console.quiet` and Rich's stream posture.** `console` in `_display.py` is `Console(stderr=True, soft_wrap=True, theme=THEME)`. Setting `console.quiet = True` short-circuits `_check_buffer` before any stream write (`.venv/lib/python3.12/site-packages/rich/console.py:2050`). Verified — locked by the unit test that emits and asserts empty stderr.
- **`logging.CRITICAL + 1` vs `logging.disable`.** `silence_root_logging` bumps the tolokaforge sentinel handler only (per D2). Any embedder installing a separate handler is preserved — this matters for library consumers who might catch tolokaforge logs and route them elsewhere. If a future embedder complaint surfaces, the alternative `logging.disable(logging.CRITICAL)` is a one-line switch; today we stay handler-local for isolation.
- **`--display=none` interaction with `--verbose` / `--quiet`.** `-v` and `-q` set the root logger level via A3's `configure_root_logging`; B2 then bumps the handler level above CRITICAL if mode is `NONE`. Sequence: A3 configures the handler (`-v` → DEBUG, `-q` → WARNING), then B2 (if `--display=none`) sets it to CRITICAL+1. Result: `--display=none -v run …` and `--display=none -q run …` both produce empty stderr on success. Locked by the `--display=none` silence test.
- **Ordering with the runtime banner** (`_print_runtime_banner` in `main.py:54`). Today the banner prints via `console.print(...)` from the `run` command body. Under `--display=none` it is silenced by `console.quiet = True`. When A5 (dashboard-URL banner, #281) lands, its author must consult `ctx.obj["display_mode"]` if they want a banner-specific behaviour beyond `console.quiet`. Documented in the `docs/CLI.md § Display modes` section (D6). Not blocking.
- **`CI` truthy interpretation.** The plan uses "non-empty and not in `{'0', 'false', 'False', 'FALSE', 'no', 'off', ''}`". If a downstream CI system sets `CI=1` and also `TOLOKAFORGE_DISPLAY=rich` (unusual, but possible in a wrapper), the env var wins (D3). Locked by the "env beats CI" test.
- **`click.Choice(case_sensitive=False)`.** Click normalises the value to the exact case of one of the choices; passing `--display=FULL` yields `"full"` to the callback. Matches `--log-format`'s existing behaviour.
- **B1 / C3 consumer contract** — `ctx.obj["display_mode"]` value type is a `DisplayMode` enum (not a raw string). B1 must import `DisplayMode` and switch on enum members. Documented in the `docs/CLI.md § Display modes` "ctx.obj" subsection.
- **Test-file collision.** `tests/unit/test_display_mode.py` and `tests/unit/test_display_cli_flag.py` are new files — no conflict with existing `test_cli_display.py` (which tests `emit_artifact_path` and Console-factory helpers) or `test_logging_cli_flags.py` (which tests A3's composition matrix). Naming picked to keep grep-obviousness (`test_display_mode` = the mode selector; `test_display_cli_flag` = the click flag surface).
- **Empty stderr assertion under `CliRunner(mix_stderr=False)`.** `result.stderr == ""` is bit-exact. A stray Rich `\r` or ANSI reset would break the equality. Under `console.quiet = True` Rich emits zero bytes to the wrapped stream — no `\r`, no `\n`, no escape codes. Locked by the assert-empty test.
