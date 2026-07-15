# Plan: B1 — Rich Live progress panel during `tolokaforge run`

Issue: Toloka/tolokaforge#285 (milestone: Terminal DX, umbrella #297)
Branch: `feat/issue-285-b1-rich-live-progress` (already created; branches off `feat/terminal-dx`; PR targets `feat/terminal-dx`)

## Context

Milestone Terminal DX has landed A1/A3/A4/B2:

- **A1 (#276)** — `tolokaforge/cli/_display.py`: shared `console` (stderr, `soft_wrap=True`, `THEME`), `make_progress`, `make_live`. Grep-guard `tests/canonical/test_cli_display_invariants.py::test_no_ad_hoc_console_in_cli` forbids any new `rich.Console(...)` outside `_display.py`.
- **A3 (#279)** — `configure_root_logging` + `-v/-q/--log-format={pretty,plain,json}`. Every stdlib and `StructuredLogger` record renders on stderr with shape `HH:MM:SS.mmm | LEVEL | k=v | message` through a sentinel-tagged `StreamHandler(sys.stderr)`.
- **A4 (#280)** — `emit_artifact_path(path)` on stdout as the single sanctioned stdout write; `Orchestrator.run()` returns a `Path`; grep-guard `test_no_bare_stdout_write_in_cli` forbids other stdout writes.
- **B2 (#282)** — `--display={full,rich,plain,log,none}` root flag + `TOLOKAFORGE_DISPLAY=…` env var; resolved mode stashed on `ctx.obj["display_mode"]` as a `DisplayMode` enum. `--display=full` falls back to `--display=rich` today because `textual` is not a dependency (C3 territory). `--display=none` silences both `console` and the log handler.

B1 is the **first issue in the milestone that adds runtime rendering** — everything before was plumbing. B1 delivers the Rich `Live` panel that `--display=rich` renders during `tolokaforge run`, plus a small event API the runner uses to notify the display of per-trial lifecycle transitions.

**Grep confirms the surface is net-new**: `rg -n "run_display|LiveRunDisplay|RunDisplayEvents|trial_started|trial_progress|trial_completed|trial_failed|judgment_scored" tolokaforge/ tests/` returns zero hits. `rg -n "from rich.layout|from rich.panel" tolokaforge/` returns zero hits — no existing `rich.layout.Layout` usage to conflict with.

**Reproduced current behaviour**: `uv run tolokaforge --help 2>&1 | grep -F -- "--display"` shows the flag surface. `uv run tolokaforge run --config examples/native/tool_use/run_config.yaml` (a real run, gated on an OpenRouter key in `.env`) today emits `Loading configuration…`, `Runtime backend:`, `Found N tasks`, then a stream of `Trial started` / `Trial completed` structured log lines on stderr with the shape A3 established. There is no Live region, no task list, no cost tracker — every progress signal is a log line.

**Current per-trial event surface (evidence)**:

- `Orchestrator.run()` (`tolokaforge/core/orchestrator.py:962-1473`) — schedules trials via `ThreadPoolExecutor` and reaps futures in a `wait(...)` loop. Emits stdlib log lines at each transition:
  - `logger.info("Trial completed", task_id=..., trial_index=..., trial_cost_usd=..., total_cost_usd=...)` after a non-retryable success (line 1390).
  - `logger.info("Trial failed (transient)", ...)` after a retryable failure (line 1370).
  - `logger.error("Trial execution exception", ..., will_retry=...)` after a hard exception (line 1401).
  - `logger.warning("Retrying trial after transient failure", ...)` after retry (line 1354).
  - `logger.warning("Budget limit reached; no new trials will be scheduled", ...)` on budget cap (line 1421).
- `InProcessConductor.run()` (`tolokaforge/core/conductor.py:304-329`) — five phases: `_setup_trial`, `_run_agent_loop`, `_capture_final_state`, `_grade`, `_write_artifacts`. `_grade` emits `logger.info("Trial graded", task_id=..., score=..., binary_pass=...)` (line 673).
- `TrialRunner.run()` (`tolokaforge/core/runner.py:157-…`) — drives the agent ↔ user-simulator loop via `ToolCallingLoop` (`tolokaforge/core/loop.py`). Uses a `_AgentMetricsSink` (`tolokaforge/core/runner.py:425-447`) whose `record_generation(result)` is called after every LLM call — this is the natural per-turn hook for `trial_progress`. Deltas: `result.usage.prompt_tokens`, `result.usage.completion_tokens`, `result.cost_usd`.
- `ProvisioningTrialExecutor.execute()` (`tolokaforge/core/trial_executor.py`) — brackets `conductor.run` with substrate provision/teardown; already emits `logger.info("Trial env provisioned", ...)` / `logger.error("Provisioning failed", ...)`.

**Metrics shape** (from `Metrics.usage`, `tolokaforge/core/llm/usage.py`): `prompt_tokens: int`, `completion_tokens: int`, `cost_usd: float | None` — cumulative per-trial after each `record_generation`. The orchestrator maintains `total_cost_usd` at the run level (`orchestrator.py:1252-1336`) by summing `trajectory.metrics.cost_usd` as futures complete.

**No native ETA today** — the queue has `queue.estimate_eta_seconds()` (used by `tolokaforge status`) but the running `Orchestrator.run()` never consults it. B1 owns a light ETA estimator inside `LiveRunDisplay` (linear extrapolation from elapsed wall-time × remaining/completed) — no queue coupling needed.

## Goal

Under `--display=rich` (and `--display=full` today, which B2 collapses to `rich`) on any stream posture, `tolokaforge run` renders a `rich.Live` panel that stays in place for the duration of the run and shows:

- **Left pane**: scrolling list of trials, one line per trial: `<glyph> task_id · trial_idx` where glyph ∈ {`⏳` running, `✓` completed, `✗` failed}. Newest activity floats near the top; completed trials scroll off after N entries (bounded window; propose 20 in Stage 1 spec).
- **Right pane**: structured summary of the focused trial derived from cumulative event totals — `turn N · in Xk / out Ytok · $Z.ZZ · last: <event_kind>`. NO free-form log lines: the seven `RunDisplayEvents` methods carry only ints/floats/enums, so the pane's content is a small fixed-shape header, not a scrolling tail. Focus follows lifecycle transitions only (D7) — the pane is stable during long-running per-turn progress ticks.
- **Bottom bar**: `142/500 · 12 running · $0.87 · in 41.2k / out 6.8k tok · fail 3 · eta 03:14`. One line, right-aligned tokens/cost with `THEME.cost` markup. `eta` is `HH:MM:SS` or `MM:SS` depending on magnitude; `n/a` before any trial completes.

Under `--display={plain,log,none}` and non-TTY streams, `LiveRunDisplay` is a **no-op context manager** — the existing log-line stream is what the user sees. This is the "auto-degrade" contract from the issue AC.

The runner-side event surface is a new `RunDisplayEvents` Protocol threaded from `Orchestrator` down through `Conductor` and `TrialRunner`. A `_NullRunDisplayEvents` singleton is the default; `LiveRunDisplay` is the sole production implementation shipped in B1.

`console.print(...)` lines emitted during the run (e.g. the banner `Runtime backend:`, `Output base:`, or a mid-run warning) scroll above the Live region without corrupting it — Rich's native behaviour when both share the same `Console`.

Log lines from the root handler installed by `configure_root_logging` (A3) — every `logger.info("Trial completed", ...)` line — also coexist cleanly: `LiveRunDisplay.__enter__` re-points the sentinel-tagged root handler's `.stream` attribute to the currently-active `sys.stderr` (which Rich's `Live` has already patched to a `_FileProxy` that respects the Live region). `__exit__` restores the original stream.

## Non-goals

- **Do NOT preempt C3 (Textual TUI, milestone 11)**. `LiveRunDisplay` is Rich-only. `DisplayMode.FULL` → still falls back to `DisplayMode.RICH` under B1 (B2 already collapses); this plan does NOT add textual as a dependency and does NOT touch any TUI-mode code. C3 will ship a separate Textual class that consumes the same `RunDisplayEvents` Protocol.
- **Do NOT preempt B3 (budgets/fallback models, #283)**. The bottom-bar `$0.87` renders the ACCUMULATING run cost the orchestrator already tracks — no budget-cap enforcement, no fallback-model routing. When B3 lands, it will add its own event(s) (e.g. `budget_warning(remaining_pct)`) that this panel can consume in a follow-up.
- **Do NOT preempt B4 (dry-run, #284) or B5 (resume, #286)**. B1 is about live-run rendering. Resume interactions (initial `completed_trials` from `RunState`) are surfaced via the `run_started(total_trials, initial_completed)` event; the panel just renders the counter — it does not add a resume UI.
- **Do NOT reroute the log stream elsewhere.** A3's `configure_root_logging` remains the source of truth for line shape; B1 only re-points the handler's `.stream` while Live is active (and restores on exit). No new log formatter, no `RichHandler` swap.
- **Do NOT change `--display=none` semantics.** B2 short-circuits `console` + log handler. `LiveRunDisplay` never activates under `NONE` (via `for_mode` gate).
- **Do NOT emit the events over gRPC.** The event Protocol is process-local; the orchestrator, conductor, and runner all live in the same process. When the Cloud Runtime lands (docs/CLOUD_RUNTIME_ARCHITECTURE.md), a follow-up will wire the events through a gRPC-side stream — out of scope here.
- **Do NOT add per-tool-call events (`tool_call_started`/`tool_call_completed`).** The right pane renders a structured summary derived from the existing seven events' cumulative totals; per-tool granularity is out of scope for MVP. Filed as a follow-up if operator demand surfaces.
- **Do NOT add a `trial_note(*, trial_id, text)` free-form-text event.** The alternative (b) considered in critic round 1 — thread a fifth emission site through `orchestrator.py:1390`, `conductor.py:673`, and the runner's tool-execution path — was rejected in favour of narrowing the right-pane contract (see D7a). If future operator demand pushes back, the follow-up adds `trial_note` as a purely additive Protocol method.
- **Do NOT add configurable refresh rate.** `refresh_per_second=4.0` (Rich's / `make_live`'s default) is fine for the frame; making it a CLI knob is scope creep. Recorded as an open question if operators report jitter.

## Target module surface

### `tolokaforge/cli/_run_display.py` — new module

```python
"""Rich `Live` progress panel for `tolokaforge run` under `--display=rich`.

Under `DisplayMode.RICH` (and today `DisplayMode.FULL`, which B2 collapses),
:class:`LiveRunDisplay` renders a three-region panel: left-pane trial list,
right-pane structured summary of the focused trial (turn count / tokens /
cost / last-event kind), bottom status bar with cost / tokens / ETA /
failure counts. Under any other mode (`PLAIN` / `LOG` / `NONE`) — and on
non-TTY streams under `RICH` — `for_mode` returns a no-op context manager
and the existing log-line stream is what the user sees.

The panel subscribes to :class:`RunDisplayEvents` — a small Protocol the
orchestrator + conductor + runner emit into. The Protocol has a no-op
default (:class:`_NullRunDisplayEvents`), so callers that never build a
display can still thread `events` through without conditional branches.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from tolokaforge.cli._display import DisplayMode, console, make_live


@runtime_checkable
class RunDisplayEvents(Protocol):
    """Per-trial lifecycle events the runner emits into a display.

    Threaded from :class:`~tolokaforge.core.orchestrator.Orchestrator` down
    through :class:`~tolokaforge.core.conductor.Conductor` and
    :class:`~tolokaforge.core.runner.TrialRunner` via one field on
    :class:`OrchestratorDeps`, one field on
    :class:`~tolokaforge.core.conductor.ConductorContext`, and one kwarg
    on :class:`TrialRunner.__init__`. Every method is fire-and-forget —
    implementations must NOT raise; a raise would corrupt the runner loop.
    """

    def run_started(self, *, total_trials: int, initial_completed: int) -> None:
        """The run has entered its execution phase. Fired once, top of
        ``Orchestrator.run()`` after ``load_tasks`` and the queue is
        primed. ``total_trials`` counts pending+completed; ``initial_completed``
        is the resume-time head-start (0 for a fresh run)."""

    def trial_started(self, *, trial_id: str, task_id: str, trial_index: int) -> None:
        """A trial has been leased by a worker and is about to enter
        provisioning. Fired inside the orchestrator's ``submit_one`` after
        ``run_queue.mark_running`` and ``run_state.mark_running``."""

    def trial_progress(
        self,
        *,
        trial_id: str,
        prompt_tokens_delta: int,
        completion_tokens_delta: int,
        cost_delta_usd: float,
    ) -> None:
        """One LLM generation finished inside the trial's agent loop.
        Fired from :class:`_AgentMetricsSink.record_generation` per call.
        Deltas — the panel accumulates by itself. ``cost_delta_usd`` may
        be zero when the provider did not surface a cost."""

    def trial_completed(self, *, trial_id: str, binary_pass: bool, score: float | None) -> None:
        """Terminal, non-retryable success: the trial ran to completion,
        was graded, and is being marked completed in the run queue. Fired
        in the orchestrator's ``wait`` loop."""

    def trial_failed(self, *, trial_id: str, error: str, retryable: bool) -> None:
        """Terminal failure (retryable + retries exhausted, or non-retryable
        exception). ``retryable=True`` for transient-then-exhausted; ``False``
        for a hard raise. Fired in the orchestrator's ``wait`` loop."""

    def judgment_scored(self, *, trial_id: str, score: float, binary_pass: bool) -> None:
        """The rubric judge finished. Fired from
        :meth:`InProcessConductor._grade` after ``trajectory.grade`` is
        populated. Distinct from ``trial_completed`` — a trial can complete
        without a judge (deterministic grading only), or a judge can score
        an errored trial (``binary_pass=False``, judge_status may be
        ``ERRORED`` — see :class:`~tolokaforge.core.models.JudgeStatus`)."""

    def run_finished(self, *, output_dir: Path) -> None:
        """The run has exited (success or budget-paused). Fired at the top
        of ``Orchestrator.run()`` right before it returns. Panel uses this
        as a signal to flush and freeze the final frame."""


class _NullRunDisplayEvents:
    """No-op :class:`RunDisplayEvents`.

    Every method returns ``None``. Used as the default on
    :class:`OrchestratorDeps` so orchestrator / conductor / runner never
    branch on ``events is None`` — they just call every method.
    """

    def run_started(self, **_: object) -> None: ...
    def trial_started(self, **_: object) -> None: ...
    def trial_progress(self, **_: object) -> None: ...
    def trial_completed(self, **_: object) -> None: ...
    def trial_failed(self, **_: object) -> None: ...
    def judgment_scored(self, **_: object) -> None: ...
    def run_finished(self, **_: object) -> None: ...


_NULL_EVENTS: RunDisplayEvents = _NullRunDisplayEvents()


@dataclass
class _TrialCard:
    """Per-trial state the left pane and right pane read from.

    Internal to :class:`LiveRunDisplay`. Frozen would be nice but the
    fields mutate on every event — dataclass by design.
    """

    trial_id: str
    task_id: str
    trial_index: int
    status: str  # "running" | "completed" | "failed"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    score: float | None = None
    binary_pass: bool | None = None
    error: str | None = None
    # Only lifecycle events (trial_started / trial_completed / trial_failed /
    # judgment_scored) bump last_update_ts — see D7. `trial_progress` does
    # NOT touch it, so focus is stable across per-turn ticks.
    last_update_ts: datetime = field(default_factory=datetime.now)
    # Turn counter — incremented once per `trial_progress` event (== per LLM
    # generation inside the agent loop). Feeds the right pane's `turn N` field.
    turn_count: int = 0
    # Name of the last event that fired for this trial. One of:
    # "started" | "progress" | "completed" | "failed" | "judged".
    # Feeds the right pane's `last: <kind>` field.
    last_event_kind: str = "started"


class LiveRunDisplay:
    """Rich Live panel for `tolokaforge run` under `--display=rich`.

    Usage — always via :meth:`for_mode` at the call site:

        with LiveRunDisplay.for_mode(mode) as display:
            orchestrator = Orchestrator(config, deps=OrchestratorDeps(events=display.events))
            output_dir = orchestrator.run()

    :meth:`for_mode` returns an active :class:`LiveRunDisplay` under
    :attr:`DisplayMode.RICH` / :attr:`DisplayMode.FULL` and a
    :class:`_NoopDisplayCtx` under any other mode; both satisfy the
    ``AbstractContextManager`` Protocol and both expose ``.events`` —
    the caller never branches.
    """

    def __init__(self, *, refresh_per_second: float = 4.0, max_trial_rows: int = 20) -> None:
        # Single re-entrant-safe lock guarding every event handler body.
        # Event handlers mutate shared counters (`self._prompt_tokens += ...`
        # etc.); with 12 concurrent workers each firing `trial_progress` from
        # its own thread, `x += n` compiles to three bytecodes with GIL-release
        # windows — concurrent updates lose data without the lock. See D8.
        self._lock: threading.Lock = threading.Lock()
        self._live: Live | None = None
        self._layout: Layout = self._build_layout()
        self._trials: dict[str, _TrialCard] = {}
        self._focused_trial_id: str | None = None
        self._total_trials: int = 0
        self._completed: int = 0
        self._failed: int = 0
        self._running: int = 0
        self._prompt_tokens: int = 0
        self._completion_tokens: int = 0
        self._total_cost_usd: float = 0.0
        self._run_start_ts: datetime | None = None
        self._max_trial_rows: int = max_trial_rows
        self._refresh_per_second: float = refresh_per_second
        # Set of trial ids the panel has ever seen — bounded via the
        # `max_trial_rows` window; completed trials scroll off in
        # last-update order.
        self._saved_log_stream = None  # populated in __enter__

    @classmethod
    def for_mode(cls, mode: DisplayMode) -> AbstractContextManager["LiveRunDisplay | _NoopDisplayCtx"]:
        """Return a context manager appropriate for `mode`.

        `RICH` / `FULL` → a fresh :class:`LiveRunDisplay` (both go through
        the Rich panel — B2 already collapses `FULL` to `RICH` at the
        callback boundary, but the check here is defensive).
        Any other mode → :class:`_NoopDisplayCtx`, whose `events`
        attribute is :data:`_NULL_EVENTS`.

        The caller passes `mode = ctx.obj["display_mode"]` — a resolved
        `DisplayMode` enum — so this method never has to re-parse the
        flag or env var.
        """

    @property
    def events(self) -> RunDisplayEvents:
        """The event sink the caller threads into the orchestrator."""
        return self  # LiveRunDisplay itself implements RunDisplayEvents

    def __enter__(self) -> "LiveRunDisplay":
        """Enter the Live region.

        Side effects — restored on ``__exit__``:

        1. `make_live(self._layout, refresh_per_second=...)` acquires the
           shared `console` and starts the auto-refresh thread.
        2. Walk `logging.getLogger().handlers`, find the sentinel-tagged
           tolokaforge handler (via `_TOLOKAFORGE_ROOT_HANDLER_SENTINEL`
           on the handler), snapshot `.stream`, and re-point it to
           `sys.stderr` (which Rich has already wrapped via `_FileProxy`).
           On exit, restore the snapshot.
        """

    def __exit__(self, *exc_info: object) -> None: ...

    # RunDisplayEvents implementation ---------------------------------

    def run_started(self, *, total_trials: int, initial_completed: int) -> None: ...
    def trial_started(self, *, trial_id: str, task_id: str, trial_index: int) -> None: ...
    def trial_progress(
        self,
        *,
        trial_id: str,
        prompt_tokens_delta: int,
        completion_tokens_delta: int,
        cost_delta_usd: float,
    ) -> None: ...
    def trial_completed(self, *, trial_id: str, binary_pass: bool, score: float | None) -> None: ...
    def trial_failed(self, *, trial_id: str, error: str, retryable: bool) -> None: ...
    def judgment_scored(self, *, trial_id: str, score: float, binary_pass: bool) -> None: ...
    def run_finished(self, *, output_dir: Path) -> None: ...

    # Internal rendering ----------------------------------------------

    def _build_layout(self) -> Layout:
        """Assemble the three-region `rich.layout.Layout`: left/right split
        via `Layout.split_row`, bottom bar via `Layout.split_column` on the
        parent. Panels are `Panel(Text(...), title=...)`. Called once at
        construction; per-event updates mutate the layout children's
        renderables in place."""

    def _render_left_pane(self) -> Panel: ...
    def _render_right_pane(self) -> Panel: ...
    def _render_bottom_bar(self) -> Text: ...
    def _estimate_eta_seconds(self) -> float | None:
        """Linear extrapolation: elapsed / completed × remaining. Returns
        `None` before any trial completes."""


class _NoopDisplayCtx:
    """Context-manager returned by `LiveRunDisplay.for_mode` under
    `PLAIN` / `LOG` / `NONE`. Enter and exit are pass-through; `.events`
    is `_NULL_EVENTS`."""

    events: RunDisplayEvents = _NULL_EVENTS

    def __enter__(self) -> "_NoopDisplayCtx":
        return self

    def __exit__(self, *_: object) -> None:
        return None


__all__ = [
    "LiveRunDisplay",
    "RunDisplayEvents",
]
```

### `tolokaforge/core/orchestrator.py` — extension

- `OrchestratorDeps` gains one field:

  ```python
  events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)
  ```

  Default preserves current behaviour: no events emitted anywhere.

- `Orchestrator.__init__` stashes it on `self._events`.
- Event emission sites in `Orchestrator.run()`:
  - After the pending-trials build (post-`_build_pending_trials`, post-queue seed): `self._events.run_started(total_trials=len(pending_trials)+prior_completed, initial_completed=prior_completed)`.
  - Inside `submit_one`, after `run_state.mark_running` and `self.state_manager.save_state`: `self._events.trial_started(trial_id=f"{task_id}:{trial_idx}", task_id=task_id, trial_index=trial_idx)`.
  - Inside the `wait` loop, on the non-retryable success branch (after `run_queue.mark_completed`): `self._events.trial_completed(trial_id=..., binary_pass=..., score=...)`.
  - On the retryable-then-exhausted branch AND the hard-exception branch (after `run_state.mark_failed`): `self._events.trial_failed(trial_id=..., error=..., retryable=<True|False>)`. Explicit retryable=True/False so the panel can distinguish "gave up after retries" from "immediate crash" if it wants to.
  - Right before `return output_dir.resolve()`: `self._events.run_finished(output_dir=output_dir)`.
- `run_worker()` is **NOT** wired in this stage — nothing consumes the events in the distributed-worker path today (only the `run` command builds a `LiveRunDisplay`; `worker` shells out to its own click command with no panel). Filed as follow-up (see "Discovered issues"). When a display consumer for `worker` lands, wiring the same emission sites is a purely additive change.
- `_build_conductor` passes `events=self._events` into `ConductorContext`.

### `tolokaforge/core/conductor.py` — extension

- `ConductorContext` gains one field:

  ```python
  events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)
  ```

  Same default as `OrchestratorDeps`. `InProcessConductor.__init__` reads it via `**vars(ctx)` (unchanged unpack; a new kwarg is a widening of the ctor signature).

- `InProcessConductor._grade` emits `self.events.judgment_scored(trial_id=setup.trial_id, score=trajectory.grade.score, binary_pass=trajectory.grade.binary_pass)` right after `trajectory.grade = self.trial_grader.grade(...)`.
- `InProcessConductor._run_agent_loop` threads `events=self.events` and `trial_id=setup.trial_id` into `TrialRunner(...)`.

### `tolokaforge/core/runner.py` — extension

- `TrialRunner.__init__` gains two kwargs:

  ```python
  events: RunDisplayEvents = _NULL_EVENTS,
  ```

  The `trial_id` is already derivable from `f"{self.task_id}:{self.trial_index}"` — no new field needed.

- `_AgentMetricsSink.__init__` gains `events` and `trial_id`. Its `record_generation(result)` emits:

  ```python
  self._events.trial_progress(
      trial_id=self._trial_id,
      prompt_tokens_delta=result.usage.prompt_tokens,
      completion_tokens_delta=result.usage.completion_tokens,
      cost_delta_usd=result.cost_usd or 0.0,
  )
  ```

  Emission happens AFTER the internal metrics accumulation so a raise inside the panel would not corrupt trial metrics — but per Protocol contract, implementations must NOT raise, so the ordering is defensive rather than load-bearing.

### `tolokaforge/cli/main.py` — extension

`run()` command body wraps `orchestrator.run()`:

```python
from tolokaforge.cli._run_display import LiveRunDisplay
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps

# … existing config load / user model override / runtime banner …

display_mode = ctx.find_root().obj.get("display_mode", DisplayMode.PLAIN)

with LiveRunDisplay.for_mode(display_mode) as display:
    orchestrator = Orchestrator(
        run_config,
        resume=resume,
        verbose=verbose,
        strict=strict,
        project=project,
        deps=OrchestratorDeps(events=display.events),
    )
    orchestrator.load_tasks()
    if not orchestrator.tasks:
        console.print("[red]No tasks found![/red]")
        raise SystemExit(1)
    console.print(f"[green]Found {len(orchestrator.tasks)} tasks[/green]")
    output_dir = orchestrator.run()

console.print("[bold green]✓ Run complete![/bold green]")
console.print(f"Results saved to: {output_dir}")
emit_artifact_path(output_dir)
```

Rationale for placement:

- **`with` enters BEFORE `load_tasks`** so the panel is up in time for the panel's "Loading tasks…" console.print — the user gets visual feedback fast on slow task loads. Under `RICH`, `load_tasks` errors surface through the Live region's cooperative rendering; under `PLAIN`, nothing changes.
- **`with` exits BEFORE `emit_artifact_path`** so the final artifact path is a clean stdout line uncontaminated by any Live redraw artefacts. `console.print("Run complete!")` fires post-exit, hitting stderr normally.
- **`OrchestratorDeps(events=display.events)`** — the `.events` property returns `LiveRunDisplay` itself under RICH and `_NULL_EVENTS` under other modes. The orchestrator does not branch.

### `docs/CLI.md § Display modes` — table row rewrite

Stage 3 updates one table row:

| Value  | Behaviour                                                                                                                                              |
|--------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| `rich` | Rich Live panel: left-pane trial list, right-pane structured summary of the focused trial (turn count / tokens / cost / last-event kind), bottom bar with cost / tokens / ETA / failure counts. |

And the paragraph immediately after ("`--display=rich` renders through the existing per-command `console.print(...)` calls…") is rewritten to describe the new panel — the plan text is Stage 3's deliverable, not this doc.

## Design decisions

### D1. Event Protocol shape — `typing.Protocol`, kwarg-only, no-op default

**Options considered**:

- (a) `abc.ABC` with abstract methods → forces every consumer to subclass; verbose.
- (b) `typing.Protocol` (runtime-checkable) with a `_NullRunDisplayEvents` fallback → duck-typed, zero-boilerplate for consumers, keyword-only calls prevent positional-arg drift when the Protocol grows.
- (c) Callback dict `{"trial_started": fn, ...}` → cheap but loses type safety and IDE completion.

**Decision: (b) `Protocol`.** Matches the AGENTS.md type table's row for "Polymorphism / behaviour contract": `typing.Protocol` (preferred — duck-typed) or `abc.ABC`. Mirrors existing engine seams — `Conductor`, `TrialExecutor`, `RuntimeBackend`, `MetricsSink`, `BaseAdapter` are all Protocols / ABCs.

`_NullRunDisplayEvents` is the AGENTS.md "no defensive branching" enforcer — no `if events is not None:` at any emission site; every one is `self._events.method(...)`. AGENTS.md Core Rule 1 ("Surface failures explicitly, do not add fallbacks that hide errors") is preserved because the null implementation is DELIBERATE and DOCUMENTED, not a silent catch.

Every method is **kwarg-only** (`def trial_progress(self, *, trial_id, ...)`) so a future field addition is safe — a positional call breaks loudly.

Every method's contract is **fire-and-forget** — implementations MUST NOT raise. The panel's own `try/except` around Rich renders (Stage 1) prevents raw exceptions leaking into the runner; the Protocol doc string enforces the invariant on future consumers.

### D2. Events emitted from orchestrator + conductor + runner (three layers)

**Options considered**:

- (a) Emit ALL events from the orchestrator by parsing structured log records → no threading, but couples the panel to A3's log shape and would miss per-turn cost deltas (no log line per generation today).
- (b) Emit ALL events from the runner (`TrialRunner`) → deepest layer, but the orchestrator owns trial dispatch / retry classification, so `trial_started` and `trial_failed` (retry-exhausted) are naturally orchestrator-side signals.
- (c) Emit from where the signal naturally lives — `run_started` / `trial_started` / `trial_completed` / `trial_failed` from orchestrator; `judgment_scored` from conductor; `trial_progress` from `_AgentMetricsSink` inside the runner.

**Decision: (c).** Each event is emitted from the layer that already owns the transition. No layer emits an event it lacks primary knowledge of. Threading cost: one field on `OrchestratorDeps`, one on `ConductorContext`, one kwarg on `TrialRunner`, one on `_AgentMetricsSink`. Four sites — trivial.

Not option (a) because per-turn cost deltas are the most operator-valuable signal on the bottom bar (a slow trial that isn't emitting completion logs still shows LIVE tokens/cost through `trial_progress`).

### D3. `LiveRunDisplay` IS the `RunDisplayEvents` implementation (no adapter class)

**Options considered**:

- (a) `LiveRunDisplay` exposes `.events` as a separate observer object.
- (b) `LiveRunDisplay` implements `RunDisplayEvents` directly and returns `self` via `.events`.

**Decision: (b).** One class, one lifecycle. The Protocol's runtime-check semantics (`@runtime_checkable`) mean `isinstance(display, RunDisplayEvents)` works. The `.events` property is a `LiveRunDisplay` → `RunDisplayEvents` (via structural typing) — zero cost, no wrapper allocation.

The property (rather than exposing `self` directly) reads better at the call site: `OrchestratorDeps(events=display.events)` communicates intent — "we're wiring the event sink" — clearer than `OrchestratorDeps(events=display)`.

### D4. `for_mode` classmethod as the SOLE activation gate

**Options considered**:

- (a) `LiveRunDisplay(...)` constructor is public; caller checks the mode manually.
- (b) `LiveRunDisplay.for_mode(mode)` is the only entry point; constructor stays public only for tests.

**Decision: (b).** Every caller passes through `for_mode`. Under `RICH`/`FULL` it returns a `LiveRunDisplay`; under `PLAIN`/`LOG`/`NONE` it returns a `_NoopDisplayCtx`. The caller never branches. This lifts "activation gate" into one place — a canonical test can grep for the pattern and forbid ad-hoc `LiveRunDisplay(...)` instantiations outside the classmethod's body.

Downside: constructing a `LiveRunDisplay` in a test requires calling `for_mode(DisplayMode.RICH)` (which today just constructs one) or importing the class directly. Accepted — the constructor is `public` (not name-mangled), it's just not the recommended entry point.

**Non-TTY under `RICH`** — `for_mode(DisplayMode.RICH)` on a non-TTY stream still returns an active `LiveRunDisplay`. Rationale: B2's `select_display_mode` auto-selects `PLAIN` on non-TTY. If an operator forced `RICH` via `--display=rich` or `TOLOKAFORGE_DISPLAY=rich` on a piped shell, that's an explicit override — honour it. The Live region degrades gracefully on non-TTY (Rich buffers and emits final frames only), and the log stream still runs above it. If the panel is objectively unreadable in a specific piped context, that's an operator misconfig — fail-loud posture matches AGENTS.md Core Rule 1.

### D5. Log-line coexistence — re-point the sentinel handler's stream on `__enter__`

**The problem**: A3's `configure_root_logging` installs a `logging.StreamHandler(sys.stderr)`. The handler captures `sys.stderr` at construction. When Rich's `Live` enters and patches `sys.stderr` in the process globals (via `redirect_stderr=True` on `make_live`, which is the default), the handler continues writing to the ORIGINAL `sys.stderr` — bypassing Rich's Live-aware `_FileProxy` and dropping log text ON TOP of the Live region.

**Options considered**:

- (a) Ignore it — accept that log lines land under/inside the Live block. Rejected: the AC says `console.print()` lines scroll above the region cleanly. Users will read a corrupted display as a bug.
- (b) Swap the root handler for a `rich.logging.RichHandler` on entry, restore on exit. Rejected: RichHandler formats records via Rich's own rules — the A3-established `HH:MM:SS.mmm | LEVEL | k=v | message` shape breaks. Two log shapes ("outside" vs "under panel") violates the "docs describe current state only" principle.
- (c) Walk `logging.getLogger().handlers`, find the sentinel-tagged handler (using `_TOLOKAFORGE_ROOT_HANDLER_SENTINEL` from `tolokaforge/core/logging.py`), save `handler.stream`, set `handler.stream = sys.stderr` (which is now Rich-wrapped). Restore on exit.

**Decision: (c).** Small, targeted, preserves A3's formatter contract, and restores cleanly. Sentinel-tag lookup means any future handler installed by embedders is left untouched — B2's "handler-local silencing" principle carries.

Code sketch:

```python
def __enter__(self) -> LiveRunDisplay:
    self._live = make_live(self._layout, refresh_per_second=self._refresh_per_second)
    self._live.__enter__()
    # Re-point the sentinel handler's stream to the now-wrapped sys.stderr.
    for handler in logging.getLogger().handlers:
        if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
            self._saved_log_stream = handler.stream
            handler.stream = sys.stderr
            break
    return self

def __exit__(self, *exc_info: object) -> None:
    if self._saved_log_stream is not None:
        for handler in logging.getLogger().handlers:
            if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
                handler.stream = self._saved_log_stream
                break
    if self._live is not None:
        self._live.__exit__(*exc_info)
```

The **restore** step must not raise — if the handler was removed between enter and exit (unlikely but possible under embedder mutation), the loop simply completes without a match and moves on. No warning; the exit path exists to leave the process in the state it entered.

### D6. Bottom bar formatting — locked format string

Locking the bottom-bar exact literal is a Stage 3 canonical assertion (SVG snapshot).

Format: `{completed}/{total} · {running} running · ${cost:.2f} · in {prompt}k / out {completion}k tok · fail {failed} · eta {eta}`

Rules:
- `cost` renders to 2 decimals for `< $10` runs, to nearest cent for `>= $10`. Below $0.01, renders `$<0.01`. (Rationale: operators visually scan the cost while a run is live; formatting jitter distracts.)
- `prompt`, `completion` render with `k` suffix ONLY when >= 10_000 tokens (e.g. `41.2k`). Below that, render `1234 tok` verbatim. The unit is added once at the end (`tok`) — the bar shape stays `in X / out Y tok` regardless.
- `eta` renders `MM:SS` for < 1h, `HH:MM:SS` for >= 1h, `n/a` before any completion.

The formatter is a pure `_format_bottom_bar(cards, stats) -> str` function; unit tests lock every branch (`cost=0`, `cost=0.005`, `prompt=1234`, `prompt=41200`, `eta=None`, `eta=17`, `eta=194`, `eta=11400`).

### D7. Focus follow — lifecycle transitions only, `trial_progress` is silent for focus

**Options considered**:

- (a) Focus follows the trial that just had `trial_progress` fire — the "active" trial.
- (b) Focus follows the trial that most recently transitioned state (`trial_started` / `trial_completed` / `trial_failed` / `judgment_scored`).
- (c) Every event updates `last_update_ts`; focus follows highest `last_update_ts`.
- (A) Minimum-focus-lock duration: focus stays for `min_focus_duration_ms=500` unless the current trial terminates.

**Decision: (b) — lifecycle transitions only.** `trial_started` / `trial_completed` / `trial_failed` / `judgment_scored` bump the trial's `last_update_ts`. **`trial_progress` does NOT** — it mutates counters and the `turn_count` + `last_event_kind` fields on the trial's card, but leaves `last_update_ts` untouched. The focused trial is `max(cards, key=lambda c: c.last_update_ts)`.

Rationale: under 12 concurrent workers each firing `trial_progress` ~1 Hz (~12 progress events/sec run-wide), option (c) would rotate focus ~12x/sec while the panel repaints at 4 Hz — every frame draws a different trial (chaotic flicker). Option (a) has the same defect. Lifecycle transitions fire at most a few times per second across the whole run, and the operator wants to see them — the panel visibly moves to the newest transition, then holds still through the following per-turn ticks. Rejected (A) as scope creep — a min-lock timer adds a wall-clock knob and edge cases; narrowing the focus signal is a cleaner primitive.

Trade-off: a trial that's mid-generation (fast-emitting `trial_progress`) will NOT preempt focus off a trial that just completed a moment ago. Correct — the operator's eyes want to land on lifecycle transitions, not micro-events. Locked in Stage 1's unit test `test_focus_does_not_alternate_under_interleaved_progress` (see below).

**Corollary — focus after termination.** When a trial terminates while others are still running, `trial_completed` / `trial_failed` bumps the just-terminated trial's `last_update_ts` — so the focus remains on that trial until another lifecycle event fires on another trial. The operator sees the completion, then the next `trial_started` or `trial_completed` on another trial shifts focus. There is NO auto-advance to "the newest RUNNING trial" — that would take focus AWAY from the transition the operator just saw. Locked in Stage 1's `test_focus_stays_on_just_completed_trial_while_others_still_running` unit test.

### D8. Concurrent event mutation — single `threading.Lock` over every handler body

**The problem**: `Orchestrator.run()` schedules trials via `ThreadPoolExecutor` (typically 12 workers). `_AgentMetricsSink.record_generation(...)` fires `trial_progress` from the worker thread. `LiveRunDisplay.trial_progress`'s body mutates run-level shared state: `self._prompt_tokens += ...`, `self._total_cost_usd += ...`, `self._trials[trial_id].prompt_tokens += ...`. In CPython, `x += n` compiles to LOAD_FAST / BINARY_ADD / STORE_FAST — three bytecodes with GIL-release windows between them. Concurrent updates from N threads lose data — the run-level counters drift *below* actual, silently. This is atomicity, not "GIL contention" — a correctness bug.

**Options considered**:

- (a) `atomic_int`-style `Counter` with `+=` guarded by GIL-only. Rejected — CPython gives no such guarantee for `+=` on ints, and we mutate multiple fields per handler (bar-level + card-level).
- (b) Per-field locks. Rejected — each event handler mutates several fields; five locks per handler is more contention *and* more surface for a lock-order deadlock.
- (c) A single `threading.Lock` acquired at the top of every event handler body. Handlers are short (dict lookup + a few arithmetic ops + a `datetime.now()`); acquisition cost is a few hundred ns per event, and total events/sec is bounded (12 workers × ~4 turns/sec = ~48 events/sec run-wide). Correctness: guaranteed exactly-once per event.

**Decision: (c).** Every method on `LiveRunDisplay` that reads *or* mutates shared state acquires `self._lock` at the top of its body:

```python
def trial_progress(
    self,
    *,
    trial_id: str,
    prompt_tokens_delta: int,
    completion_tokens_delta: int,
    cost_delta_usd: float,
) -> None:
    with self._lock:
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        card.prompt_tokens += prompt_tokens_delta
        card.completion_tokens += completion_tokens_delta
        card.cost_usd += cost_delta_usd
        card.turn_count += 1
        card.last_event_kind = "progress"
        # NOTE: last_update_ts intentionally NOT bumped — see D7.
        self._prompt_tokens += prompt_tokens_delta
        self._completion_tokens += completion_tokens_delta
        self._total_cost_usd += cost_delta_usd
```

Every other handler (`run_started`, `trial_started`, `trial_completed`, `trial_failed`, `judgment_scored`, `run_finished`) applies the same pattern. Render helpers (`_render_left_pane` / `_render_right_pane` / `_render_bottom_bar`) also acquire the lock — Rich's auto-refresh thread invokes them off-schedule.

Locked in Stage 1's unit test `test_trial_progress_is_atomic_under_concurrent_workers` — 12 threads × 1000 events of `cost_delta_usd=0.001` and `prompt_tokens_delta=1` — asserts `display._total_cost_usd == pytest.approx(12.0)` exactly and `display._prompt_tokens == 12000` exactly (not "close to" — exact-equal).

### D9. ETA — linear extrapolation, no Kalman filter

`elapsed_run_seconds / max(completed, 1) * max(0, total - completed)`, with `elapsed_run_seconds = (now - run_start_ts).total_seconds()`, `run_start_ts` set inside `run_started`. Returns `None` before any completion (guarded by `completed == 0`).

Not: EWMA, Kalman, per-task medians. Users understand linear; a "smart" ETA that drifts unpredictably is worse than a naive one that under-estimates.

### D10. Trial-row window — bounded ring, drop oldest completed first

Left pane shows at most `max_trial_rows` (default 20) rows. When exceeded:
- Every running trial is always shown (they matter live).
- Completed/failed trials scroll off in reverse `last_update_ts` order — the OLDEST completed row is dropped first.

Rationale: on a 500-trial run with 12 workers, the panel is useless if it lists 500 rows. Twenty rows fits an 80×24 terminal comfortably with room for the right pane and the bottom bar. Configurable via constructor kwarg for tests / future tuning; no CLI knob today.

### D11. Rich SVG snapshot goldens — width 80 and 120, deterministic

The panel is deterministic by construction (no random ordering, no system-time in the layout body — only in the bottom-bar `eta` computation, which the test patches). Golden generation:

```python
recorder = Console(record=True, width=80, file=io.StringIO(), force_terminal=True)
display = LiveRunDisplay(refresh_per_second=1000)  # instant frames
# monkeypatch datetime.now -> fixed value, so `last_update_ts` and `eta` are stable
with patch("tolokaforge.cli._run_display.datetime") as fake_dt:
    fake_dt.now.return_value = datetime(2026, 7, 15, 12, 0, 0)
    # replace display._live.console with `recorder` for capture, OR
    # call `recorder.print(display._layout)` after replaying events synthetically
    display.run_started(total_trials=50, initial_completed=0)
    display.trial_started(trial_id="task_a:0", task_id="task_a", trial_index=0)
    display.trial_progress(trial_id="task_a:0", prompt_tokens_delta=1200, completion_tokens_delta=340, cost_delta_usd=0.008)
    display.trial_completed(trial_id="task_a:0", binary_pass=True, score=0.85)
    display.trial_failed(trial_id="task_b:0", error="LLMApiTimeoutError", retryable=False)
    # …
    recorder.print(display._layout)
svg = recorder.export_svg(title="tolokaforge run", theme=DEFAULT_TERMINAL_THEME)
Path("tests/canonical/golden/run_display/panel_80.svg").write_text(svg)
```

Two goldens: `panel_80.svg` (80-col), `panel_120.svg` (120-col). Test replays the exact same event sequence into a fresh `LiveRunDisplay`, exports, compares string-equal to the golden. Regeneration flag: `pytest tests/canonical/test_run_display_goldens.py --update-canon` — matches the existing project convention (see `tests/canonical/test_logging_goldens.py:15,120` for the pytest CLI-flag pattern).

The test uses a **direct `recorder.print(display._layout)`** — it does NOT actually enter Rich's `Live` (which spawns a background auto-refresh thread and would introduce non-determinism). Rendering the layout snapshot is enough for the AC ("Live layout renders correctly on 80- and 120-column terminals").

## Stages

Every stage lands as one commit, has behaviour-locking tests, and updates the docs that describe current state.

### Stage 1: `RunDisplayEvents` Protocol + `LiveRunDisplay` (no runner wiring) + unit tests

- **Contract:**
  - New module `tolokaforge/cli/_run_display.py` exports `LiveRunDisplay`, `RunDisplayEvents`. `_NullRunDisplayEvents` is package-private (leading underscore); reachable via `from tolokaforge.cli._run_display import _NULL_EVENTS` for tests.
  - `RunDisplayEvents` Protocol methods and signatures locked as in the "Target module surface" section — kwarg-only, no return value, no raises.
  - `LiveRunDisplay.for_mode(DisplayMode.RICH)` / `for_mode(DisplayMode.FULL)` returns an active display; every other mode returns a `_NoopDisplayCtx`.
  - `LiveRunDisplay` implements `RunDisplayEvents` directly; `.events` property returns `self`.
  - `LiveRunDisplay` uses `make_live(...)` from `_display.py` — no new `Console(...)`. Grep-guard `test_no_ad_hoc_console_in_cli` continues to pass.
- **Behaviour to lock (tier: `unit`, `tests/unit/test_run_display.py` — new file):**
  - **Protocol shape:**
    - `isinstance(_NullRunDisplayEvents(), RunDisplayEvents) is True` (runtime-checkable).
    - `isinstance(LiveRunDisplay(), RunDisplayEvents) is True`.
    - Every method name matches the seven declared in the Protocol; no extras.
  - **`for_mode` activation gate** (parametrised over five modes):
    - `for_mode(RICH)` → `isinstance(ctx, LiveRunDisplay)`.
    - `for_mode(FULL)` → `isinstance(ctx, LiveRunDisplay)` (B2 already collapses at the callback, but the class re-checks).
    - `for_mode(PLAIN)` / `for_mode(LOG)` / `for_mode(NONE)` → `type(ctx).__name__ == "_NoopDisplayCtx"`; `ctx.events is _NULL_EVENTS`.
  - **`_NoopDisplayCtx` behaviour:**
    - `with LiveRunDisplay.for_mode(DisplayMode.PLAIN) as d:` — enters, exits, no side effects; `d.events.trial_started(...)` returns `None`; no exception.
  - **State machine — synthetic event replay:**
    - `display.run_started(total_trials=50, initial_completed=0)` — `display._total_trials == 50`.
    - `display.trial_started(trial_id="a:0", task_id="a", trial_index=0)` — `display._trials["a:0"].status == "running"`; `display._trials["a:0"].last_event_kind == "started"`; `display._running == 1`; `display._focused_trial_id == "a:0"`.
    - `display.trial_progress(trial_id="a:0", prompt_tokens_delta=1200, completion_tokens_delta=340, cost_delta_usd=0.008)` — accumulates: `card.prompt_tokens == 1200`, `card.completion_tokens == 340`, `card.cost_usd == 0.008`, `card.turn_count == 1`, `card.last_event_kind == "progress"`; run-level: `display._prompt_tokens == 1200`, `display._total_cost_usd == 0.008`; **`card.last_update_ts` is UNCHANGED from its value after `trial_started`** (D7 — `trial_progress` does not bump `last_update_ts`).
    - `display.trial_completed(trial_id="a:0", binary_pass=True, score=0.85)` — `card.status == "completed"`, `card.score == 0.85`, `card.last_event_kind == "completed"`; `display._running == 0`, `display._completed == 1`; `card.last_update_ts` bumps.
    - `display.trial_failed(trial_id="b:0", error="LLMApiTimeoutError", retryable=False)` — `card.status == "failed"`, `card.last_event_kind == "failed"`; `display._failed == 1`; `card.last_update_ts` bumps.
    - `display.judgment_scored(...)` — updates `card.score` and `card.binary_pass`, sets `card.last_event_kind == "judged"`, bumps `card.last_update_ts`, sets `_focused_trial_id` to that trial.
    - `display.run_finished(output_dir=Path("/x"))` — no state mutation beyond a "finished" flag; the caller exits the `with` next.
  - **Focus follow — lifecycle only (D7):**
    - Interleaved lifecycle events (`trial_started` / `trial_completed` / `trial_failed` / `judgment_scored`) on two trials assert `_focused_trial_id` follows the highest `last_update_ts`.
    - **`test_focus_does_not_alternate_under_interleaved_progress`** — fire `trial_started` on `a:0`, then fire 100 interleaved `trial_progress` events across `a:0` and `b:0`. Assert `_focused_trial_id == "a:0"` throughout (progress events do NOT change focus). Then fire `trial_started` on `b:0` — assert `_focused_trial_id == "b:0"`. Locks the D7 decision.
    - **`test_focus_stays_on_just_completed_trial_while_others_still_running`** — fire `trial_started` on `a:0`, `trial_started` on `b:0` (focus moves to `b:0`), then `trial_completed` on `a:0`. Assert `_focused_trial_id == "a:0"` (the just-completed trial). Fire a `trial_progress` on `b:0` — assert `_focused_trial_id == "a:0"` unchanged (progress does not preempt). Fire `trial_completed` on `b:0` — assert `_focused_trial_id == "b:0"`. Locks the D7 corollary — no auto-advance to "newest running".
  - **Concurrent-write atomicity (D8):**
    - **`test_trial_progress_is_atomic_under_concurrent_workers`** — spawn 12 `threading.Thread`s, each of which calls `display.trial_progress(trial_id=f"t{i}:0", prompt_tokens_delta=1, completion_tokens_delta=1, cost_delta_usd=0.001)` in a loop of 1000 iterations. Join all threads. Assert `display._total_cost_usd == pytest.approx(12.0, abs=1e-9)` (must be exact-equal within float rounding — 12000 additions of 0.001 == 12.0 exactly is not guaranteed by IEEE 754, so `abs=1e-9` tolerates the last-bit accumulation error; the atomicity requirement is that NO addition is lost).
    - Assert `display._prompt_tokens == 12000` (int — exact equal).
    - Assert `display._completion_tokens == 12000` (int — exact equal).
    - Same shape for a `trial_started` + `trial_completed` variant: 12 threads each fire `trial_started` on a unique trial id, then `trial_completed` on the same id; assert `display._completed == 12` and `display._running == 0` exact.
    - Rationale: without the D8 lock, `x += n` losses would push these counts below the expected value. The test is deterministic in expected value; scheduler jitter would otherwise cause silent underflow.
  - **Trial-row window bounds:**
    - Emit 25 `trial_completed` events with `max_trial_rows=20`. The 5 oldest by `last_update_ts` scroll off (state is preserved in `_trials` but not rendered — the "rendered set" is a computed property). Recommendation: expose the "visible cards" logic as `_visible_cards()` so the test can assert on the trimmed list without diving into Rich renderables.
  - **Bottom-bar formatting (parametrised over the D6 cases):**
    - `_format_bottom_bar(...)` locked in a table-driven test: `(completed=142, total=500, running=12, cost=0.87, prompt_tokens=41200, completion_tokens=6800, failed=3, eta_seconds=194)` → exact string `"142/500 · 12 running · $0.87 · in 41.2k / out 6.8k tok · fail 3 · eta 03:14"`.
    - Additional rows: `cost=0.0` → `"$0.00"`; `cost=0.003` → `"$<0.01"`; `prompt=1234` → `"1234 tok"` (no `k`); `eta=None` → `"n/a"`; `eta=17` → `"00:17"`; `eta=11400` → `"03:10:00"`.
  - **`__enter__` / `__exit__` on shared console (no real terminal):**
    - Use `console.record = True` (via a fixture that toggles it) — enter, invoke a synthetic replay, exit. Assert no exceptions raised; assert the sentinel-handler stream is saved and restored.
    - Set up a fake sentinel handler on the root logger: install a `logging.StreamHandler(io.StringIO())`, tag it with `_TOLOKAFORGE_ROOT_HANDLER_SENTINEL = True`. Enter the display. Assert `handler.stream is sys.stderr` inside the `with` block. Exit. Assert `handler.stream` is restored to the original `StringIO`.
    - Assert idempotency: entering a fresh display AFTER the first `__exit__` works (no residual state).
  - **`console.print("hi")` above the Live region** (round-1 lock on the AC):
    - Enter the display. `console.print("hello")` inside the `with`. Exit. Read `capsys.readouterr()` (or the shared console's `stderr` capture). Assert the string `"hello"` appears once (not corrupted). Assert `"hello"` does NOT appear inside a substring wrapped in an ANSI screen-clear code (a Live redraw would prepend `\x1b[…` codes to a `\r`-corrupted line).
    - Note: `console.print` interleaving is a Rich native. This test locks that WE didn't break it via our layout setup or handler swap.
  - **Panel does NOT raise on unknown trial_id:**
    - `display.trial_progress(trial_id="ghost:0", ...)` where no `trial_started` fired for `ghost:0`. Assert no exception. The card is created lazily on the first non-`trial_started` event (with `status="running"`, since we can't know it failed silently). AGENTS.md fail-loud says "surface failures explicitly" — but this branch is a defensive path against ordering drift in the orchestrator (which we control), and raising would corrupt the runner loop per the Protocol contract. Logged at `DEBUG` via `logging.getLogger("tolokaforge.cli.run_display")`.
  - **`RunDisplayEvents` methods are kwarg-only:**
    - `display.trial_started("x:0", "x", 0)` (positional) raises `TypeError` — kwarg-only by design.
- **Compatibility:** internal only. New module. `RunDisplayEvents` is a new public export from `tolokaforge.cli._run_display`; `LiveRunDisplay` is a new public export. No changes to `_display.py`, no changes to CLI flags, no changes to `docs/CLI.md`. Grep-guards remain unchanged (no new `Console(...)` instances; no new `print(...)` outside `_display.py`).
- **Deliverable:**
  - `tolokaforge/cli/_run_display.py` — new file, ~250 LOC.
  - `tests/unit/test_run_display.py` — new file, ~350 LOC (parametrised across state and formatting).
- **Validation:**
  - `dev.run_tests(marker="unit", pattern="test_run_display")` green.
  - `dev.run_tests(marker="canonical")` green — the grep-guards still pass (no new `Console`, no new bare stdout writes).
  - `dev.lint_check(paths=["tolokaforge/cli", "tests/unit"])` clean.
  - `dev.format_check` clean.
- **Doc updates:** none this stage. `docs/CLI.md` update lands in Stage 3 together with the SVG snapshot goldens and CHANGELOG.

### Stage 2: Wire `RunDisplayEvents` through orchestrator/conductor/runner + CLI integration + integration tests

- **Contract:**
  - `OrchestratorDeps` gains `events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)`. Import moves from `_run_display.py` to `orchestrator.py` — one import line. Note: `_NullRunDisplayEvents` becomes cross-module reachable, so promote it (or `_NULL_EVENTS`) to a `_run_display.py` package export (still leading-underscore = package-private).
  - `Orchestrator.__init__` reads `self._events = resolved_deps.events`.
  - `Orchestrator.run()` emits (in order):
    - `self._events.run_started(total_trials=..., initial_completed=...)` — after `_build_pending_trials` + queue seed, before the thread-pool starts.
    - `self._events.trial_started(...)` — inside `submit_one`, after `run_state.mark_running` + `state_manager.save_state`.
    - `self._events.trial_completed(...)` or `self._events.trial_failed(...)` — inside the `wait` loop, matching the existing log lines (`Trial completed` / `Trial failed (transient)` retry-exhausted / hard-exception).
    - `self._events.run_finished(output_dir=...)` — right before `return output_dir.resolve()`.
  - **`Orchestrator.run_worker()` is intentionally NOT wired in this stage.** Rationale: nothing in the `worker` command builds a `LiveRunDisplay` (only `run` does), so mirroring emission sites there would be dead-on-arrival code. Filed as follow-up "wire events through distributed-worker path when a display consumer exists" (see "Discovered issues"). When a display consumer for `worker` lands (or when Cloud Runtime's trial-plane worker gets its own panel), the same emission pattern is a purely additive change on top of this stage.
  - `ConductorContext` gains `events: RunDisplayEvents = field(default_factory=_NullRunDisplayEvents)`. `_build_conductor` passes `events=self._events`.
  - `InProcessConductor.__init__` reads `events`. `_grade` emits `self.events.judgment_scored(trial_id=setup.trial_id, score=trajectory.grade.score, binary_pass=trajectory.grade.binary_pass)` after `trajectory.grade` is populated.
  - `InProcessConductor._run_agent_loop` threads `events=self.events` and `trial_id=setup.trial_id` into `TrialRunner(...)`.
  - `TrialRunner.__init__` gains `events: RunDisplayEvents = _NULL_EVENTS`. Stashed on `self._events`.
  - `_AgentMetricsSink.__init__` gains `events` and `trial_id`. `record_generation` emits `trial_progress` after internal accumulation.
  - `tolokaforge/cli/main.py::run()` wraps `orchestrator.load_tasks()` + `orchestrator.run()` in a `with LiveRunDisplay.for_mode(...) as display:` block. `deps=OrchestratorDeps(events=display.events)` threads the sink.
- **Behaviour to lock (tier: `unit`, split across two new files):**
  - **`tests/unit/test_run_display_wiring.py`** — events fire from the right layers at the right times. Uses a **recording `RunDisplayEvents` implementation** (`_RecordingEvents` — 30-line test double that appends `(method, kwargs)` tuples to `events.calls`).
    - **Orchestrator emissions** — construct `Orchestrator(config, deps=OrchestratorDeps(events=recorder), conductor_factory=<InMemoryConductor factory>)`, run against a synthetic 3-task-1-repeat setup, assert `recorder.calls` starts with `run_started(total_trials=3, initial_completed=0)`, followed by three `trial_started` + three `trial_completed` in interleaved order (thread ordering non-deterministic; assert set-equality on the trial_ids), followed by `run_finished`.
    - **Conductor emission** — `InProcessConductor` with a fake grader that returns `Grade(score=0.7, binary_pass=True)`. Assert `judgment_scored(trial_id="a:0", score=0.7, binary_pass=True)` fires exactly once per trial.
    - **`_AgentMetricsSink` emission** — construct one directly, call `sink.record_generation(fake_result)` with `fake_result.usage.prompt_tokens=1000, completion_tokens=200, cost_usd=0.005`; assert `trial_progress(trial_id=..., prompt_tokens_delta=1000, completion_tokens_delta=200, cost_delta_usd=0.005)` fired.
    - **`_AgentMetricsSink` when `events` omitted** — defaults to `_NULL_EVENTS`; no exception. Existing behaviour preserved.
    - **Failure path** — `InMemoryConductor` returning a `TrialStatus.ERROR` trajectory: `orchestrator.run()` classifies it as retryable (per `_is_retryable_trajectory`), and if retries are set to 0, `trial_failed(retryable=True, error=<termination reason>)` fires (the retry-exhausted branch). With retries=1, the trial re-runs and eventually `trial_failed` OR `trial_completed` depending on the second attempt.
    - **Hard exception** — `InMemoryConductor` raising `RuntimeError("boom")`: `trial_failed(trial_id=..., error="boom", retryable=<True|False>)` fires; assert `error` contains "boom" (round-1 lock on error content — else regressions could silently drop the message).
  - **`tests/unit/test_run_display_cli_integration.py`** — full `tolokaforge run` under `CliRunner(mix_stderr=False)` with the stub `Orchestrator` from `tests/unit/test_cli_stdout_contract.py::_make_stub_orchestrator`, one addition: the stub records the `events` kwarg passed to `deps` so the test can inspect what the CLI wired up. Wraps the display around `run()` invocations.
    - **Under `--display=rich`:** `LiveRunDisplay.for_mode(DisplayMode.RICH)` returned an active display. Assert (via monkeypatching `LiveRunDisplay` with a fake that records enter/exit): the CLI called `__enter__` before `orchestrator.load_tasks` and `__exit__` after `orchestrator.run` returned, and the `events` kwarg on `OrchestratorDeps` was the fake display's `.events`.
    - **`emit_artifact_path` fires AFTER `__exit__`** (round-2 lock). Extend the fake display recorder to append a marker to a shared list on `__exit__`, and monkeypatch `emit_artifact_path` from `tolokaforge.cli._display` to append its own marker on entry. Assert the shared list is `["__exit__", "emit_artifact_path"]` in that order — proves the CLI does not fold the artifact-path emission inside the `with LiveRunDisplay(...)` block (which would let Live redraw artefacts contaminate stdout). A future refactor that inverts the order fails this test.
    - **Under `--display=plain`:** the fake `for_mode` returned a `_NoopDisplayCtx`. Assert `events` on `OrchestratorDeps` is `_NULL_EVENTS`.
    - **Under `--display=none`:** identical to `plain` — the display is a no-op AND the console/logs are silenced by B2's group callback. Assert `result.stderr == ""` still holds (regression on B2's silencer).
    - **On failure (stub `Orchestrator.run` raises):** the display's `__exit__` fires with the exception info. No unclean teardown; the exception propagates to click; `result.exit_code != 0`; `result.stdout == ""`.
    - **Composition with `-v` / `-q` / `--log-format`:** display activation is orthogonal to the log-format axis. `--display=rich --log-format=json` still activates the panel; `-v --display=rich` still bumps log level. Locked in the same style as B2's `TestCompositionWithLogFormat`.
    - **Stub orchestrator honours events:** the stub `_make_stub_orchestrator` is extended to fire `events.run_started` / `events.trial_completed` / `events.run_finished` from its `.run()` body so the CLI integration test can assert end-to-end wiring (event fires → panel visibly updates). Without this, the CLI test only proves plumbing; it doesn't prove the display sees the events. Round-1 lock.
- **Compatibility:**
  - **`OrchestratorDeps.events` is a new field with a default** — additive, no consumer break.
  - **`ConductorContext.events` is a new field with a default** — additive. `InProcessConductor.__init__` unpacks via `**vars(ctx)` (existing pattern); the new field surfaces as a new kwarg with default, safe.
  - **`TrialRunner.__init__(events=...)` is a new kwarg** — every existing caller (only `InProcessConductor._run_agent_loop`) is updated in this stage; test-side stand-ins with the old positional/kwarg signature keep working because the default is `_NULL_EVENTS`.
  - **CLI `run` command surface** unchanged — no new flags. The `--display` flag from B2 drives everything.
- **Deliverable:**
  - `tolokaforge/core/orchestrator.py` — 4 event-emission call sites in `run()` (no `run_worker` mirroring — follow-up); one field on `OrchestratorDeps`; one line on `_build_conductor` (pass `events`).
  - `tolokaforge/core/conductor.py` — one field on `ConductorContext`; one attribute in `InProcessConductor.__init__`; one call site in `_grade`; two kwargs threaded into `TrialRunner(...)`.
  - `tolokaforge/core/runner.py` — one kwarg on `TrialRunner.__init__`; two kwargs on `_AgentMetricsSink.__init__`; one call site in `record_generation`.
  - `tolokaforge/cli/main.py` — `with LiveRunDisplay.for_mode(...) as display:` wraps the `orchestrator.load_tasks() + orchestrator.run()` block; `deps=OrchestratorDeps(events=display.events)`.
  - `tests/unit/test_run_display_wiring.py` — new file, ~250 LOC.
  - `tests/unit/test_run_display_cli_integration.py` — new file, ~200 LOC.
- **Validation:**
  - `dev.run_tests(marker="unit")` green — every event-emission site fires under a `_RecordingEvents` sink.
  - `dev.run_tests(marker="canonical")` green — no invariants broken. Existing `test_conductor_contract.py`, `test_trial_executor_contract.py` should still pass (the additive kwargs default to `_NULL_EVENTS`).
  - `dev.lint_check(paths=["tolokaforge/cli", "tolokaforge/core", "tests/unit"])` clean.
  - **Manual smoke** (quote in PR body):
    - `TOLOKAFORGE_DISPLAY=rich uv run tolokaforge run --config examples/native/tool_use/run_config.yaml` — the Live panel renders in a real terminal, cost/tokens tick during the run, `Trial completed` log lines interleave cleanly above the panel, final artifact path lands on stdout. (Uses real LLM keys — one run only, then `Ctrl+C` when the layout is confirmed. Cost ~$0.01.)
    - `TOLOKAFORGE_DISPLAY=plain uv run tolokaforge run --config … 2>&1 | head` — plain-mode log stream, no Live panel, unchanged from A3's behaviour.
- **Doc updates:** none yet (Stage 3).

### Stage 3: Rich SVG snapshot goldens + `docs/CLI.md § Display modes` rewrite + CHANGELOG

- **Contract:**
  - `tests/canonical/test_run_display_goldens.py` — canonical test that:
    1. Constructs a fresh `LiveRunDisplay` (via the constructor, NOT `for_mode` — the test needs the class directly).
    2. Under `patch("tolokaforge.cli._run_display.datetime")`, replays a fixed sequence of ~15 events.
    3. Renders `display._layout` into a `Console(record=True, width=80|120, force_terminal=True)`.
    4. Calls `recorder.export_svg(title="tolokaforge run", theme=rich.terminal_theme.DEFAULT_TERMINAL_THEME)`.
    5. Compares string-equal to `tests/canonical/golden/run_display/panel_{80,120}.svg`.
  - Regeneration path: `pytest tests/canonical/test_run_display_goldens.py --update-canon -v` — matches the existing project convention (`tests/canonical/test_logging_goldens.py:15,120`).
  - `docs/CLI.md § Display modes` — rewrite the `rich` row + the paragraph immediately after ("`--display=rich` renders through the existing per-command `console.print(...)` calls; `--display=full` always falls back to `rich` because textual is not a dependency."). Written as current state (no "previously X, now Y") per AGENTS.md Core Rule 8.
  - `docs/CLI.md § Choosing between `console.print`, `make_progress`, and `make_live`` — append one line pointing at `LiveRunDisplay` for the "run-scoped panel" case.
  - `CHANGELOG.md` — "Unreleased / Feat" entries listing the new panel + the event Protocol.
- **Behaviour to lock (tier: `canonical`):**
  - `test_run_display_panel_svg_80` — 80-column golden matches byte-for-byte.
  - `test_run_display_panel_svg_120` — 120-column golden matches byte-for-byte.
  - `test_console_print_over_live_region_preserved` — enter the display, `console.print("[info]hello[/info]")`, capture the recorded output, assert `"hello"` appears exactly once and is NOT wrapped inside a screen-clear escape sequence (`\x1b[2J` or the "erase display" sequence Rich uses when Live redraws corrupt the region). This is a canonical assertion on the AC ("`console.print()` lines scroll above the Live region without corrupting it") — kept in canonical tier because it also locks Rich API stability.
  - `test_run_display_module_exports_public_surface` — new function on `tests/canonical/test_cli_display_invariants.py` (or a new file next to it) asserting `LiveRunDisplay`, `RunDisplayEvents` are importable from `tolokaforge.cli._run_display`, and that `RunDisplayEvents` is `runtime_checkable`.
- **Compatibility:**
  - **`docs/CLI.md § Display modes` — the `rich` row content is a compatibility surface**. Any future panel change (adding a new pane, changing the bottom-bar format) needs a CHANGELOG entry.
  - **SVG goldens are locked** — the panel's rendered shape is now a canonical artifact. Font colours / borders / glyphs cannot change silently.
- **Deliverable:**
  - `tests/canonical/test_run_display_goldens.py` — new file, ~120 LOC (goldens replay + comparison).
  - `tests/canonical/golden/run_display/panel_80.svg` — new golden (~15 KB).
  - `tests/canonical/golden/run_display/panel_120.svg` — new golden (~20 KB).
  - `tests/canonical/test_cli_display_invariants.py` — one new function `test_run_display_public_surface`.
  - `docs/CLI.md` — rewrite `rich` row + adjacent paragraph.
  - `CHANGELOG.md` — new "Feat" bullets under "Unreleased".
- **Validation:**
  - `dev.run_tests(marker="canonical", pattern="test_run_display or test_cli_display_invariants")` green.
  - `uv run pytest tests/unit tests/canonical -x -m "unit or canonical"` full suites green.
  - `dev.lint_check` and `dev.format_check` clean.
  - `rg "LiveRunDisplay|RunDisplayEvents|run_display" docs/` returns hits only in `docs/CLI.md` and the plan file (which lives in `docs/plans/`).
- **Doc updates:**

  `docs/CLI.md § Display modes` — the `rich` row rewrite:

  | `rich`   | Rich Live panel — left-pane trial list (status glyphs), right-pane structured summary of the focused trial (`turn N · in Xk / out Y tok · $Z.ZZ · last: <event_kind>`), bottom bar `{completed}/{total} · {running} running · ${cost} · in {prompt}/{completion} tok · fail {failed} · eta {eta}`. Log lines from `configure_root_logging` interleave above the panel (the display swaps the sentinel handler's stream to the Rich-wrapped `sys.stderr` on entry and restores on exit). |

  And the paragraph immediately after:

  > `--display=rich` renders `tolokaforge run` inside a `rich.Live` region owned by `tolokaforge.cli._run_display.LiveRunDisplay`. The orchestrator, conductor, and runner emit lifecycle events into a `RunDisplayEvents` Protocol (`trial_started`, `trial_progress`, `trial_completed`, `trial_failed`, `judgment_scored`, `run_started`, `run_finished`); `LiveRunDisplay` subscribes and repaints at 4 Hz. `--display=full` today collapses to `--display=rich` because `textual` is not a dependency; when C3 lands, `--display=full` will render a Textual TUI that consumes the same Protocol.

  `CHANGELOG.md` — under "Unreleased / Feat":

  ```markdown
  - **cli**: `--display=rich` renders `tolokaforge run` inside a Rich Live panel with a trial list, a structured summary of the focused trial (turn count / tokens / cost / last-event kind), and a status bar showing completed/total, running workers, cumulative cost, tokens, failures, and ETA. New event Protocol `tolokaforge.cli._run_display.RunDisplayEvents` is emitted by the orchestrator, conductor, and runner; `LiveRunDisplay` is the sole shipped consumer. See [docs/CLI.md](docs/CLI.md) § Display modes. (#285)
  ```

## Test strategy

- **Unit tier for the panel (`test_run_display.py`)** — synthetic event replay against a fresh `LiveRunDisplay`. No Rich `Live` mainloop — the tests call `display.<event>(...)` methods directly and assert on `display._trials` state, `display._focused_trial_id`, `display._completed` etc. Bottom-bar formatting locked via a table-driven test on the pure `_format_bottom_bar` helper. `__enter__`/`__exit__` tested against a fake sentinel handler on the root logger (`logging.StreamHandler(io.StringIO())` tagged with `_TOLOKAFORGE_ROOT_HANDLER_SENTINEL`) to lock the stream re-point + restore. Rich SVG snapshot is NOT this tier's concern.
- **Unit tier for the wiring (`test_run_display_wiring.py`)** — a recording `RunDisplayEvents` test double (`_RecordingEvents`) is threaded through `Orchestrator(deps=OrchestratorDeps(events=recorder))` with an `InMemoryConductor` factory. Assert the recorded call list. No real LLM, no real Docker, no real runner grpc — pure Python at the orchestrator / conductor level.
- **Unit tier for the CLI integration (`test_run_display_cli_integration.py`)** — `CliRunner(mix_stderr=False)` + `_make_stub_orchestrator` (extended to record what `events` was wired to). Monkeypatches `LiveRunDisplay` with a fake that records `__enter__` / `__exit__` calls. Locks the "for_mode is called with `ctx.obj["display_mode"]`" contract at the CLI boundary.
- **Canonical tier for the SVG goldens (`test_run_display_goldens.py`)** — Rich `Console(record=True, width=80|120, force_terminal=True)` + fixed `datetime.now` + fixed event sequence. Golden regeneration via `pytest --update-canon` CLI flag, matching the existing `test_logging_goldens.py` pattern. Two goldens (80/120 col) match the AC exactly.
- **Canonical tier for the `console.print` coexistence** — recorded output over a synthetic session: `display.__enter__()` → `console.print("hello")` → `display.__exit__()`. Assert `"hello"` renders once, not wrapped in a screen-clear escape. Additionally locks that Rich's Live-preserves-console-print behaviour did not regress on the currently-pinned Rich (14.1.0; `>=13.0.0`).
- **No integration tier** — no real orchestrator, no real runner. The one real-run smoke lives in Stage 2's manual validation (quoted in the PR body). This matches AGENTS.md posture: unit / canonical for deterministic contracts, integration only when API keys / Docker services / real LLMs are needed. B1's rendering is deterministic given synthetic events, so unit + canonical cover it.
- **Grep-guards continue to pass** — no new `Console(...)` in `tolokaforge/cli/` outside `_display.py`; no new `print(` outside `_display.py`. `_run_display.py` uses `make_live(...)` from `_display.py` and imports `console` from `_display.py` — never constructs its own.

## Discovered issues

**Fix in this PR** (Stage 2):
- `_AgentMetricsSink` currently only tracks the AGENT's per-generation metrics — the USER simulator's LLM calls (`UserSimulator.generate_reply(...)`) go through the same `LLMClient` path but their token usage is NOT surfaced via `record_generation`. B1's bottom-bar cost is the agent's cost only. Recording the user-side cost via a symmetric hook is orthogonal to B1's AC (which says nothing about per-role attribution). **Do not fix in this PR** — file follow-up. Rationale: B3 (budgets) is where cost breakdown belongs; adding a `RunDisplayEvents.user_progress(...)` here is premature.

Actually, no fix belongs in this PR beyond what the plan already covers. B1's scope is tight — the panel + the event Protocol. Neighbouring hygiene work I noticed (below) is filed as issues, not folded in.

**Filed as follow-up issues** (via `gh issue create`, real issue numbers below):

1. **#328 — `cli(display): per-tool-call granularity — tool_call_started / tool_call_completed events`** — the right pane's structured summary is derived from `trial_progress` cumulative totals today; per-tool granularity would let the panel show a live tool-call stream. Not in B1 AC. Depends on B1 landing first.

2. **#329 — `cli(display): user-simulator / judge cost attribution in RunDisplayEvents`** — bottom bar shows agent cost only. User simulator (`tau_*` tasks) and rubric judge (rubric tasks) accrue their own cost. Cross-cuts B3 (#283, budgets) — cap should probably enforce on total cost, not agent-only.

3. **#330 — `cli(display): configurable refresh rate for LiveRunDisplay`** — hard-coded at `refresh_per_second=4.0` today. If operators report jitter on slow terminals, expose as `--display-refresh-hz` or via `TOLOKAFORGE_DISPLAY_REFRESH_HZ` env var. Deferred — no operator demand today.

4. **#331 — `cloud-runtime: RunDisplayEvents over gRPC for a distributed conductor`** — Cloud Runtime target (`docs/CLOUD_RUNTIME_ARCHITECTURE.md`) will push conductor execution to a trial-plane worker. The event Protocol is process-local today. When that lands, the events need a gRPC stream carrier. Design-note issue for the cloud-runtime milestone.

5. **#332 — `docs(cli): expand LiveRunDisplay coverage in Choosing between console.print / make_progress / make_live`** — Stage 3 adds one line, but a longer-form example (with a real event sequence + screenshot) would help external contributors consume the same Protocol for their own displays.

6. **#333 — `cli(display): wire RunDisplayEvents through Orchestrator.run_worker (distributed-worker path)`** — B1 intentionally does not mirror the emission sites in `run_worker()` (dead-on-arrival — no display consumer for `worker` today). When a worker-scoped display or Cloud Runtime's trial-plane worker gains a panel, wire the same emission pattern. Filed 2026-07-15.

7. **#319 — `cli(env): TOLOKAFORGE_LOG_FORMAT env-var equivalent for --log-format`** — already filed by B2. Cross-reference only; no new filing.

**Not filed (rejected)**:

- "Add `LiveRunDisplay` for `tolokaforge prepare` / `tolokaforge worker`" — `prepare` is a one-shot queue seeder; the panel would render for ~1 second and add zero value. `worker` runs a subset of trials; a panel per-worker would be useful in a multi-terminal debug session but no operator has asked. Reject.
- "Add a `--display=rich-minimal` variant that shows only the bottom bar" — flag proliferation. If users want less real estate, they can `--display=plain` and read the log lines. Reject.
- "Emit events over the log stream so external tools can consume them" — orthogonal. Log records under `--log-format=json` already carry structured `k=v` scope pairs; a downstream consumer can grep for `Trial started` / `Trial completed` today. Reject.

## Risks / open questions

- **Log-line coexistence via stream re-point is Rich-version-sensitive.** D5 relies on Rich patching `sys.stderr` in the process globals when `make_live(redirect_stderr=True)` (the default) is called (verified against Rich 14.1.0 `rich/live.py:197-199`). If a future Rich release changes the redirection mechanism (e.g. context-var-based instead of module-global), the sentinel-handler stream swap becomes a no-op. Locked in Stage 3's `test_console_print_over_live_region_preserved` — if Rich changes, that test fails loud. Fix window is one Rich release; the fallback is (b) from D5 (swap to `RichHandler`).
- **`_AgentMetricsSink.record_generation` is on the LLM turn's hot path.** Emission of `trial_progress` runs after metrics accumulation. Cost per call is bounded by D8's single-lock body — dict lookup + a few arithmetic ops + one `datetime.now()` skipped for progress events. Acquisition cost is a few hundred ns; total events/sec is ~48 (12 workers × 4 turns/sec), so lock contention is negligible on the LLM turn scale (10s+). Correctness under concurrency is guaranteed exactly-once by D8's lock — see `test_trial_progress_is_atomic_under_concurrent_workers`.
- **80-col vs 120-col rendering divergence.** The right-pane structured summary line has a fixed shape but wraps differently at 80 vs 120 cols. Both goldens capture this — but a 100-col terminal renders neither. Rich handles this via `Layout` sizing, so the panel remains readable at any width; only the golden coverage is exactly 80/120. If a bug surfaces at 100 cols, add a third golden.
- **`_estimate_eta_seconds` before the first completion is `None`.** The bottom bar shows `eta n/a` until the first trial finishes. On a run with one very-long-lasting trial (30+ minutes), operators will see `eta n/a` for a long time. Acceptable — linear extrapolation from one completion is unstable anyway; showing `n/a` is honest. If operators complain, the follow-up is a queue-based ETA (which `tolokaforge status` already computes).
- **`--display=full` behaviour today.** B2 already collapses `full` to `rich` at the group callback boundary. `LiveRunDisplay.for_mode(DisplayMode.FULL)` also returns an active `LiveRunDisplay` (D4). When C3 lands `uv add textual` and the Textual TUI class, `for_mode(DisplayMode.FULL)` will return the Textual variant. Locked forward-compat in Stage 1's `for_mode` test: today, both `RICH` and `FULL` produce a `LiveRunDisplay` instance; C3 will update the `FULL` branch to construct the Textual class and preserve the "`ctx.events` returns a `RunDisplayEvents` implementation" contract.
- **`TrialRunner` is also used by the judge loop** — actually no. The judge runs inside the Runner container via a separate loop; `TrialRunner` is the agent-user loop only. Wiring `_AgentMetricsSink.events` is safe.
- **`_NullRunDisplayEvents` vs `Optional[RunDisplayEvents]`.** Chosen the null-object over `Optional[...]` to avoid `if events is not None: events.method(...)` at every emission site. Cost: one class, six no-op methods. Payoff: no defensive branching in the hot path. Trade-off is orthogonal to AGENTS.md Core Rule 1 — the null object is DELIBERATE and DOCUMENTED, not a silent failure fallback.
- **Rich `Console.export_svg` output is version-sensitive.** Goldens will be generated under the currently-installed Rich (14.1.0; `pyproject.toml` pins `rich>=13.0.0`). If Rich bumps and default styles change, the goldens may drift — the update flow is `pytest --update-canon`, and the PR that bumps Rich re-generates. Not a blocker.
- **`console.print("Loading tasks…")` between `__enter__` and `orchestrator.load_tasks`** — the Live region is up but no events have fired yet, so the layout renders empty panels. Rich handles this — empty `Panel(Text(""), title="Trials")` renders a bordered empty box. Locked in Stage 3's 80-col golden with `total_trials=0` at the top.
- **What if `--display=rich` is chosen on a system where the terminal cannot render Unicode glyphs (`⏳`, `✓`, `✗`)?** Rich handles the fallback to `[ ]`, `[X]`, `[!]` via `console.legacy_windows` / the terminal's declared encoding. No explicit branching in `LiveRunDisplay`. If a downstream operator reports garbled glyphs, the panel author reads the terminal's encoding and picks a portable fallback set — out of scope for B1.
- **Test file naming.** `test_run_display.py` (Stage 1), `test_run_display_wiring.py` (Stage 2), `test_run_display_cli_integration.py` (Stage 2), `test_run_display_goldens.py` (Stage 3, canonical). Naming is grep-obvious; no conflict with existing `test_cli_display.py` (A1 factory tests) or `test_cli_display_flag.py` (B2 flag tests).
