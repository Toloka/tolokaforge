# Plan: A3 — Structured log format with `--verbose`/`--quiet`

Issue: Toloka/tolokaforge#279 (milestone: Terminal DX, umbrella #297)
Branch: `feat/issue-279-a3-structured-log-format` (branches off `feat/terminal-dx`, PR targets `feat/terminal-dx`)

## Context

The CLI's log output today is fragmentary. Three separate paths render logs, none share a format, all default to noisy INFO+context on the console:

- **stdlib** `logging.getLogger(__name__)` (used by `tolokaforge/docker/*`, `tolokaforge/adapters/*`, `tolokaforge/core/*`) — records emitted via `logger.info(msg, extra={...})`. **The `extra` payload is attached to `LogRecord.__dict__` but the current formatter never renders it.** No process-wide handler configuration exists in the CLI entrypoint; records reach `logging.lastResort` or a per-caller `basicConfig`. Reproduced with `run_python`:

  ```
  2026-07-14 19:54:03 - tolokaforge.demo - INFO - Loaded          # extra={"judge": "kimi-k2"} silently dropped
  ```

- **`StructuredLogger`** (`tolokaforge/core/logging.py:16`) — used by `orchestrator`, `runner/core`, `output_writer`, adapters. Owns its own `StreamHandler(sys.stdout)` (`logging.py:48`) with the legacy format `"%(asctime)s - %(name)s - %(levelname)s - %(message)s"` and `datefmt="%Y-%m-%d %H:%M:%S"` (seconds resolution, no msec). Sets `propagate = False`, so it bypasses any root handler. Inlines context kwargs into the message string (`_log` → `f"{message} ({k=v, k=v})"`), producing:

  ```
  2026-07-14 19:54:03 - trial:0 - INFO - Processing trial (task_id=task-123, trial_index=0)
  ```

- **Ad-hoc `logging.basicConfig` sites** — `tolokaforge/cli/adapter_commands.py:46` (when `convert --verbose`), `tolokaforge/runner/__main__.py:55`, `tolokaforge/env/rag_service/app.py:35`, plus a duplicate `docker_logger` handler installed inside `Orchestrator.__init__` at `orchestrator.py:280-290`. Each pins the legacy format.

There is no root-level flag surface for verbosity. Every subcommand (`run`, `prepare`, `worker`, `convert`) has its own `--verbose` flag (`main.py:158,328,385`, `adapter_commands.py:35`) whose semantics differ: on `run/prepare/worker` it flows into `Orchestrator(verbose=…)` → `StructuredLogger(level=DEBUG)`; on `convert` it fires a bare `logging.basicConfig(level=DEBUG)`.

A1 (#276) shipped the shared `_display.console` (stderr, THEME, `soft_wrap=True`). The canonical grep-guard `tests/canonical/test_cli_display_invariants.py::test_no_ad_hoc_console_in_cli` forbids new `rich.Console(...)` constructions in `tolokaforge/cli/*` — A3 must reuse the shared surface for coloured output.

## Goal

One structured formatter, one root logging bootstrap, one authoritative root flag surface.

- New `StructuredFormatter(logging.Formatter)` in `tolokaforge/core/logging.py` emits three shapes:
  - **pretty** (default when `sys.stderr.isatty()`): `HH:MM:SS.mmm | LEVEL | k=v k=v | message`, with ANSI colours mapped from A1's `THEME` — `INFO=cyan`, `WARNING=yellow`, `ERROR=bold red`, `DEBUG=dim`. Message text stays uncoloured so downstream `grep` on the message survives.
  - **plain** (default when `sys.stderr` is not a TTY): identical layout with no ANSI escape codes.
  - **json** (opt-in): one JSON object per line — `{"ts": "HH:MM:SS.mmm", "level": "INFO", "logger": "tolokaforge.orchestrator", "message": "...", "extra": {...}}`. Every line parses with `json.loads`.
- One public function `configure_root_logging(level, log_format, stream=sys.stderr) -> None` installs / replaces the root handler. Idempotent. Sets `logging.getLogger()` handler to a `StreamHandler(stream)` with `StructuredFormatter(mode=…)`.
- New root CLI flags on the `cli` group:
  - `--verbose / -v` → DEBUG.
  - `--quiet / -q` → WARNING only.
  - `--log-format={pretty,plain,json}` — `pretty` on TTY, `plain` on non-TTY, `json` opt-in.
  - Precedence: rejecting `--verbose` and `--quiet` together with `click.UsageError`; otherwise `-q > default > -v`.
- The group callback runs before every subcommand and calls `configure_root_logging(...)`.
- `StructuredLogger` is rewired to route through root — its `.info(msg, key=val)` call becomes `self.logger.log(level, msg, extra={sanitized})` with `propagate = True`. The `.logs[]` list and `save_to_file()` YAML output stay bit-identical. The private handler / private formatter go away.
- The `docker_logger` handler in `Orchestrator.__init__` (a duplicate legacy formatter) is deleted; the root handler covers `tolokaforge.docker.*` via propagation.
- The `logging.basicConfig` in `adapter_commands.py:46` goes away — the root formatter already handles the subcommand's `--verbose`.

Canonical golden files pin the exact rendered bytes per mode. A JSON-parseability test asserts `json.loads(line)` returns the expected shape.

## Non-goals

- **Runner container / RAG env service structured logging.** Both are separate processes (`runner/__main__.py`, `env/rag_service/app.py`) whose logging is set up before the CLI's group callback would ever run. Filed as #308 (runner) and #309 (rag service). Out of A3's process scope.
- **Env-var overrides for log format** (`TOLOKAFORGE_LOG_FORMAT=…`). B2 (#282) owns the `--display` env-var protocol and can extend the same protocol to `--log-format` in a follow-up. A3 wires only the CLI flags.
- **Deleting the subcommand `--verbose` flags** on `run/prepare/worker/convert`. They flow into `Orchestrator(verbose=…)` and set the *trial* log level, which is a distinct axis (per-trial `StructuredLogger.level` in `save_to_file` output). Kept as-is; deprecation is a separate hygiene follow-up.
- **A4's `stdout=artifact` split (#280).** A3 writes exclusively to stderr — that's compatible with A4's future contract, but A3 does not itself carve out the stdout channel or touch the run-dir print path.
- **B2's `--display` toggle (#282).** `--display=log` is a *display mode*; `--log-format={pretty,plain,json}` is a *line shape*. Orthogonal axes. B2 will consume A3's `configure_root_logging(…, log_format="plain")` when `--display=log` picks the log-only mode.
- **Removing `StructuredLogger` in favour of stdlib.** Deferred; #279 preserves the class semantics per the issue's Notes.

## Target formatter and configuration surface

```python
# tolokaforge/core/logging.py

class LogFormat(str, Enum):
    PRETTY = "pretty"
    PLAIN = "plain"
    JSON = "json"


class StructuredFormatter(logging.Formatter):
    """Renders LogRecord in one of three modes.

    Layout (pretty & plain):

        HH:MM:SS.mmm | LEVEL | k1=v1 k2=v2 | message

    - Time is millisecond-resolution local time by default; ``clock`` may
      inject a deterministic ``datetime`` factory for tests.
    - Scope pairs are every ``LogRecord.__dict__`` key that is NOT one of
      stdlib's built-in record attributes (computed once against a
      canonical ``_LOG_RECORD_RESERVED`` set). Keys are alphabetically
      sorted for deterministic golden files. Values render via ``repr``
      when they contain whitespace or ``|``, else bare.
    - When there are no scope pairs the middle segment is empty:
      ``HH:MM:SS.mmm | LEVEL |  | message`` (double-space preserved so
      column grep is trivial).

    JSON mode emits one JSON object per line with keys:
    ``{"ts", "level", "logger", "message", "extra"}``. ``extra`` is the
    same scope-pair dict; missing extras render as ``{}``. Non-JSONable
    values fall back to ``repr``.
    """

    def __init__(
        self,
        mode: LogFormat,
        *,
        clock: Callable[[], datetime] | None = None,
        use_colour: bool | None = None,  # None means: auto from mode (pretty=True, else False)
    ) -> None: ...

    def format(self, record: logging.LogRecord) -> str: ...


def configure_root_logging(
    *,
    level: int = logging.INFO,
    log_format: LogFormat | None = None,   # None = auto-select from stream.isatty()
    stream: TextIO | None = None,          # default: sys.stderr
) -> None:
    """Install (or replace) the tolokaforge root log handler.

    Idempotent: if a previous tolokaforge handler is installed (detected
    via a sentinel attribute on the handler), it is removed before the
    new one is added. Sets ``logging.root.level`` and returns nothing.
    """
```

Rendering contract for scope pairs:

- Reserved LogRecord attributes not rendered: `name, msg, args, levelname, levelno, pathname, filename, module, exc_info, exc_text, stack_info, lineno, funcName, created, msecs, relativeCreated, thread, threadName, processName, process, message, taskName, asctime` (canonical set — locked by a unit test that constructs a blank LogRecord and reads its `__dict__`).
- Any `_`-prefixed keys are also skipped as a general defensive rule (private attributes and any accidental leakage of internal sentinels). Note: the `_FACTORY_SENTINEL` used by `tolokaforge/secrets/log_filter.py:133` is set on the *factory function object*, not on `LogRecord.__dict__`, so it never appears as a scope key — but the `_`-prefix rule covers any similar future case.
- Ordering: alphabetical (stable, deterministic goldens).

## Design decision: subcommand `--verbose` × root `-v/-q` composition

Two axes carry the name `--verbose`: the root flag on `cli` (console log level) and the per-subcommand flag on `run/prepare/worker/convert` (per-trial `StructuredLogger.level` inside the orchestrator, which lands in `<trial>/logs.yaml`).

**Decision: option (a) — symmetric.** Subcommand `--verbose` on `run/prepare/worker/convert` continues to bump the console log level to DEBUG, matching today's behaviour (`Orchestrator(verbose=True)` currently produces visible DEBUG on the console because `StructuredLogger` owns its own stdout handler at DEBUG). After the rewire in Stage 2, `StructuredLogger` propagates through root — so the subcommand flag must explicitly bump the root handler to keep console DEBUG visible.

Rationale: users today read `tolokaforge run --verbose` as "give me DEBUG on the terminal". Silently dropping that behaviour (option b) would be a regression at the user-facing surface even if the flag continues to affect `logs.yaml`. Symmetric also aligns `run/prepare/worker` with `convert` (which already forces DEBUG on console when `--verbose` is set).

**Precedence table** (locked by parametrized tests in Stage 2):

| Root flag | Subcommand `--verbose` | Console level | `logs.yaml` level |
|-----------|------------------------|---------------|-------------------|
| (none)    | (none)                 | INFO          | INFO              |
| `-v`      | (none)                 | DEBUG         | INFO              |
| `-q`      | (none)                 | WARNING       | INFO              |
| `-v` `-q` | any                    | exit 2 with `UsageError("--verbose and --quiet are mutually exclusive")` | — |
| (none)    | `--verbose`            | DEBUG         | DEBUG             |
| `-v`      | `--verbose`            | DEBUG         | DEBUG             |
| `-q`      | `--verbose`            | WARNING (root `-q` wins on console) | DEBUG |

The rule for the last row: root `-q` is *explicit* user intent to silence the console; subcommand `--verbose` still affects the orchestrator's per-trial logger (and therefore `logs.yaml`), but does not override the root's console-quiet decision. Implementation: subcommand handlers pass their `--verbose` value into `Orchestrator(verbose=…)` unconditionally, and additionally call `configure_root_logging(level=DEBUG)` only when the click context's root-level flag was not `-q`. A tiny helper on the click `ctx.obj` (`ctx.obj["root_quiet"] = True/False`) records the root's decision so subcommands can inspect it.

## Stages

Every stage lands as one commit, has its own tests that would fail without the stage, and updates the docs that describe *current state*.

### Stage 1: `StructuredFormatter` + `configure_root_logging` (formatter only, not yet wired)

- **Contract:** the two symbols above (`StructuredFormatter`, `configure_root_logging`, and the `LogFormat` enum) are added to `tolokaforge/core/logging.py`. No production caller is changed yet.
- **Behaviour to lock (tier: `unit`):**
  - `StructuredFormatter(mode=PLAIN, clock=fixed).format(record_bare)` returns exactly `"14:30:00.500 | INFO |  | started"` (double-space around empty scope segment).
  - `StructuredFormatter(mode=PLAIN, clock=fixed).format(record_with_extra)` returns `"14:30:00.500 | INFO | judge=kimi-k2 run_id=abc sample=42 | trial started"` — keys alphabetically sorted.
  - `StructuredFormatter(mode=JSON, clock=fixed).format(record_with_extra)` produces exactly one JSON object; `json.loads(line)` returns `{"ts": "14:30:00.500", "level": "INFO", "logger": "tolokaforge.orch", "message": "trial started", "extra": {"judge": "kimi-k2", "run_id": "abc", "sample": 42}}`.
  - `StructuredFormatter(mode=PRETTY, clock=fixed).format(record_info)` starts with `"\x1b[36m14:30:00.500"` (cyan escape code for INFO), ends with the same message payload plus a reset (`"\x1b[0m"`).
  - `configure_root_logging(level=DEBUG, log_format=None, stream=<pipe>)` — with a non-TTY stream — auto-selects `PLAIN`. Called twice, only one handler is present on `logging.root` (idempotence).
  - **isatty auto-selection (both directions).** Two tests using in-memory fake streams (no `sys.stderr` monkeypatching):
    - `configure_root_logging(log_format=None, stream=fake_tty)` where `fake_tty` is a `StringIO`-like object with `isatty=lambda: True` → the installed handler's formatter mode is `PRETTY`.
    - `configure_root_logging(log_format=None, stream=fake_pipe)` where `fake_pipe.isatty` returns `False` → mode is `PLAIN`.
  - Scope pairs are filtered against the canonical reserved-attribute set — a synthetic `LogRecord` with every reserved key set to a distinguishable value renders `k=v | message` with no reserved leakage.
  - Values containing whitespace or `|` render via `repr` (`k='hello world'` / `k='pipe|here'`).
  - `_`-prefixed extra keys are dropped (defensive rule): a record with `LogRecord.__dict__["_internal"] = "hidden"` renders with no `_internal=…` pair.
- **Compatibility:** internal only — no CLI or public API changes yet. New symbols are additive.
- **Deliverable:**
  - `tolokaforge/core/logging.py` gains `LogFormat`, `StructuredFormatter`, `configure_root_logging`, and the reserved-attribute set as a module-level frozenset.
  - `tests/unit/test_logging_formatter.py` (new file).
- **Validation:** `dev.run_tests(marker="unit", pattern="test_logging_formatter")`; ruff clean.
- **Doc updates:** none in this stage (docs get updated in Stage 3 when the flags land).

### Stage 2: Root CLI flags + wiring + StructuredLogger rewire + legacy cleanups

- **Contract:**
  - `@cli.command()` group `cli` in `tolokaforge/cli/main.py` gains:
    - `--verbose / -v` — flag, mutually exclusive with `--quiet`.
    - `--quiet / -q` — flag.
    - `--log-format` — `click.Choice(["pretty", "plain", "json"], case_sensitive=False)`, default `None` (auto).
    - Precedence: both set → `click.UsageError("--verbose and --quiet are mutually exclusive")`. Otherwise level resolves to `WARNING` (`-q`) / `DEBUG` (`-v`) / `INFO` (neither).
    - The group's callback (`cli()`) calls `configure_root_logging(level=…, log_format=…)` before any subcommand runs, and stores the resolved decision on `ctx.obj` (at minimum: `ctx.obj["root_quiet"] = bool`, `ctx.obj["root_verbose"] = bool`) so subcommands can honour it.
    - `install_global_redactor()` continues to run — the redacting record factory sits *upstream* of the new formatter, so secret scrubbing keeps working.
  - `StructuredLogger._log(...)` in `tolokaforge/core/logging.py` — its stdlib delegate call becomes `self.logger.log(log_level, message, extra=self._sanitize_extra(full_context))`; the appended `" ({k=v, ..})"` string is removed. `self.logger.propagate` is flipped to `True` and the private `StreamHandler` in `__init__` is deleted. `_sanitize_extra` renames any key that collides with a reserved LogRecord attribute (prefix `ctx_`) so `.extra=` never raises.
  - Duplicate `docker_logger` handler installation in `tolokaforge/core/orchestrator.py:280-290` is deleted — the root handler receives its records via propagation. `docker_logger.setLevel(log_level)` stays (per-namespace threshold) but no handler / formatter attached.
  - `logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)` at `tolokaforge/cli/adapter_commands.py:46` is deleted — the root formatter handles it.
  - **Subcommand `--verbose` wiring (symmetric, option a — see "Design decision" section above).** On `run/prepare/worker/convert`, when `--verbose` is set: the handler passes `verbose=True` to `Orchestrator` (unchanged; drives per-trial `logs.yaml` level) AND — provided `ctx.obj["root_quiet"]` is `False` — calls `configure_root_logging(level=logging.DEBUG)` to bump the root handler so console DEBUG is visible. If root `-q` was set, the console call is skipped (root wins on console suppression); the orchestrator still gets `verbose=True`.
- **Behaviour to lock (tier: `unit`):**
  - **Composition matrix** (parametrized test — each row spies via a `logging.Handler` attached AFTER group callback returns, and inspects both the spy's captured records AND the trial-level `logs.yaml` where relevant):
    - `["-v", "run", ...]` → spy sees a DEBUG record (root level = DEBUG).
    - `["-q", "run", ...]` → spy does NOT see an INFO record; sees WARNING (root level = WARNING).
    - `["-v", "-q", "run", ...]` → exit code 2, stderr contains `--verbose and --quiet are mutually exclusive`.
    - `["-v", "run", "--verbose", ...]` → spy sees DEBUG record; `Orchestrator(verbose=True)` invoked (asserted via a mocked constructor or via a run-fixture that captures the ctor kwargs).
    - `["-q", "run", "--verbose", ...]` → spy does NOT see any DEBUG or INFO record (root `-q` wins on console); AND `Orchestrator(verbose=True)` is still invoked (subcommand verbose still affects per-trial output).
    - `["run", "--verbose", ...]` (no root flag) → spy sees DEBUG record (subcommand verbose bumps console to DEBUG when root neither -v nor -q); `Orchestrator(verbose=True)` invoked.
    - Repeat the two subcommand-verbose bumping rows for `prepare --verbose`, `worker --verbose`, and `convert --verbose` so all four subcommands are locked symmetrically.
  - `CliRunner().invoke(cli, ["--log-format", "json", "--help"])` — help exits cleanly (proves the option parses).
  - `StructuredLogger("orch").info("hello", task_id="t1")` produces a stdlib `LogRecord` whose `__dict__["task_id"] == "t1"`. `StructuredLogger.logs[0]["context"]["task_id"] == "t1"` (in-memory list unchanged).
  - After `configure_root_logging(log_format=PLAIN)`, calling `logging.getLogger("tolokaforge.demo").info("started", extra={"k": "v"})` emits exactly one line to stderr matching `r"^\d\d:\d\d:\d\d\.\d\d\d \| INFO \| k=v \| started$"`.
  - **`tolokaforge.docker.*` records reach root exactly once** after the orchestrator no longer installs its own handler. Concrete assertion: with a single spy handler attached to `logging.root`, `docker_logger.info("pulled image")` produces `captured_lines.count("pulled image") == 1`. This locks *absence-of-double* — a future refactor that re-adds a docker-namespace handler would produce `count == 2` and fail this test.
- **Compatibility:**
  - **Root CLI flag surface** is a compatibility surface. Added flags are additive; `--verbose`/`--quiet` mutual exclusion is documented in `--help` and `docs/CLI.md` (Stage 3). No existing subcommand flag is removed.
  - **`StructuredLogger` public API** unchanged: method signatures, `.logs` list, `save_to_file()` output, strict-mode raise semantics — all bit-identical. The change is internal (private handler removal + `extra=` routing). Migration note: none needed — no caller reads the private handler.
  - **`StructuredLogger` on-console rendering** changes shape (from `"msg (k=v, k=v)"` to root-formatter's `"HH:MM:SS.mmm | INFO | k=v k=v | msg"`). Console output is not a compatibility surface (no test asserts on it today; no external contract). Documented in `docs/LOGGING.md` (Stage 3).
  - **StructuredLogger console output moves from `sys.stdout` (current `tolokaforge/core/logging.py:48`) to `sys.stderr`** via the new root handler — aligned with A4's stdout carveout (#280). Downstream consumers that piped tolokaforge's stdout to capture log lines will now see them on stderr. Documented in `docs/LOGGING.md` and `CHANGELOG.md` (Stage 3); called out explicitly in the PR body so any external stdout-consumers can adjust.
  - **Subcommand `--verbose` × root `-q` interaction** is a new documented behaviour (see "Design decision" section). Locked by the composition matrix test above.
- **Deliverable:**
  - `tolokaforge/cli/main.py` — group flags + `configure_root_logging(...)` call in `cli()` body; `ctx.obj["root_quiet"]` / `ctx.obj["root_verbose"]` stashed.
  - `tolokaforge/cli/main.py` — `run`, `prepare`, `worker` subcommand handlers gain the honour-root-quiet subcommand-verbose bump: `if verbose and not ctx.obj["root_quiet"]: configure_root_logging(level=logging.DEBUG)`.
  - `tolokaforge/cli/adapter_commands.py` — same subcommand-verbose bump on `convert`; the existing `logging.basicConfig` at line 46 is deleted.
  - `tolokaforge/core/logging.py` — `StructuredLogger._log`, `_sanitize_extra`, `__init__` no longer install a handler, `propagate = True`.
  - `tolokaforge/core/orchestrator.py:280-290` — deleted (kept the `setLevel` line if it survives review).
  - `tests/unit/test_logging_cli_flags.py` (new) — parametrized composition matrix.
  - `tests/unit/test_logging.py` — update `test_structured_logger_basic` if it starts asserting on the private handler (spot check; today's assertions are on `.logs[]` only, so no change expected).
- **Validation:** `dev.run_tests(marker="unit", pattern="test_logging")`; smoke `uv run tolokaforge --help` and `uv run tolokaforge -v run --help` both exit 0 with expected flag lines.
- **Doc updates:** none yet; docs land in Stage 3 with the golden file.

### Stage 3: Canonical golden files + docs rewrite

- **Contract:** golden files pin exact rendered bytes per mode and lock the JSON schema.
- **Behaviour to lock (tier: `canonical`):**
  - `tests/canonical/golden/logging/pretty__scoped.log` — one line, ANSI-included, with the exact text produced by `StructuredFormatter(PRETTY, clock=fixed_20260714_143000_500)` for a record with logger `tolokaforge.orch`, level `INFO`, message `trial started`, and `extra={"judge": "kimi-k2", "sample": 42, "run_id": "abc"}`.
  - `tests/canonical/golden/logging/plain__scoped.log` — same record, plain mode.
  - `tests/canonical/golden/logging/json__scoped.log` — one JSON line, same record.
  - `pretty__bare.log`, `plain__bare.log`, `json__bare.log` — same records without any extras.
  - `pretty__warning.log`, `pretty__error.log`, `pretty__debug.log` — the three other levels (locks the ANSI palette).
  - `pretty__reserved_collision.log` — a record whose extras include a key that shadows a reserved LogRecord attribute (e.g. `extra={"module": "shadowed"}`) — locks the `_sanitize_extra` `ctx_module` rename.
  - `tests/canonical/test_logging_golden.py` compares each generated line byte-for-byte against the golden. JSON files additionally round-trip through `json.loads` before diffing (so trailing-newline drift doesn't leak into the comparison).
  - JSON parseability assertion: for every JSON golden, `json.loads(open(path).read().rstrip("\n"))` returns a dict with exactly the keys `{"ts", "level", "logger", "message", "extra"}` and expected values.
- **Compatibility:**
  - **JSON schema** is a compatibility surface — machine consumers will grep. The `{ts, level, logger, message, extra}` shape is locked here; a schema change in a future PR needs a CHANGELOG entry and doc update.
  - **Pretty/plain layout** is a compatibility surface for grep patterns; column shape (`HH:MM:SS.mmm | LEVEL | k=v | message`) is locked by the golden files.
- **Deliverable:**
  - `tests/canonical/golden/logging/*.log` — 10 golden files (3 modes × {bare, scoped} + pretty {warning, error, debug, reserved_collision}).
  - `tests/canonical/test_logging_golden.py` (new).
  - `docs/CLI.md` — new section **"Log format & verbosity"** documenting the three root flags, precedence, auto-selection, and the exact `HH:MM:SS.mmm | LEVEL | k=v | message` line contract. Rewrite: this reads as if the new format is the only format that ever existed — no "previously" or "as of A3".
  - `docs/LOGGING.md` — rewrite § **Console Output** (line 148 today) to show the new format; rewrite § **CLI Flags** (line 100) to document the root `-v/-q/--log-format` surface and describe the subcommand `--verbose` × root `-q` composition rule (root `-q` wins on console; subcommand `--verbose` still drives `logs.yaml`). Add a one-paragraph § **Log stream** noting that all tolokaforge log output goes to `sys.stderr` — including records that historically went to `sys.stdout` via `StructuredLogger`.
  - `CHANGELOG.md` — two lines:
    - "Structured log format (`HH:MM:SS.mmm | LEVEL | k=v | msg`) with root `--verbose`/`--quiet`/`--log-format`. See docs/CLI.md."
    - "BREAKING (observable): `StructuredLogger` console output now goes to `sys.stderr` (previously `sys.stdout`). Downstream stdout-consumers should switch to `2>&1` or `--log-format=json` on stderr."
- **Validation:** `dev.run_tests(marker="canonical", pattern="test_logging_golden")`; grep every doc for stale format strings (`rg "%(asctime)s"` in `docs/` should return zero hits after the rewrite).

## Test strategy

- **Fixed-clock injection.** `StructuredFormatter.__init__` accepts an optional `clock: Callable[[], datetime]` argument (default: `lambda: datetime.now()`). Tests instantiate with `clock=lambda: datetime(2026, 7, 14, 14, 30, 0, 500_000)` — millisecond precision baked into a stable ISO instant. Golden files are generated once against this clock.
- **isatty auto-selection uses fake streams — no `sys.stderr` monkeypatching.** Both directions covered:
  - Pretty: a lightweight stub with `write=…, flush=…, isatty=lambda: True` is passed as `stream=` — `configure_root_logging(log_format=None, stream=fake_tty)` selects `PRETTY`.
  - Plain: same stub with `isatty=lambda: False` — selects `PLAIN`.
  This avoids process-global `sys.stderr` monkeypatching flakiness and keeps the tests deterministic in parallel test runs.
- **ANSI escape assertions.** Pretty-mode goldens include raw ANSI. `test_logging_formatter.py` asserts the level-prefix escape codes (`\x1b[36m` for INFO, `\x1b[33m` for WARN, `\x1b[1;31m` for ERROR, `\x1b[2m` for DEBUG) and the trailing `\x1b[0m`.
- **Golden regeneration** guard: `dev.update_canonical_snapshots` is the escape hatch; the test invokes the formatter with the fixed clock, so regeneration is deterministic.
- **Redaction interaction.** `test_logging_formatter_redaction.py` — install `SecretManager` with a fake secret, emit a record containing that secret, assert the formatted line has `***REDACTED***` and no leak. Locks the invariant that the record factory scrubs before the formatter renders.
- **CliRunner assertions on level.** `test_logging_cli_flags.py` uses a spy handler attached after `configure_root_logging` returns, so it reads the fully installed handler chain rather than intercepting mid-configuration.
- **Orchestrator constructor capture.** The composition-matrix rows that assert `Orchestrator(verbose=True)` was invoked use a `monkeypatch.setattr` on `Orchestrator.__init__` (or a light shim that records the `verbose` kwarg) rather than running a real orchestrator — cheap, deterministic, needs no LLM/docker.
- **Docker-namespace absence-of-double.** The docker propagation test attaches exactly one spy `Handler` to `logging.root`, emits one record via `docker_logger.info("pulled image")`, and asserts `captured_lines.count("pulled image") == 1`. Locks that no docker-namespace handler is re-added silently in the future.

## Discovered issues

- **Fix in this PR (Stage 2):**
  - Delete duplicate `docker_logger` handler installation in `tolokaforge/core/orchestrator.py:280-290`. Today it forces the legacy format on every `tolokaforge.docker.*` record and would double-render alongside the new root formatter.
  - Delete `logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)` at `tolokaforge/cli/adapter_commands.py:46`. Root formatter subsumes it; the subcommand `--verbose` calls `configure_root_logging(level=DEBUG)` instead.
- **Filed as issues:**
  - **#308** — Runner container (`tolokaforge/runner/__main__.py:55`) bypasses `--log-format`; needs env-var propagation.
  - **#309** — RAG env service (`tolokaforge/env/rag_service/app.py:35`) same class of drift.
- **Deferred / not in scope:**
  - Deleting `--verbose` on `run/prepare/worker/convert` — kept for back-compat (per-trial log-level knob). Future hygiene issue.
  - Env-var equivalents for `--log-format` — B2 (#282) territory.

## Risks / open questions

- **Line-shape lock is a compatibility surface.** Once a machine consumer greps `HH:MM:SS.mmm | INFO | ...`, changing the delimiter (e.g. two-space vs single-pipe) breaks them. Locked by canonical goldens; changes need a CHANGELOG entry.
- **JSON key stability.** `{ts, level, logger, message, extra}` chosen deliberately: `logger` (not `module`) matches the stdlib attribute; `extra` (not `context`) matches `logger.info(msg, extra=…)` idiom. Deviating from `StructuredLogger.save_to_file()`'s `{timestamp, level, module, message, context}` shape — that on-disk YAML is a separate contract (`docs/OUTPUT_FORMAT.md`) and stays unchanged. **Open question:** should the on-disk YAML shape converge on the same key names in a follow-up? Recommend yes (separate PR, semver-aware) — out of A3's scope.
- **ANSI on Windows CMD.** Modern Windows Terminal handles ANSI, but classic `cmd.exe` does not. `sys.stderr.isatty()` returns True in both. Users on classic `cmd.exe` see raw escape codes — the escape hatch is `--log-format=plain`. Documented explicitly.
- **Rich soft-wrapping vs pipe safety.** A1's `_display.console` is `soft_wrap=True`. The new handler writes to `sys.stderr` (the raw stream, not through Rich), so it is unaffected by soft-wrap policy. Consistent with the "structured, greppable" goal — Rich's wrapping would break `grep -F`.
- **B2 collision.** B2's `--display={full,rich,plain,log,none}` is a display-mode selector; A3's `--log-format={pretty,plain,json}` is a line-shape selector. No name overlap; both may coexist. Document the composition rule in B2.
- **Subcommand `--verbose` semantics.** Resolved (see "Design decision" section): option (a) symmetric — subcommand `--verbose` on `run/prepare/worker/convert` bumps both `Orchestrator(verbose=True)` (per-trial `logs.yaml` DEBUG) AND, when root `-q` is not set, the root handler to DEBUG so console output stays consistent with today's behaviour. Root `-q` wins on console suppression. Documented in `docs/LOGGING.md`; locked by the Stage 2 composition matrix test. Renaming one of the two flags to disambiguate the axes is a separate hygiene follow-up.
