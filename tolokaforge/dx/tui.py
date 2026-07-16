"""Textual TUI for ``tolokaforge run --display=full``.

:class:`TextualRunApp` is a :class:`textual.app.App` that consumes the
:class:`RunDisplayEvents` seam and renders a keyboard-navigable, tabbed
run view. It slots into :meth:`LiveRunDisplay.for_mode` alongside the
Rich :class:`LiveRunDisplay` and the passive :class:`_NoopDisplayCtx`;
the CLI callback picks between them via ``--display=``.

The app runs on the main UI thread inside a dedicated background thread
started at ``__enter__``. Every :class:`RunDisplayEvents` method is
called by the orchestrator / conductor / runner from *their* worker
threads and routes work onto Textual's event loop via
:meth:`App.call_from_thread` (or buffers into a pending queue when the
app has not mounted yet). Handlers never raise — the Protocol contract
promises that.

Widget layout (top → bottom):

- :class:`textual.widgets.Header` — status band with the app title.
- ``#status`` (:class:`RunStatusBar`) — one-line run stats.
- ``#body`` — horizontal split of ``#trials`` (:class:`TrialListView`)
  and ``#focused`` (:class:`FocusedTrialView`).
- ``#tabs`` (:class:`textual.widgets.TabbedContent`) — Overview / Logs /
  Services / Infra / Errors.
- :class:`textual.widgets.Footer` — key bindings.

Log records are captured through a :class:`_LogSink` installed for the
lifetime of the app; the Logs and Errors tabs read from the same ring
buffer the Rich panel uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.driver import Driver
from textual.drivers.linux_driver import LinuxDriver
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from tolokaforge.core.logging import _TOLOKAFORGE_ROOT_HANDLER_SENTINEL
from tolokaforge.core.run_display_events import (
    ContainerSnapshot,
    LLMCallRole,
    RunDisplayEvents,
    ServiceSnapshot,
)
from tolokaforge.dx.live_panel import (
    _LOG_BUFFER_MAX,
    _clear_llm_call_state,
    _cost_bar_style,
    _derive_hint,
    _format_call_state_line,
    _format_cost,
    _format_eta,
    _format_tokens,
    _health_glyph,
    _LogSink,
    _looks_like_auth_error,
    _now,
    _summarise_ports,
    _TrialCard,
    _truncate_error,
)

_LIVE_CALLS_MAX_ROWS = 10
"""Row cap for the Overview tab's "Live calls" section. Under high
concurrency (hundreds of parallel trials with retries), an uncapped list
would push the phase / services / trial-counter lines off the Overview
tab. Rows beyond the cap collapse into a `…and N more` tail so the
section stays scannable at a glance."""

_LOGGER = logging.getLogger("tolokaforge.dx.tui")


@contextlib.contextmanager
def _tolerate_signal_thread_errors():
    """Scope-limited monkey-patch of ``signal.signal`` used by Textual's
    ``LinuxDriver``.

    Textual's driver installs OS signal handlers (``SIGTSTP`` / ``SIGCONT``
    / ``SIGWINCH``) at boot. Python raises ``ValueError("signal only works
    in main thread ...")`` when those calls run off the main thread — and
    :class:`TextualRunApp` runs the driver on a daemon thread. Inside this
    context manager the offending ``ValueError`` is swallowed and any other
    ``ValueError`` is re-raised. The patch targets
    ``textual.drivers.linux_driver.signal.signal`` only and is restored on
    exit. See tolokaforge issue #470.
    """
    import textual.drivers.linux_driver as _driver_mod

    original = _driver_mod.signal.signal

    def _tolerant_signal(signum, handler):
        try:
            return original(signum, handler)
        except ValueError as exc:
            if "main thread" in str(exc):
                return None
            raise

    _driver_mod.signal.signal = _tolerant_signal
    try:
        yield
    finally:
        _driver_mod.signal.signal = original


class _SignalTolerantLinuxDriver(LinuxDriver):
    """``LinuxDriver`` that no-ops off-main-thread signal-handler installs.

    Textual's ``LinuxDriver`` installs ``SIGTSTP`` / ``SIGCONT`` in
    ``__init__`` and ``SIGWINCH`` in ``start_application_mode``; Python
    restricts signal-handler installation to the main thread, so those
    calls crash the whole app when the driver runs on
    :class:`TextualRunApp`'s daemon thread. Each entry point that installs
    a signal is wrapped in :func:`_tolerate_signal_thread_errors` so the
    handler install becomes a no-op instead. Ctrl-Z suspend and mid-run
    terminal-resize reflow are the accepted losses. See tolokaforge issue
    #470.
    """

    def __init__(self, *args, **kwargs):
        with _tolerate_signal_thread_errors():
            super().__init__(*args, **kwargs)

    def start_application_mode(self):
        with _tolerate_signal_thread_errors():
            super().start_application_mode()

    def stop(self):
        with _tolerate_signal_thread_errors():
            super().stop()


_TAB_IDS: tuple[str, ...] = ("overview", "logs", "services", "infra", "errors")


def _format_trial_row(card: _TrialCard, index_width: int, total: int) -> str:
    """Render a single trial-list entry: ``⏳ [17/500] task_c · 0``."""
    glyphs = {"running": "⏳", "completed": "✓", "failed": "✗"}
    glyph = glyphs.get(card.status, "•")
    human_index = card.total_index + 1
    prefix = f"[{human_index:>{index_width}}/{total or '?'}]"
    return f"{glyph} {prefix} {card.task_id} · {card.trial_index}"


class RunStatusBar(Static):
    """One-line summary above the body: run id, progress, cost, fails, ETA."""

    DEFAULT_CSS = """
    RunStatusBar {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    """


class TrialListView(ListView):
    """Left-pane trial list. Adds ``j``/``k`` bindings on top of ``ListView``."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("pagedown", "page_down", "PgDn", show=False),
        Binding("pageup", "page_up", "PgUp", show=False),
        Binding("home", "cursor_top", "Top", show=False),
        Binding("end", "cursor_bottom", "Bottom", show=False),
    ]

    def action_page_down(self) -> None:
        for _ in range(20):
            self.action_cursor_down()

    def action_page_up(self) -> None:
        for _ in range(20):
            self.action_cursor_up()

    def action_cursor_top(self) -> None:
        if len(self.children) == 0:
            return
        self.index = 0

    def action_cursor_bottom(self) -> None:
        if len(self.children) == 0:
            return
        self.index = len(self.children) - 1


class FocusedTrialView(Static):
    """Right-pane summary of the currently focused trial."""

    DEFAULT_CSS = """
    FocusedTrialView {
        padding: 1;
    }
    """


class HelpScreen(ModalScreen[None]):
    """Modal help overlay listing the app's keybindings."""

    BINDINGS = [Binding("escape,question_mark,?,q", "dismiss", "Close", show=False)]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > Vertical {
        width: 60;
        max-height: 80%;
        border: thick $primary 80%;
        padding: 1 2;
        background: $surface;
    }
    """

    _LINES: tuple[tuple[str, str], ...] = (
        ("j / ↓", "next trial"),
        ("k / ↑", "previous trial"),
        ("PgDn / PgUp", "jump ~20 rows"),
        ("Home / End", "first / last trial"),
        ("1 – 5", "switch tab (Overview…Errors)"),
        ("l", "focus Logs tab"),
        ("/", "search current tab"),
        ("?", "toggle this help"),
        ("q", "quit (Ctrl-C still kills the run)"),
    )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Keybindings[/b]", markup=True)
            yield Label("")
            for key, desc in self._LINES:
                yield Label(f"[b]{key:<12}[/b] {desc}", markup=True)
            yield Label("")
            yield Label("[dim]Press Esc or ? to close[/dim]", markup=True)

    def action_dismiss(self, result: None = None) -> None:  # type: ignore[override]
        self.app.pop_screen()


class TextualRunApp(App[None]):
    """Full TUI consumer of :class:`RunDisplayEvents`.

    Runs on Textual's asyncio loop inside a background thread; event
    methods called by worker threads route through
    :meth:`App.call_from_thread` when the loop is active and buffer into
    :attr:`_pending` before mount.
    """

    def get_driver_class(self) -> type[Driver]:
        """Force our signal-tolerant driver on Linux + macOS.

        Textual's :meth:`App.__init__` calls
        ``self.driver_class = driver_class or self.get_driver_class()`` which
        overwrites a class-level ``driver_class = …`` attribute with the
        return of this method (line 637 in textual/app.py). So the correct
        override point is the method, not the attribute. Windows falls
        back to ``super().get_driver_class()`` (its driver doesn't have
        the SIGTSTP / SIGCONT / SIGWINCH problem). See #470.
        """
        import sys as _sys

        if _sys.platform == "win32":
            return super().get_driver_class()
        return _SignalTolerantLinuxDriver

    CSS = """
    #status { dock: top; height: 1; }
    #body { height: 1fr; }
    #trials {
        width: 40%;
        border-right: solid $panel;
    }
    #focused {
        width: 1fr;
    }
    #tabs {
        height: 40%;
    }
    RichLog {
        background: $surface;
    }
    """

    TITLE = "tolokaforge run"

    BINDINGS = [
        Binding("1", "show_tab('overview')", "Overview"),
        Binding("2", "show_tab('logs')", "Logs"),
        Binding("3", "show_tab('services')", "Services"),
        Binding("4", "show_tab('infra')", "Infra"),
        Binding("5", "show_tab('errors')", "Errors"),
        Binding("l", "show_tab('logs')", "Logs", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("?", "help", "Help", show=False),
        Binding("slash", "noop", "Search", show=False),
        Binding("q", "request_quit", "Quit"),
    ]

    def __init__(self, *, cost_budget_usd: float | None = None) -> None:
        super().__init__()
        self._lock: threading.RLock = threading.RLock()
        # State machine — mirrored across the trial list, focused pane,
        # infra tab, and status bar. Event handlers mutate this under
        # the lock; render helpers snapshot under the lock and paint on
        # the UI thread.
        self._trials: dict[str, _TrialCard] = {}
        self._trial_order: list[str] = []
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
        self._cost_budget_usd: float | None = cost_budget_usd
        self._current_phase: str | None = None
        self._current_phase_detail: str | None = None
        self._services: list[ServiceSnapshot] = []
        self._banner: tuple[str, str, str | None] | None = None
        self._run_id: str | None = None
        self._output_dir: Path | None = None
        # Log capture — populated by :class:`_LogSink` installed on
        # ``__enter__``. The Logs / Errors tabs read from this deque.
        self._log_buffer: deque[logging.LogRecord] = deque(maxlen=_LOG_BUFFER_MAX)
        self._replaced_log_handlers: list[tuple[logging.Handler, _LogSink]] = []
        # Events fired before ``on_mount`` land here; drained once
        # widgets are ready. Guarded by :attr:`_lock`.
        self._pending: list[tuple[str, dict]] = []
        # Records already routed to the Logs tab — indexes into the
        # ring buffer we've already rendered. Recomputed on refresh.
        self._log_records_rendered: int = 0
        self._error_records_rendered: int = 0
        # Threading — production runs the app in a background thread so
        # ``__enter__`` returns immediately. Tests use ``run_test`` and
        # never touch these attrs.
        self._thread: threading.Thread | None = None
        self._mounted_event: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # RunDisplayEvents implementation
    # ------------------------------------------------------------------

    @property
    def events(self) -> RunDisplayEvents:
        return self

    def log_records(self) -> tuple[logging.LogRecord, ...]:
        """Snapshot the panel-scoped log ring buffer."""
        with self._lock:
            return tuple(self._log_buffer)

    def run_started(self, *, total_trials: int, initial_completed: int) -> None:
        self._safe_dispatch(
            "run_started",
            {"total_trials": total_trials, "initial_completed": initial_completed},
        )

    def trial_started(
        self,
        *,
        trial_id: str,
        task_id: str,
        trial_index: int,
        total_index: int,
        agent_model: str | None = None,
        user_model: str | None = None,
    ) -> None:
        self._safe_dispatch(
            "trial_started",
            {
                "trial_id": trial_id,
                "task_id": task_id,
                "trial_index": trial_index,
                "total_index": total_index,
                "agent_model": agent_model,
                "user_model": user_model,
            },
        )

    def trial_progress(
        self,
        *,
        trial_id: str,
        prompt_tokens_delta: int,
        completion_tokens_delta: int,
        cost_delta_usd: float,
    ) -> None:
        self._safe_dispatch(
            "trial_progress",
            {
                "trial_id": trial_id,
                "prompt_tokens_delta": prompt_tokens_delta,
                "completion_tokens_delta": completion_tokens_delta,
                "cost_delta_usd": cost_delta_usd,
            },
        )

    def trial_provisioned(
        self,
        *,
        trial_id: str,
        containers: list[ContainerSnapshot],
        endpoints: dict[str, str],
    ) -> None:
        self._safe_dispatch(
            "trial_provisioned",
            {
                "trial_id": trial_id,
                "containers": list(containers),
                "endpoints": dict(endpoints),
            },
        )

    def trial_completed(self, *, trial_id: str, binary_pass: bool, score: float | None) -> None:
        self._safe_dispatch(
            "trial_completed",
            {"trial_id": trial_id, "binary_pass": binary_pass, "score": score},
        )

    def trial_failed(self, *, trial_id: str, error: str, retryable: bool) -> None:
        self._safe_dispatch(
            "trial_failed",
            {"trial_id": trial_id, "error": error, "retryable": retryable},
        )

    def judgment_scored(self, *, trial_id: str, score: float, binary_pass: bool) -> None:
        self._safe_dispatch(
            "judgment_scored",
            {"trial_id": trial_id, "score": score, "binary_pass": binary_pass},
        )

    def run_finished(self, *, output_dir: Path) -> None:
        self._safe_dispatch("run_finished", {"output_dir": output_dir})

    def phase_changed(
        self,
        *,
        phase: str,
        detail: str | None = None,
        services: list[ServiceSnapshot] | None = None,
    ) -> None:
        payload: dict = {"phase": phase, "detail": detail}
        if services is not None:
            payload["services"] = list(services)
        self._safe_dispatch("phase_changed", payload)

    def llm_call_started(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,
        provider: str,
        model: str,
        attempt: int,
    ) -> None:
        self._safe_dispatch(
            "llm_call_started",
            {
                "trial_id": trial_id,
                "role": role,
                "provider": provider,
                "model": model,
                "attempt": attempt,
            },
        )

    def llm_call_finished(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,
        provider: str,
        model: str,
        attempt: int,
        duration_s: float,
        error: str | None,
    ) -> None:
        self._safe_dispatch(
            "llm_call_finished",
            {
                "trial_id": trial_id,
                "role": role,
                "provider": provider,
                "model": model,
                "attempt": attempt,
                "duration_s": duration_s,
                "error": error,
            },
        )

    def llm_retry_scheduled(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,
        provider: str,
        model: str,
        attempt: int,
        next_attempt_in_s: float,
        reason: str,
    ) -> None:
        self._safe_dispatch(
            "llm_retry_scheduled",
            {
                "trial_id": trial_id,
                "role": role,
                "provider": provider,
                "model": model,
                "attempt": attempt,
                "next_attempt_in_s": next_attempt_in_s,
                "reason": reason,
            },
        )

    # ------------------------------------------------------------------
    # Dispatch — route worker-thread events onto the Textual loop
    # ------------------------------------------------------------------

    def _safe_dispatch(self, kind: str, payload: dict) -> None:
        """Route an event to :meth:`_apply_event` on the UI thread.

        Contract of :class:`RunDisplayEvents`: handlers must not raise.
        Every branch here swallows exceptions after logging at WARNING —
        a bad event never corrupts the runner loop.
        """
        try:
            if not self._is_on_ui_thread() and self.is_running:
                self.call_from_thread(self._apply_event, kind, payload)
                return
            if self.is_running:
                self._apply_event(kind, payload)
                return
            with self._lock:
                self._pending.append((kind, payload))
        except Exception:  # noqa: BLE001 — display must never propagate
            _LOGGER.warning("TextualRunApp dispatch failed for %s", kind, exc_info=True)

    @staticmethod
    def _is_on_ui_thread() -> bool:
        """True when we're already running inside an asyncio event loop.

        Textual is asyncio-driven. When the caller is already on the
        loop (test code driving via :meth:`App.run_test`, or an internal
        callback), :meth:`App.call_from_thread` would raise — so we call
        the handler directly instead.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def _apply_event(self, kind: str, payload: dict) -> None:
        try:
            self._apply_locked(kind, payload)
        except Exception:  # noqa: BLE001 — same swallow discipline
            _LOGGER.warning("TextualRunApp apply failed for %s", kind, exc_info=True)
            return
        self._refresh_ui()

    def _apply_locked(self, kind: str, payload: dict) -> None:
        with self._lock:
            handler = getattr(self, f"_state_{kind}")
            handler(**payload)

    # ------------------------------------------------------------------
    # State-mutation handlers (called under :attr:`_lock`)
    # ------------------------------------------------------------------

    def _state_run_started(self, *, total_trials: int, initial_completed: int) -> None:
        self._total_trials = total_trials
        self._initial_completed = initial_completed
        self._completed = initial_completed
        self._run_start_ts = _now()
        self._current_phase = None
        self._current_phase_detail = None

    def _state_phase_changed(
        self,
        *,
        phase: str,
        detail: str | None = None,
        services: list[ServiceSnapshot] | None = None,
    ) -> None:
        self._current_phase = phase
        self._current_phase_detail = detail
        if services is not None:
            self._services = list(services)

    def _state_trial_started(
        self,
        *,
        trial_id: str,
        task_id: str,
        trial_index: int,
        total_index: int,
        agent_model: str | None = None,
        user_model: str | None = None,
    ) -> None:
        card = _TrialCard(
            trial_id=trial_id,
            task_id=task_id,
            trial_index=trial_index,
            total_index=total_index,
            status="running",
            last_update_ts=_now(),
            last_event_kind="started",
            agent_model=agent_model,
            user_model=user_model,
        )
        if trial_id not in self._trials:
            self._trial_order.append(trial_id)
        self._trials[trial_id] = card
        self._running += 1
        self._focused_trial_id = trial_id

    def _state_trial_progress(
        self,
        *,
        trial_id: str,
        prompt_tokens_delta: int,
        completion_tokens_delta: int,
        cost_delta_usd: float,
    ) -> None:
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        card.prompt_tokens += prompt_tokens_delta
        card.completion_tokens += completion_tokens_delta
        card.cost_usd += cost_delta_usd
        card.turn_count += 1
        card.last_event_kind = "progress"
        self._prompt_tokens += prompt_tokens_delta
        self._completion_tokens += completion_tokens_delta
        self._total_cost_usd += cost_delta_usd

    def _state_trial_provisioned(
        self,
        *,
        trial_id: str,
        containers: list[ContainerSnapshot],
        endpoints: dict[str, str],  # noqa: ARG002 — kept on payload for parity
    ) -> None:
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        card.containers = list(containers)

    def _state_trial_completed(
        self, *, trial_id: str, binary_pass: bool, score: float | None
    ) -> None:
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        was_running = card.status == "running"
        card.status = "completed"
        card.binary_pass = binary_pass
        card.score = score
        card.last_event_kind = "completed"
        card.last_update_ts = _now()
        _clear_llm_call_state(card)
        if was_running and self._running > 0:
            self._running -= 1
        self._completed += 1
        self._focused_trial_id = trial_id

    def _state_trial_failed(self, *, trial_id: str, error: str, retryable: bool) -> None:
        del retryable  # panel doesn't currently branch on this
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        was_running = card.status == "running"
        card.status = "failed"
        card.error = error
        card.last_event_kind = "failed"
        card.last_update_ts = _now()
        _clear_llm_call_state(card)
        if was_running and self._running > 0:
            self._running -= 1
        self._failed += 1
        self._focused_trial_id = trial_id
        if self._banner is None and _looks_like_auth_error(error):
            hint = _derive_hint(error) or "Verify the provider API key"
            self._banner = ("Auth failure", _truncate_error(error, width=120), hint)

    def _state_llm_call_started(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,
        provider: str,
        model: str,
        attempt: int,  # noqa: ARG002 — reserved for future retry-attempt labelling
    ) -> None:
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        card.llm_role = role
        card.llm_provider_model = f"{provider}/{model}"
        card.llm_call_start_ts = _now()
        card.llm_retry_state = None

    def _state_llm_call_finished(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,  # noqa: ARG002 — carried on the seam; the card holds the last-started role
        provider: str,  # noqa: ARG002
        model: str,  # noqa: ARG002
        attempt: int,  # noqa: ARG002
        duration_s: float,  # noqa: ARG002
        error: str | None,  # noqa: ARG002 — terminal failures surface via ``trial_failed``
    ) -> None:
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        _clear_llm_call_state(card)

    def _state_llm_retry_scheduled(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,  # noqa: ARG002
        provider: str,  # noqa: ARG002
        model: str,  # noqa: ARG002
        attempt: int,
        next_attempt_in_s: float,
        reason: str,
    ) -> None:
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        card.llm_retry_state = (attempt, next_attempt_in_s, reason)

    def _state_judgment_scored(self, *, trial_id: str, score: float, binary_pass: bool) -> None:
        card = self._trials.get(trial_id) or self._lazy_card(trial_id)
        card.score = score
        card.binary_pass = binary_pass
        card.last_event_kind = "judged"
        card.last_update_ts = _now()
        self._focused_trial_id = trial_id

    def _state_run_finished(self, *, output_dir: Path) -> None:
        self._finished = True
        self._output_dir = output_dir

    def _lazy_card(self, trial_id: str) -> _TrialCard:
        task_id, _, idx = trial_id.partition(":")
        try:
            trial_index = int(idx) if idx else 0
        except ValueError:
            trial_index = 0
        card = _TrialCard(
            trial_id=trial_id,
            task_id=task_id or trial_id,
            trial_index=trial_index,
            total_index=0,
            status="running",
            last_update_ts=_now(),
            last_event_kind="started",
        )
        self._trials[trial_id] = card
        self._trial_order.append(trial_id)
        return card

    def _estimate_eta_seconds(self) -> float | None:
        if self._run_start_ts is None:
            return None
        completed_this_run = self._completed - self._initial_completed
        remaining = self._total_trials - self._completed
        if completed_this_run <= 0 or remaining <= 0:
            return None
        elapsed = (_now() - self._run_start_ts).total_seconds()
        return elapsed / completed_this_run * remaining

    # ------------------------------------------------------------------
    # Textual lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield RunStatusBar("", id="status")
        with Horizontal(id="body"):
            yield TrialListView(id="trials")
            yield FocusedTrialView("(waiting for first trial)", id="focused", markup=True)
        with TabbedContent(id="tabs", initial="overview"):
            with TabPane("Overview", id="overview"):
                yield Static("(waiting for run)", id="overview-body", markup=True)
            with TabPane("Logs", id="logs"):
                yield RichLog(id="logs-body", markup=True, wrap=True, auto_scroll=True)
            with TabPane("Services", id="services"):
                yield DataTable(id="services-body")
            with TabPane("Infra", id="infra"):
                yield DataTable(id="infra-body")
            with TabPane("Errors", id="errors"):
                yield RichLog(id="errors-body", markup=True, wrap=True, auto_scroll=True)
        yield Footer()

    async def on_mount(self) -> None:
        services_table = self.query_one("#services-body", DataTable)
        services_table.add_columns("service", "status", "", "ports")
        services_table.cursor_type = "row"
        infra_table = self.query_one("#infra-body", DataTable)
        infra_table.add_columns("container", "", "ports")
        infra_table.cursor_type = "row"
        # Drain events that arrived before mount.
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        for kind, payload in pending:
            self._apply_event(kind, payload)
        self._refresh_ui()
        self._mounted_event.set()

    # ------------------------------------------------------------------
    # Rendering — snapshot state under lock, paint on UI thread
    # ------------------------------------------------------------------

    def _refresh_ui(self) -> None:
        if not self.is_running or not self._mounted_event.is_set():
            return
        self._refresh_status_bar()
        self._refresh_trial_list()
        self._refresh_focused_pane()
        self._refresh_overview_tab()
        self._refresh_services_tab()
        self._refresh_infra_tab()
        self._refresh_logs()

    def _refresh_status_bar(self) -> None:
        with self._lock:
            completed = self._completed
            total = self._total_trials
            running = self._running
            cost = self._total_cost_usd
            failed = self._failed
            eta = self._estimate_eta_seconds()
            cost_style = _cost_bar_style(cost, self._cost_budget_usd)
        cost_segment = _format_cost(cost)
        if cost_style == "warn":
            cost_segment = f"[yellow]{cost_segment}[/yellow]"
        elif cost_style == "error":
            cost_segment = f"[bold red]{cost_segment}[/bold red]"
        line = (
            f"{completed}/{total or '?'} · {running} running · "
            f"{cost_segment} · fail {failed} · eta {_format_eta(eta)}"
        )
        try:
            status = self.query_one("#status", RunStatusBar)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            return
        status.update(Text.from_markup(line))

    def _visible_cards(self) -> list[_TrialCard]:
        with self._lock:
            return [self._trials[tid] for tid in self._trial_order if tid in self._trials]

    def _refresh_trial_list(self) -> None:
        cards = self._visible_cards()
        with self._lock:
            total = self._total_trials
            focused = self._focused_trial_id
        try:
            view = self.query_one("#trials", TrialListView)
        except Exception:  # noqa: BLE001
            return
        index_width = max(len(str(total)), 1) if total else 1
        existing = [item for item in view.children if isinstance(item, ListItem)]
        for item in existing:
            item.remove()
        for card in cards:
            view.append(
                ListItem(Label(_format_trial_row(card, index_width, total)), name=card.trial_id)
            )
        if focused is not None:
            for idx, card in enumerate(cards):
                if card.trial_id == focused:
                    view.index = idx
                    break

    def _refresh_focused_pane(self) -> None:
        with self._lock:
            trial_id = self._focused_trial_id
            card = self._trials.get(trial_id) if trial_id is not None else None
            snapshot = (
                None
                if card is None
                else (
                    card.turn_count,
                    card.prompt_tokens,
                    card.completion_tokens,
                    card.cost_usd,
                    card.status,
                    card.error,
                    card.task_id,
                    card.trial_index,
                    card.score,
                    card.binary_pass,
                    list(card.containers) if card.containers is not None else None,
                    card.agent_model,
                    card.llm_role,
                    card.llm_provider_model,
                    card.llm_call_start_ts,
                    card.llm_retry_state,
                )
            )
        try:
            pane = self.query_one("#focused", FocusedTrialView)
        except Exception:  # noqa: BLE001
            return
        if snapshot is None:
            pane.update(Text("(waiting for first trial)"))
            return
        (
            turn,
            prompt,
            completion,
            cost,
            status,
            error,
            task_id,
            trial_index,
            score,
            binary_pass,
            containers,
            agent_model,
            llm_role,
            llm_provider_model,
            llm_call_start_ts,
            llm_retry_state,
        ) = snapshot
        lines: list[str] = []
        header = f"[b]{task_id} · {trial_index}[/b]  ({status})"
        lines.append(header)
        if agent_model is not None:
            lines.append(f"model: {agent_model}")
        lines.append(
            f"turn {turn} · in {_format_tokens(prompt)} / out {_format_tokens(completion)} tok · "
            f"{_format_cost(cost)}"
        )
        call_line = _format_call_state_line(
            llm_role=llm_role,
            llm_provider_model=llm_provider_model,
            llm_call_start_ts=llm_call_start_ts,
            llm_retry_state=llm_retry_state,
        )
        if call_line is not None:
            lines.append(call_line)
        if binary_pass is not None or score is not None:
            score_txt = "n/a" if score is None else f"{score:.2f}"
            pass_txt = "n/a" if binary_pass is None else ("pass" if binary_pass else "fail")
            lines.append(f"score {score_txt} · {pass_txt}")
        if status == "failed" and error:
            lines.append("")
            lines.append(f"[bold red]{_truncate_error(error, width=200)}[/bold red]")
            hint = _derive_hint(error)
            if hint:
                lines.append(f"[yellow]Hint:[/yellow] {hint}")
        if containers:
            lines.append("")
            lines.append("[b]Infrastructure[/b]")
            for c in containers:
                health = c.get("health") or c.get("state", "unknown")
                ports = _summarise_ports(c.get("ports", {}))
                port_txt = f" · {ports}" if ports else ""
                lines.append(f"{_health_glyph(health)} {c.get('name', '')}{port_txt}")
        pane.update(Text.from_markup("\n".join(lines)))

    def _refresh_overview_tab(self) -> None:
        with self._lock:
            banner = self._banner
            phase = self._current_phase
            detail = self._current_phase_detail
            services = list(self._services)
            total_trials = self._total_trials
            completed = self._completed
            failed = self._failed
            live_calls = self._snapshot_live_calls_locked()
        try:
            body = self.query_one("#overview-body", Static)
        except Exception:  # noqa: BLE001
            return
        parts: list[str] = []
        if banner is not None:
            title, message, hint = banner
            parts.append(f"[bold red]✗ {title}[/bold red]: {message}")
            if hint:
                parts.append(f"[yellow]Hint:[/yellow] {hint}")
            parts.append("")
        if phase is not None:
            phase_line = phase.replace("_", " ").capitalize()
            if detail:
                phase_line += f" — {detail}"
            parts.append(f"[b]Phase:[/b] {phase_line}")
        if services:
            healthy = sum(1 for s in services if s.get("status") == "healthy")
            parts.append(f"[b]Services:[/b] {healthy}/{len(services)} healthy")
        parts.append(
            f"[b]Trials:[/b] {completed}/{total_trials or '?'} completed · {failed} failed"
        )
        visible, total = live_calls
        if visible:
            parts.append("")
            parts.append("[b]Live calls[/b]")
            for line in visible:
                parts.append(line)
            hidden = total - len(visible)
            if hidden > 0:
                parts.append(f"[dim]…and {hidden} more[/dim]")
        if not parts:
            parts.append("(waiting for run)")
        body.update(Text.from_markup("\n".join(parts)))

    def _snapshot_live_calls_locked(self) -> tuple[list[str], int]:
        """Under-lock render of the Overview "Live calls" list.

        One line per running trial whose most recent LLM event set
        ``llm_role``. Preserves ``_trial_order`` — older starts render
        first so a caller scanning top-to-bottom sees the longest-waiting
        attempts. Returns ``(visible, total)`` where ``visible`` is capped
        at ``_LIVE_CALLS_MAX_ROWS`` and ``total`` counts every in-flight
        call (used to compute the "…and N more" tail).
        """
        lines: list[str] = []
        total = 0
        for trial_id in self._trial_order:
            card = self._trials.get(trial_id)
            if card is None or card.llm_role is None:
                continue
            call_line = _format_call_state_line(
                llm_role=card.llm_role,
                llm_provider_model=card.llm_provider_model,
                llm_call_start_ts=card.llm_call_start_ts,
                llm_retry_state=card.llm_retry_state,
            )
            if call_line is None:
                continue
            total += 1
            if len(lines) < _LIVE_CALLS_MAX_ROWS:
                lines.append(f"{card.task_id} · {card.trial_index}: {call_line}")
        return lines, total

    def _refresh_services_tab(self) -> None:
        with self._lock:
            services = list(self._services)
        try:
            table = self.query_one("#services-body", DataTable)
        except Exception:  # noqa: BLE001
            return
        table.clear()
        for svc in services:
            table.add_row(
                svc.get("name", ""),
                svc.get("status", ""),
                _health_glyph(svc.get("status", "unknown")),
                _summarise_ports(svc.get("ports", {})),
            )

    def _refresh_infra_tab(self) -> None:
        with self._lock:
            focused = self._focused_trial_id
            card = self._trials.get(focused) if focused is not None else None
            containers = list(card.containers) if card and card.containers else []
        try:
            table = self.query_one("#infra-body", DataTable)
        except Exception:  # noqa: BLE001
            return
        table.clear()
        for c in containers:
            health = c.get("health") or c.get("state", "unknown")
            table.add_row(
                c.get("name", ""),
                _health_glyph(health),
                _summarise_ports(c.get("ports", {})),
            )

    def _refresh_logs(self) -> None:
        with self._lock:
            records = list(self._log_buffer)
            already_logs = self._log_records_rendered
            already_errors = self._error_records_rendered
        new_records = records[already_logs:]
        new_errors = [r for r in records[already_errors:] if r.levelno >= logging.WARNING]
        if new_records:
            try:
                logs_widget = self.query_one("#logs-body", RichLog)
                for record in new_records:
                    logs_widget.write(self._format_log_line(record))
            except Exception:  # noqa: BLE001
                pass
        if new_errors:
            try:
                errors_widget = self.query_one("#errors-body", RichLog)
                for record in new_errors:
                    errors_widget.write(self._format_log_line(record))
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._log_records_rendered = len(records)
            self._error_records_rendered = len(records)

    @staticmethod
    def _format_log_line(record: logging.LogRecord) -> str:
        level = record.levelname
        message = record.getMessage()
        return f"[dim]{level:<8}[/dim] {message}"

    # ------------------------------------------------------------------
    # Actions — key bindings dispatch here
    # ------------------------------------------------------------------

    def action_show_tab(self, tab_id: str) -> None:
        if tab_id not in _TAB_IDS:
            return
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = tab_id

    def action_help(self) -> None:
        # Toggle — if already showing, pop; else push.
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
            return
        self.push_screen(HelpScreen())

    def action_request_quit(self) -> None:
        # Confirmation is out of scope for stage 2 — the plan explicitly
        # notes killing the run needs Ctrl-C. ``q`` exits the UI only;
        # the orchestrator thread is unaffected.
        self.exit()

    def action_noop(self) -> None:
        """Placeholder for the ``/`` search binding.

        A per-tab search feature is deferred; the binding is registered
        so the footer surfaces the key and the help modal describes it.
        """
        return

    # ------------------------------------------------------------------
    # Context-manager protocol — matches ``LiveRunDisplay.for_mode``
    # ------------------------------------------------------------------

    def __enter__(self) -> TextualRunApp:
        self._install_log_sink()
        # Start the app on a dedicated daemon thread — Textual owns the
        # terminal until ``exit`` is called. ``__enter__`` returns once
        # the app has mounted, so ``_pending`` events (which the CLI
        # emits before the orchestrator starts trials) are drained
        # deterministically before the caller enters its run loop.
        self._thread = threading.Thread(target=self._run_app, name="TextualRunApp", daemon=True)
        self._thread.start()
        if not self._mounted_event.wait(timeout=10.0):
            _LOGGER.warning("TextualRunApp did not mount within 10s; continuing anyway")
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.is_running:
            try:
                self.call_from_thread(self.exit)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._restore_log_sink()

    def _run_app(self) -> None:
        try:
            self.run()
        except Exception:  # noqa: BLE001 — TUI failure must not corrupt the run
            _LOGGER.warning("TextualRunApp thread crashed", exc_info=True)

    def _install_log_sink(self) -> None:
        # Textual takes over the terminal via its own screen; log records
        # never need to reach stderr while the TUI is active — the Logs /
        # Errors tabs read the buffered records directly. `print_above` is
        # a no-op so WARNING+ records still populate the buffer without
        # scrolling anything out under the app.
        root = logging.getLogger()
        for handler in list(root.handlers):
            if not getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
                continue
            sink = _LogSink(
                print_above=lambda _line: None,
                formatter=handler.formatter,
                buffer=self._log_buffer,
            )
            sink.setLevel(handler.level)
            setattr(sink, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
            root.removeHandler(handler)
            root.addHandler(sink)
            self._replaced_log_handlers.append((handler, sink))

    def _restore_log_sink(self) -> None:
        root = logging.getLogger()
        for original, sink in self._replaced_log_handlers:
            root.removeHandler(sink)
            root.addHandler(original)
        self._replaced_log_handlers = []


__all__ = [
    "FocusedTrialView",
    "HelpScreen",
    "RunStatusBar",
    "TextualRunApp",
    "TrialListView",
]
