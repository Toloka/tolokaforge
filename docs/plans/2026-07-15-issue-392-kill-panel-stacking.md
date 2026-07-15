# Plan: Kill Rich Live panel stacking (diagnostic-first)

Issue: #392
Branch: fix/kill-panel-stacking
Base/target branch: `feat/terminal-dx` (NOT `main` — panel plumbing lives there)

## Context

Under `--display=rich`, the `LiveRunDisplay` panel stacks duplicate copies of
itself *during trial execution*. Three prior fixes on `feat/terminal-dx`
(`d2fff0a`, `4c8be0a`, `ce29487`) closed three write channels that bypass
Rich Live's cursor bookkeeping, but one remained open on the trial hot path.

Diagnosis (run through the real `litellm` + `LiveRunDisplay` code path, not by
reading): **litellm installs its own `logging.StreamHandler` on the `LiteLLM`,
`LiteLLM Router`, and `LiteLLM Proxy` loggers at import time, pointing at the
`sys.stderr` object captured before Rich Live starts, while leaving
`propagate=True`.** On the trial hot path litellm emits INFO/WARNING records
(cost, routing, retries). Each record is written **twice**: once through
litellm's private handler straight to the captured raw stderr (bypassing Rich
Live's cursor coordination → the panel re-appends below the raw line instead of
overwriting in place → stacked copies), and once via propagation to the root
`_LogSink` (which routes it correctly). The existing `__enter__` swap only
replaces sentinel-tagged handlers **on the root logger**; litellm's handlers
live on child loggers and are never touched.

Deterministic repro (no API call): with `configure_root_logging(level=INFO)`
active and a `LiveRunDisplay` entered, `verbose_logger.info(...)` on the
`LiteLLM` logger produces a raw litellm-format line on the captured stderr that
bypasses Live — while the same record also lands in the panel's `_LogSink`
buffer. Removing litellm's private handlers for the Live lifetime eliminates the
raw write; the record still reaches the panel via propagation to root's
`_LogSink`.

## Goal

1. Land a reusable, env-gated diagnostic (`_StderrProbe`) that records every
   write to the process's real stderr stream with a caller stack trace, so any
   present or future stderr-bypass channel is identifiable from a live run.
2. Close the litellm (and any other child-logger) stderr-bypass channel for the
   `LiveRunDisplay` lifetime, so no logging handler writes to a stream that
   bypasses Rich Live's cursor coordination while the panel is active.

Observable contract after the fix: during a `--display=rich` run, the only
writes reaching the terminal stream are Rich Live's own coordinated renders and
the `_LogSink.print_above` (WARNING+) lines it routes through Live's console.
No stacked panel copies.

## Non-goals

- Boot-phase stacking (already fixed by the three prior commits).
- Textual TUI stability (Textual owns the screen; unaffected).
- The boot-log-tail widget (#394).
- litellm's process-wide **double-emission** outside the Live lifetime
  (duplicate log lines under `--display={plain,log}`, and litellm-format lines
  ignoring `--log-format`). Filed as #396 — the fix here neutralises the
  handlers only while the panel is active.
- fd-level (`os.dup2`) stderr capture for subprocess/raw-fd leaks (issue
  candidate #2). The confirmed channel is Python-level; an object-level probe is
  sufficient. See Risks.

## Stages

### Stage 1: `_StderrProbe` diagnostic

- **Contract:**
  - New class `_StderrProbe` in `tolokaforge/dx/live_panel.py`, a context
    manager. Constructor takes the target log-file `path: Path`.
  - On `__enter__`: wraps the **underlying real stderr stream object's `write`**
    — i.e. the `sys.stderr` resolved *before* Rich Live installs its
    redirect proxy — with a tap. This is load-bearing: litellm's handler holds
    the captured stream *object*, so re-binding the `sys.stderr` *name* after
    Rich's proxy is installed would miss the leak. The probe must therefore be
    installed at the very top of `LiveRunDisplay.__enter__`, before
    `self._live.__enter__()`, and record the stream object it wrapped.
  - For every `write(chunk)` call the tap appends one entry to the log file:
    `{ISO-ts} | {caller file:line} | {repr(chunk)[:200]}` followed by a compact
    stack trace (top 5 frames), then delegates to the original `write` so
    terminal output is unaffected.
  - On `__exit__`: restores the original `write`, flushes/closes the log file.
  - Env gate lives in `LiveRunDisplay.__enter__`: read
    `os.environ.get("TOLOKAFORGE_STDERR_PROBE")`. When set, wrap; when unset,
    the tap is never installed (zero production overhead). This env var is a file
    path, not a credential — it does **not** match the `_CREDENTIAL_PAT`
    grep-guard in `tests/unit/secrets/test_no_raw_secret_access.py`, so a plain
    `os.environ.get` is permitted here (reviewer note).
- **Behaviour to lock (unit):** in `tests/unit/test_run_display.py` —
  - A `_StderrProbe(tmp_path/"probe.log")` context: `sys.stderr.write("noise")`
    inside its lifetime produces a log-file entry containing the chunk repr and
    a stack trace naming the calling frame; the original `write` still received
    the chunk (captured via a fake stream).
  - After `__exit__`, the wrapped stream's `write` is the original again (a
    subsequent write is NOT recorded).
- **Compatibility:** internal only. `_StderrProbe` is a private diagnostic; no
  CLI flag, no public API, no config field.
- **Deliverable:** `_StderrProbe` class + env-gated installation in
  `LiveRunDisplay.__enter__`/`__exit__`; unit tests green.
- **Validation:** `run_tests` marker `unit` for `tests/unit/test_run_display.py`;
  `lint_check` + `format_check`. Reviewer checks the probe wraps the stream
  object (not the `sys.stderr` name post-proxy) and is fully off when the env
  var is unset.
- **Doc updates:** none in this stage (private diagnostic; documented alongside
  the fix in Stage 2's `docs/CLI.md` edit).

### Stage 2: Neutralise child-logger stderr-bypass handlers for the Live lifetime

- **Contract:**
  - `LiveRunDisplay.__enter__` extends its existing handler-swap so that, in
    addition to swapping sentinel-tagged root handlers, it sweeps **every**
    non-root logger in `logging.root.manager.loggerDict` and, for each handler
    that is a `logging.StreamHandler` whose `.stream` is one of the pre-Live
    "dangerous" terminal streams (`sys.stderr`, `sys.stdout`, `sys.__stderr__`,
    `sys.__stdout__`, captured at `__enter__`), removes that handler for the Live
    lifetime. Because these child loggers propagate to root (`propagate=True`
    for the litellm loggers), removal routes their records through the root
    `_LogSink` — INFO/DEBUG buffered, WARNING+ printed above the panel via
    Live's console.
  - **`loggerDict` entries are `logging.Logger` OR `logging.PlaceHolder`; the
    sweep MUST skip placeholders.** `PlaceHolder` has no `.handlers` attribute —
    a naive `for lg in loggerDict.values(): lg.handlers` raises `AttributeError`
    inside `__enter__`, crashing the exact display the fix targets. The sweep
    iterates only `v for v in loggerDict.values() if isinstance(v, logging.Logger)`.
    This crash escapes clean-state unit tests (which may hold zero placeholders),
    so it is locked with a dedicated assertion below.
  - Defensive branch: if a swept logger has `propagate=False`, install a
    `_LogSink` on it (so its records still surface through Live) rather than only
    removing the handler. The litellm loggers propagate, so removal alone
    suffices for the confirmed channel; the `propagate=False` branch guards
    future chatty libraries.
  - `__exit__` restores every removed handler (and drops any `_LogSink` it
    installed on a `propagate=False` logger), symmetric with the existing
    root-handler restore. The predicate is stream-identity based (no hardcoded
    logger names) so it is general and does not couple `dx` to litellm
    internals (AGENTS.md Core Rule 6).
- **Behaviour to lock (unit):** in `tests/unit/test_run_display.py`, exercising
  the **real** `__enter__`/`__exit__` code path (not a mock of it):
  - *Test 1 — bypassing handler removed:* monkeypatch `sys.stderr` to a fake
    stream, then install a child logger `"chatty"` with a
    `StreamHandler(sys.stderr)` (bound to the **captured dangerous stream**, so
    the stream-identity predicate matches — mirror Test 3's binding pattern; do
    NOT bind to an arbitrary `StringIO`, which would not match and would tempt a
    loosened "remove all StreamHandlers" predicate) and `propagate=True`. Enter
    `LiveRunDisplay`; assert the child logger's bypassing handler is gone during
    the Live lifetime, a `logger.warning(...)` on `"chatty"` writes **nothing**
    to the fake bypass stream, and the record lands in `display.log_records()`
    (routed via propagation → root `_LogSink`). After `__exit__`, the child
    handler is restored.
  - *Test 2 — non-dangerous handler untouched:* a child logger whose handler
    writes to an unrelated `io.StringIO` (not a captured dangerous stream) is
    left untouched during the Live lifetime.
  - *Test 3 — litellm-shape regression guard (no litellm import):* a
    `propagate=True` child logger with a `StreamHandler` bound to the captured
    `sys.stderr` object emits zero raw writes to that object during the Live
    lifetime.
  - *Test 4 — `propagate=False` records still surface:* a `propagate=False`
    child logger with a `StreamHandler` bound to a captured dangerous stream.
    Enter `LiveRunDisplay`; assert its bypassing handler is removed AND a
    `_LogSink` is installed on that logger, a `logger.warning(...)` lands in
    `display.log_records()` (would be silently dropped without the `_LogSink`
    branch — AGENTS.md Core Rule 1), and after `__exit__` the original handler
    is restored and the `_LogSink` removed. This locks the defensive branch that
    is otherwise dead code for the confirmed (all-`propagate=True`) channel.
  - *Test 5 — PlaceHolder does not crash the sweep:* seed
    `logging.getLogger("a.b.c")` so `logging.root.manager.loggerDict["a.b"]` is
    a `logging.PlaceHolder`, then assert `with LiveRunDisplay(): ...` enters and
    exits without raising. This is the regression lock for the 🟠 crash that
    escapes clean-state tests.
- **Compatibility:** internal only — log routing behaviour during the Live
  context. No CLI flag, config field, or public-API change.
- **Deliverable:** generalised handler sweep in
  `LiveRunDisplay.__enter__`/`__exit__`; unit tests green; the panel no longer
  stacks under a real run.
- **Validation:**
  - `run_tests` marker `unit` (`tests/unit/test_run_display.py`) + the full
    3227-test unit+canonical baseline stays green.
  - **Live acceptance (implementer + reviewer run this):**
    `TOLOKAFORGE_STDERR_PROBE=/tmp/probe.log scripts/with_env.sh uv run tolokaforge run --config examples/native/coding/run_config.yaml`
    (2 workers, 8 turns, real OpenRouter key in `.env`). Read `/tmp/probe.log`:
    during the trial-execution phase every recorded write must originate from a
    Rich render frame (`rich/...`) or the `_LogSink.print_above` path
    (`tolokaforge/.../logging` / `_LogSink`). **Zero writes with a third-party
    caller frame (e.g. `litellm/...`).** Visual: no stacked panel copies across
    turns 0→N.
    - AC refinement vs. the ticket: the ticket's literal wording ("zero writes
      originating from anywhere other than `_LogSink`") cannot hold, because the
      probe wraps the same underlying stream Rich Live renders through — Rich's
      own coordinated renders WILL appear in the probe log. The testable
      criterion is "zero **bypassing** writes": no write whose stack lacks a
      Rich/`_LogSink` frame. State this in the PR description.
- **Doc updates:** rewrite `docs/CLI.md:152` (the `_LogSink` paragraph under
  "Live run panel") so it reads as the only state: log records from
  `configure_root_logging` **and any child logger whose handler would otherwise
  write straight to the terminal stream** are routed through `_LogSink` for the
  Live lifetime — INFO/DEBUG buffered, WARNING+ printed above the panel — and
  restored on `__exit__`. **The rewrite must REPLACE the stale clause "WARNING
  and above still write to the original stderr the sentinel handler was wired
  to" — this is already inaccurate (since `ce29487` WARNING+ is routed through
  `live.console.print` / `print_above`, not raw stderr; see live_panel.py:503-511
  and the `_LogSink` docstring). Do not append the child-logger sentence beside
  the stale clause; delete the stale wording.** Add a one-line note that this is
  what prevents chatty libraries (litellm) from stacking the panel. Add a
  CHANGELOG `### Fix` entry under `## Unreleased` referencing #392. Run
  `rg -n "LiteLLM|verbose_logger|stderr" docs/CLI.md` to confirm no stale
  description of the old root-only behaviour survives.

## Discovered issues

- **Fix in this PR:** none. (The stacking channel is the whole scope; the fix
  is the generalised sweep in Stage 2.)
- **Filed as issues:**
  - **#396** — litellm double-emits every log record (private handler +
    propagation): duplicate lines under `--display={plain,log}` and
    litellm-format lines ignoring `--log-format`, in all modes. The Stage 2
    fix only neutralises the handlers while the Live panel is active; #396 is
    the process-wide neutralisation at CLI startup.

## Risks / open questions

- **fd-level leaks are out of scope.** The confirmed channel is Python-level
  (litellm's `StreamHandler`), and the coding example provisions no per-trial
  Docker, so ticket candidate #2 (subprocess inheriting raw fd 2) cannot fire
  on this hot path. `_StderrProbe` is object-level (`.write` tap) and would not
  catch a raw-fd subprocess write. If a future env-service run re-introduces
  stacking, an fd-level (`os.dup2`) probe/fix is a separate ticket — do not
  gold-plate this PR with it.
- **Sweep blast radius.** The sweep removes handlers from third-party loggers
  for the Live lifetime. Scoped by stream identity (only handlers pointing at
  the captured terminal streams) and fully reversed on `__exit__`, so the blast
  radius is one run's Live window. The `propagate=False` defensive branch
  ensures a swept non-propagating logger's records are not silently dropped.
- **Thread-safety of the sweep.** Loggers may be created lazily by worker
  threads *after* `__enter__` runs the sweep (e.g. a per-provider logger on
  first call). Such a late-created logger with a bypassing handler would escape
  the one-shot sweep. Confirmed non-issue for litellm (its `LiteLLM*` loggers
  and their handlers are created at import, before `__enter__`). Note for the
  implementer: keep the sweep in `__enter__` (do not attempt a live-updating
  sweep); if the live run shows any residual bypass, that is the signal a
  late-created logger exists and warrants a follow-up, not a redesign.
