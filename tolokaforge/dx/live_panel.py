"""Rich ``Live`` progress panel for ``tolokaforge run`` under ``--display=rich``.

Under :attr:`DisplayMode.RICH` (and today :attr:`DisplayMode.FULL`, which
:mod:`tolokaforge.dx._display` collapses to ``RICH`` at the callback
boundary), :class:`LiveRunDisplay` renders a three-region panel: left-pane
trial list, right-pane structured summary of the focused trial (turn count
/ tokens / cost / last-event kind), bottom status bar with cost / tokens
/ ETA / failure counts.

Under any other mode (:attr:`DisplayMode.PLAIN` / :attr:`DisplayMode.LOG`
/ :attr:`DisplayMode.NONE`), :meth:`LiveRunDisplay.for_mode` returns a
no-op context manager and the existing log-line stream is what the user
sees.

The panel subscribes to :class:`RunDisplayEvents` — a small Protocol the
orchestrator, conductor, and runner emit into. The Protocol has a no-op
default (:data:`_NULL_EVENTS`), so callers that never build a display can
still thread ``events`` through without conditional branches.
"""

from __future__ import annotations

import logging
import sys
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from tolokaforge.core.logging import _TOLOKAFORGE_ROOT_HANDLER_SENTINEL
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    RunDisplayEvents,
)
from tolokaforge.dx._display import DisplayMode, format_duration, make_live

_LOGGER = logging.getLogger("tolokaforge.dx.live_panel")


def _now() -> datetime:
    """Wall-clock accessor used for all timestamp assignment inside the display.

    Extracted so tests can monkey-patch it to a deterministic factory when
    they need a strictly-ordered sequence of ``last_update_ts`` values."""
    return datetime.now()


@dataclass
class _TrialCard:
    """Per-trial state feeding the left pane and right pane.

    Only lifecycle events (``trial_started`` / ``trial_completed`` /
    ``trial_failed`` / ``judgment_scored``) bump :attr:`last_update_ts`.
    ``trial_progress`` mutates ``turn_count`` + ``last_event_kind`` +
    counters but leaves ``last_update_ts`` untouched, so focus does not
    alternate on per-turn ticks.
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
    last_update_ts: datetime = field(default_factory=_now)
    turn_count: int = 0
    last_event_kind: str = "started"


@dataclass
class _BottomBarStats:
    """Pure inputs for :func:`_format_bottom_bar` — no display state.

    Test fixtures instantiate this directly; :meth:`LiveRunDisplay._render_bottom_bar`
    populates it from ``self`` under the lock.

    ``cost_style`` names the theme token wrapping the ``$X.YY`` cost
    segment: ``"default"`` (unwrapped, existing shape), ``"warn"``
    (yellow — cumulative cost ≥ 80 % of the run's cost budget), or
    ``"error"`` (bold red — ≥ 100 %). Derived by :func:`_cost_bar_style`.
    """

    completed: int
    total: int
    running: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    failed: int
    eta_seconds: float | None
    cost_style: str = "default"


def _format_tokens(n: int) -> str:
    """Render token counts: ``6.8k`` for large counts, raw integer below.

    Threshold is 5_000: ``6800 → "6.8k"``, ``41200 → "41.2k"``, ``1234 → "1234"``.
    """
    if n >= 5_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _format_cost(cost: float) -> str:
    """Render cumulative cost: ``$0.00`` at zero, ``$<0.01`` for tiny amounts."""
    if cost <= 0.0:
        return "$0.00"
    if cost < 0.01:
        return "$<0.01"
    return f"${cost:.2f}"


def _format_eta(eta_seconds: float | None) -> str:
    """Render ETA as ``MM:SS`` under 1h, ``HH:MM:SS`` above, ``n/a`` when unknown."""
    if eta_seconds is None:
        return "n/a"
    return format_duration(eta_seconds)


def _cost_bar_style(cost_usd: float, budget_usd: float | None) -> str:
    """Return the theme token wrapping the bottom-bar's cost segment.

    ``"default"`` — no budget context or below the amber threshold.
    ``"warn"`` — cumulative cost ≥ 80 % of ``budget_usd``.
    ``"error"`` — cumulative cost ≥ 100 % of ``budget_usd``.

    Defensive on ``budget_usd <= 0.0`` (would divide by zero); returns
    ``"default"`` so a mis-configured cap doesn't paint the panel red.
    """
    if budget_usd is None or budget_usd <= 0.0:
        return "default"
    ratio = cost_usd / budget_usd
    if ratio >= 1.0:
        return "error"
    if ratio >= 0.8:
        return "warn"
    return "default"


def _format_bottom_bar(stats: _BottomBarStats) -> str:
    """Render the bottom status bar — locked literal shape.

    When ``stats.cost_style`` is ``"warn"`` or ``"error"`` the ``$X.YY``
    segment is wrapped in ``[warn]…[/warn]`` / ``[error]…[/error]`` Rich
    markup; the rest of the bar is unchanged. Below 80 % of the budget
    (``cost_style == "default"``) the segment is unwrapped and the
    returned line is byte-identical to the pre-B3 shape.
    """
    cost_segment = _format_cost(stats.cost_usd)
    if stats.cost_style != "default":
        cost_segment = f"[{stats.cost_style}]{cost_segment}[/{stats.cost_style}]"
    return (
        f"{stats.completed}/{stats.total} · {stats.running} running · "
        f"{cost_segment} · "
        f"in {_format_tokens(stats.prompt_tokens)} / "
        f"out {_format_tokens(stats.completion_tokens)} tok · "
        f"fail {stats.failed} · eta {_format_eta(stats.eta_seconds)}"
    )


_PHASE_LABELS: dict[str, str] = {
    "loading_tasks": "Loading tasks",
    "starting_services": "Starting services",
    "services_ready": "Services ready",
    "connecting_runtime": "Connecting runtime",
    "priming_queue": "Priming queue",
}


def _format_phase_line(phase: str, detail: str | None) -> str:
    """Render the bottom bar during the pre-``run_started`` startup window.

    Panel shows a human phase label with a spinner-adjacent ellipsis while
    the run is still booting (``total_trials == 0``). Once ``run_started``
    fires the panel switches back to the counters formatter.
    """
    label = _PHASE_LABELS.get(phase, phase.replace("_", " ").capitalize())
    if detail:
        return f"{label}… ({detail})"
    return f"{label}…"


def _looks_like_auth_error(error_str: str) -> bool:
    """True when ``error_str`` came from a 401/403 provider auth failure.

    Litellm wraps every provider's auth error in ``openai.AuthenticationError``,
    whose string representation carries the class name plus the original
    response body. Substring matching on the class name is stable across
    providers; the ``"code":401`` fallback covers the raw JSON body case
    that OpenRouter surfaces.
    """
    if not error_str:
        return False
    if "AuthenticationError" in error_str:
        return True
    if '"code":401' in error_str or '"code": 401' in error_str:
        return True
    if '"code":403' in error_str or '"code": 403' in error_str:
        return True
    return False


def _derive_hint(error_str: str) -> str | None:
    """One-line remediation hint keyed off common failure signatures.

    Returns ``None`` when nothing recognisable — the panel then falls
    back to just showing the raw error string.
    """
    lower = error_str.lower() if error_str else ""
    if "openrouter" in lower and _looks_like_auth_error(error_str):
        return "Check OPENROUTER_API_KEY in .env"
    if "openai" in lower and _looks_like_auth_error(error_str):
        return "Check OPENAI_API_KEY in .env"
    if "anthropic" in lower and _looks_like_auth_error(error_str):
        return "Check ANTHROPIC_API_KEY in .env"
    if _looks_like_auth_error(error_str):
        return "Check the provider API key in .env"
    return None


def _truncate_error(error_str: str, *, width: int = 60) -> str:
    """First line of ``error_str``, truncated with an ellipsis."""
    if not error_str:
        return ""
    first_line = error_str.split("\n", 1)[0]
    if len(first_line) <= width:
        return first_line
    return first_line[: width - 1] + "…"


class _NoopDisplayCtx:
    """Context manager returned by :meth:`LiveRunDisplay.for_mode` under
    ``PLAIN`` / ``LOG`` / ``NONE``. Enter and exit are pass-through; ``events``
    is :data:`_NULL_EVENTS`, so the caller wires ``deps=OrchestratorDeps(events=ctx.events)``
    without branching on the mode.
    """

    events: RunDisplayEvents = _NULL_EVENTS

    def __enter__(self) -> _NoopDisplayCtx:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class LiveRunDisplay:
    """Rich Live panel for ``tolokaforge run`` under ``--display=rich``.

    Every public event-handler method acquires :attr:`_lock` at the top of
    its body — with 12 concurrent workers each firing ``trial_progress`` from
    its own thread, ``x += n`` compiles to three bytecodes with GIL-release
    windows, and concurrent updates would silently lose data without a lock.
    """

    def __init__(
        self,
        *,
        refresh_per_second: float = 4.0,
        max_trial_rows: int = 20,
        cost_budget_usd: float | None = None,
    ) -> None:
        # Reentrant — _refresh_live_locked (called inside every event handler
        # while holding the lock) invokes _build_layout, whose render helpers
        # (_visible_cards, _render_right_pane, _render_bottom_bar) re-acquire
        # the same lock. Non-reentrant Lock deadlocks here.
        self._lock: threading.RLock = threading.RLock()
        self._live: Live | None = None
        self._trials: dict[str, _TrialCard] = {}
        self._focused_trial_id: str | None = None
        self._total_trials: int = 0
        self._initial_completed: int = 0
        self._completed: int = 0
        self._failed: int = 0
        self._running: int = 0
        self._prompt_tokens: int = 0
        self._completion_tokens: int = 0
        self._total_cost_usd: float = 0.0
        self._run_start_ts: datetime | None = None
        self._finished: bool = False
        self._max_trial_rows: int = max_trial_rows
        self._refresh_per_second: float = refresh_per_second
        # ``None`` disables the amber / red styling; ``0.0`` is treated as
        # "no budget" by :func:`_cost_bar_style` (defensive against a
        # zero-cap misconfiguration).
        self._cost_budget_usd: float | None = cost_budget_usd
        # Pre-``run_started`` phase visibility (Docker startup window).
        # ``None`` means either the run has already started (bottom bar
        # switches to counters) or no phase event has fired yet.
        self._current_phase: str | None = None
        self._current_phase_detail: str | None = None
        # Top-of-panel error banner. Populated once the first auth-shaped
        # ``trial_failed`` fires so a bad key doesn't hide as ``fail 1``
        # in the counters. Tuple: (title, message, hint | None).
        self._banner: tuple[str, str, str | None] | None = None
        self._saved_log_streams: list[tuple[logging.Handler, object]] = []
        self._layout: Layout = self._build_layout()

    @classmethod
    def for_mode(
        cls,
        mode: DisplayMode,
        *,
        cost_budget_usd: float | None = None,
    ) -> AbstractContextManager[LiveRunDisplay | _NoopDisplayCtx]:
        """Return a fresh :class:`LiveRunDisplay` under ``RICH`` / ``FULL``,
        a :class:`_NoopDisplayCtx` under any other mode.

        The caller passes ``mode = ctx.obj["display_mode"]`` (a resolved
        :class:`DisplayMode` enum) so this method never re-parses the flag
        or env var. ``cost_budget_usd`` — when the CLI resolved a cost
        cap — enables the amber@80 % / red@100 % styling on the bottom-bar
        cost segment.
        """
        if mode in (DisplayMode.RICH, DisplayMode.FULL):
            return cls(cost_budget_usd=cost_budget_usd)
        return _NoopDisplayCtx()

    @property
    def events(self) -> RunDisplayEvents:
        """The event sink the caller threads into the orchestrator."""
        return self

    def __enter__(self) -> LiveRunDisplay:
        self._live = make_live(self._layout, refresh_per_second=self._refresh_per_second)
        self._live.__enter__()
        # In production, ``configure_root_logging`` guarantees at most one
        # sentinel-tagged handler at any time; iterating defensively keeps
        # the re-point correct if an embedder or test suite has installed
        # additional sentinel handlers.
        for handler in logging.getLogger().handlers:
            if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
                self._saved_log_streams.append((handler, handler.stream))
                handler.stream = sys.stderr
        return self

    def __exit__(self, *exc_info: object) -> None:
        for handler, stream in self._saved_log_streams:
            handler.stream = stream
        self._saved_log_streams = []
        if self._live is not None:
            self._live.__exit__(*exc_info)
            self._live = None

    # RunDisplayEvents implementation ---------------------------------

    def run_started(self, *, total_trials: int, initial_completed: int) -> None:
        with self._lock:
            self._total_trials = total_trials
            self._initial_completed = initial_completed
            self._completed = initial_completed
            self._run_start_ts = _now()
            # Trials are about to dispatch — bottom bar switches from
            # "Starting services…" to the counters formatter.
            self._current_phase = None
            self._current_phase_detail = None
            self._refresh_live_locked()

    def phase_changed(self, *, phase: str, detail: str | None = None) -> None:
        """Record a pre-``run_started`` milestone (Docker startup, etc.).

        The bottom bar reads :attr:`_current_phase` when ``_total_trials == 0``
        and renders ``"Starting services… (detail)"`` instead of the empty
        ``0/0 · 0 running · …`` line. Once ``run_started`` fires, phase state
        is cleared and the bar switches to counters.
        """
        with self._lock:
            self._current_phase = phase
            self._current_phase_detail = detail
            self._refresh_live_locked()

    def trial_started(self, *, trial_id: str, task_id: str, trial_index: int) -> None:
        with self._lock:
            card = _TrialCard(
                trial_id=trial_id,
                task_id=task_id,
                trial_index=trial_index,
                status="running",
                last_update_ts=_now(),
                last_event_kind="started",
            )
            self._trials[trial_id] = card
            self._running += 1
            self._focused_trial_id = trial_id
            self._refresh_live_locked()

    def trial_progress(
        self,
        *,
        trial_id: str,
        prompt_tokens_delta: int,
        completion_tokens_delta: int,
        cost_delta_usd: float,
    ) -> None:
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            card.prompt_tokens += prompt_tokens_delta
            card.completion_tokens += completion_tokens_delta
            card.cost_usd += cost_delta_usd
            card.turn_count += 1
            card.last_event_kind = "progress"
            # last_update_ts intentionally NOT bumped — focus is stable across
            # per-turn ticks.
            self._prompt_tokens += prompt_tokens_delta
            self._completion_tokens += completion_tokens_delta
            self._total_cost_usd += cost_delta_usd
            self._refresh_live_locked()

    def trial_completed(self, *, trial_id: str, binary_pass: bool, score: float | None) -> None:
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            was_running = card.status == "running"
            card.status = "completed"
            card.binary_pass = binary_pass
            card.score = score
            card.last_event_kind = "completed"
            card.last_update_ts = _now()
            if was_running and self._running > 0:
                self._running -= 1
            self._completed += 1
            self._focused_trial_id = trial_id
            self._refresh_live_locked()

    def trial_failed(self, *, trial_id: str, error: str, retryable: bool) -> None:
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            was_running = card.status == "running"
            card.status = "failed"
            card.error = error
            card.last_event_kind = "failed"
            card.last_update_ts = _now()
            if was_running and self._running > 0:
                self._running -= 1
            self._failed += 1
            self._focused_trial_id = trial_id
            # Surface auth-shaped failures in a top-of-panel banner so
            # they don't hide as one row in ``fail N``. Fired on the
            # first auth-classified failure only — subsequent ones show
            # in the trials pane like normal failures.
            if self._banner is None and _looks_like_auth_error(error):
                hint = _derive_hint(error) or "Verify the provider API key"
                self._banner = (
                    "Auth failure",
                    _truncate_error(error, width=120),
                    hint,
                )
            self._refresh_live_locked()

    def judgment_scored(self, *, trial_id: str, score: float, binary_pass: bool) -> None:
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            card.score = score
            card.binary_pass = binary_pass
            card.last_event_kind = "judged"
            card.last_update_ts = _now()
            self._focused_trial_id = trial_id
            self._refresh_live_locked()

    def run_finished(self, *, output_dir: Path) -> None:
        with self._lock:
            self._finished = True
            self._refresh_live_locked()

    def _refresh_live_locked(self) -> None:
        """Rebuild the layout and push it to Rich's Live if active.

        Rich's ``Layout.__init__`` binds child renderables into a tree once;
        state changes to ``self._trials`` / counters do not propagate back
        into those bound children. We rebuild the layout from scratch and
        call ``Live.update(...)`` on every event so the auto-refresh thread
        renders the CURRENT state, not the "empty startup" snapshot.

        ``refresh=False`` — auto-refresh at ``refresh_per_second`` handles
        the visible repaint; we just swap the source of truth. Caller MUST
        already hold :attr:`_lock`.
        """
        if self._live is None:
            return
        self._live.update(self._build_layout(), refresh=False)

    # Internal helpers -----------------------------------------------

    def _lazy_card_locked(self, trial_id: str) -> _TrialCard:
        """Create a placeholder card for a trial that never emitted ``trial_started``.

        Guards against ordering drift in the orchestrator (which we control);
        raising would corrupt the runner loop per the Protocol contract. Caller
        MUST already hold :attr:`_lock`.
        """
        _LOGGER.debug("Creating lazy card for unknown trial_id=%s", trial_id)
        task_id, _, idx = trial_id.partition(":")
        try:
            trial_index = int(idx) if idx else 0
        except ValueError:
            trial_index = 0
        card = _TrialCard(
            trial_id=trial_id,
            task_id=task_id or trial_id,
            trial_index=trial_index,
            status="running",
            last_update_ts=_now(),
            last_event_kind="started",
        )
        self._trials[trial_id] = card
        return card

    def _visible_cards(self) -> list[_TrialCard]:
        """Return the trimmed set of cards the left pane should render.

        Every running trial is always shown; completed / failed trials scroll
        off in ``last_update_ts`` order once :attr:`_max_trial_rows` fills.
        Holds the lock across the whole sort/partition so a concurrent event
        cannot mutate ``card.status`` or ``card.last_update_ts`` mid-read.
        """
        with self._lock:
            all_cards = list(self._trials.values())
            if len(all_cards) <= self._max_trial_rows:
                return sorted(all_cards, key=lambda c: c.last_update_ts, reverse=True)
            running = [c for c in all_cards if c.status == "running"]
            terminal = sorted(
                (c for c in all_cards if c.status != "running"),
                key=lambda c: c.last_update_ts,
                reverse=True,
            )
            running_sorted = sorted(running, key=lambda c: c.last_update_ts, reverse=True)
            remaining = self._max_trial_rows - len(running_sorted)
            if remaining <= 0:
                return running_sorted[: self._max_trial_rows]
            return running_sorted + terminal[:remaining]

    def _estimate_eta_seconds(self) -> float | None:
        """Linear extrapolation from run-elapsed wall-time × remaining/completed.

        Returns ``None`` before the first in-run completion. Caller MUST hold
        :attr:`_lock` (reads shared counters directly).
        """
        if self._run_start_ts is None:
            return None
        completed_this_run = self._completed - self._initial_completed
        remaining = self._total_trials - self._completed
        if completed_this_run <= 0 or remaining <= 0:
            return None
        elapsed = (_now() - self._run_start_ts).total_seconds()
        return elapsed / completed_this_run * remaining

    def _build_layout(self) -> Layout:
        """Build (or rebuild) the layout tree from current state.

        The optional ``banner`` row appears only when :attr:`_banner` is
        populated — a failed panel with no banner is byte-identical to the
        pre-banner layout so existing goldens still match on non-auth
        failure paths.
        """
        layout = Layout()
        with self._lock:
            banner = self._banner
        if banner is not None:
            layout.split_column(
                Layout(name="banner", size=5),
                Layout(name="main", ratio=1),
                Layout(name="bottom", size=1),
            )
            layout["banner"].update(self._render_banner(banner))
        else:
            layout.split_column(
                Layout(name="main", ratio=1),
                Layout(name="bottom", size=1),
            )
        layout["main"].split_row(
            Layout(name="trials", ratio=2),
            Layout(name="focused", ratio=3),
        )
        layout["trials"].update(self._render_left_pane())
        layout["focused"].update(self._render_right_pane())
        layout["bottom"].update(self._render_bottom_bar())
        return layout

    def _render_banner(self, banner: tuple[str, str, str | None]) -> Panel:
        title, message, hint = banner
        lines = [message]
        if hint:
            lines.append(f"[warn]Hint:[/warn] {hint}")
        body = Text.from_markup("\n".join(lines))
        return Panel(body, title=f"[error]✗ {title}[/error]", border_style="error")

    def _render_left_pane(self) -> Panel:
        glyphs = {"running": "⏳", "completed": "✓", "failed": "✗"}
        rendered: list[str] = []
        has_markup = False
        for card in self._visible_cards():
            glyph = glyphs.get(card.status, "•")
            base = f"{glyph} {card.task_id} · {card.trial_index}"
            if card.status == "failed" and card.error:
                err = _truncate_error(card.error, width=40)
                rendered.append(f"{base}  [error]{err}[/error]")
                has_markup = True
            else:
                rendered.append(base)
        # Preserve byte-identical output vs pre-error-render goldens: only
        # switch to ``Text.from_markup`` when we actually have markup to
        # interpret. Rows with no bracket content render via plain ``Text``.
        if not rendered:
            body = Text("(no trials yet)")
        elif has_markup:
            body = Text.from_markup("\n".join(rendered))
        else:
            body = Text("\n".join(rendered))
        return Panel(body, title="Trials")

    def _render_right_pane(self) -> Panel:
        with self._lock:
            trial_id = self._focused_trial_id
            card = self._trials.get(trial_id) if trial_id is not None else None
            snapshot: (
                tuple[
                    int,  # turn_count
                    int,  # prompt_tokens
                    int,  # completion_tokens
                    float,  # cost_usd
                    str,  # last_event_kind
                    str,  # status
                    str | None,  # error
                    str,  # task_id
                    int,  # trial_index
                ]
                | None
            ) = (
                (
                    card.turn_count,
                    card.prompt_tokens,
                    card.completion_tokens,
                    card.cost_usd,
                    card.last_event_kind,
                    card.status,
                    card.error,
                    card.task_id,
                    card.trial_index,
                )
                if card is not None
                else None
            )
        if snapshot is None:
            body = Text("(waiting for first trial)")
        else:
            (
                turn,
                prompt,
                completion,
                cost,
                last_kind,
                status,
                error,
                task_id,
                trial_index,
            ) = snapshot
            if status == "failed" and error:
                # Give the operator the full picture on the focused failed
                # trial: task identity, first line of the error, and — for
                # recognisable failure signatures — a remediation hint.
                hint = _derive_hint(error)
                parts = [
                    f"[error]FAILED[/error]  {task_id} · {trial_index}",
                    "",
                    _truncate_error(error, width=200),
                ]
                if hint:
                    parts.append("")
                    parts.append(f"[warn]Hint:[/warn] {hint}")
                body = Text.from_markup("\n".join(parts))
            else:
                body = Text(
                    f"turn {turn} · "
                    f"in {_format_tokens(prompt)} / out {_format_tokens(completion)} tok · "
                    f"{_format_cost(cost)} · "
                    f"last: {last_kind}"
                )
        return Panel(body, title="Focused trial")

    def _render_bottom_bar(self) -> Text:
        with self._lock:
            # Startup window: no trials yet AND a phase event has fired.
            # Renders "Starting services…" instead of the empty counters.
            if self._total_trials == 0 and self._current_phase is not None:
                phase_line = _format_phase_line(self._current_phase, self._current_phase_detail)
                return Text(phase_line, style="muted")
            cost_style = _cost_bar_style(self._total_cost_usd, self._cost_budget_usd)
            stats = _BottomBarStats(
                completed=self._completed,
                total=self._total_trials,
                running=self._running,
                cost_usd=self._total_cost_usd,
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                failed=self._failed,
                eta_seconds=self._estimate_eta_seconds(),
                cost_style=cost_style,
            )
        line = _format_bottom_bar(stats)
        # ``Text.from_markup`` interprets ``[warn]…[/warn]`` / ``[error]…[/error]``
        # against the shared theme. The "default" path stays on ``Text(...)``
        # so the pre-B3 goldens (unset-budget baseline) remain byte-identical.
        if cost_style == "default":
            return Text(line)
        return Text.from_markup(line)


__all__ = [
    "LiveRunDisplay",
    "RunDisplayEvents",
]
