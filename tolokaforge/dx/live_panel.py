"""Rich ``Live`` progress panel for ``tolokaforge run`` under ``--display=rich``.

Under :attr:`DisplayMode.RICH`, :class:`LiveRunDisplay` renders a
three-region panel: left-pane trial list, right-pane structured summary
of the focused trial (turn count / tokens / cost / last-event kind),
bottom status bar with cost / tokens / ETA / failure counts.

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
import os
import select
import sys
import termios
import threading
import traceback
import tty
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape as _escape_markup
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from tolokaforge.core.logging import _TOLOKAFORGE_ROOT_HANDLER_SENTINEL
from tolokaforge.core.logging_context import TRIAL_ID_CTXVAR
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    ComponentPhase,
    ComponentSnapshot,
    ContainerSnapshot,
    LLMCallRole,
    RunDisplayEvents,
    ServiceSnapshot,
    build_component_id,
)
from tolokaforge.dx._display import DisplayMode, format_duration, make_live

_LOGGER = logging.getLogger("tolokaforge.dx.live_panel")

_LOG_BUFFER_MAX = 500
"""Ring-buffer size for INFO/DEBUG records held inside the panel. The
buffer is scoped to one run — ~500 records covers the busiest observed
Docker-boot window with a few hundred records of headroom for
in-trial output — and never grows without bound thanks to
``deque(maxlen=…)``."""

_BOOT_LOG_MAX_LINES = 5
"""Content-row cap for the boot-log region. Feeds both
:func:`_render_boot_log_tail`'s default ``max_lines`` and the desired
height computed in :meth:`LiveRunDisplay._build_layout` so the two
never drift out of sync."""

_COMPONENT_TAIL_MAX_LINES = 5
"""Per-component log-tail cap. Rendered under the row of any component in
a non-healthy phase; capped at this many most-recent lines to keep the
Components widget compact when a component is failing."""

_COMPONENT_TAIL_BUFFER_MAX = 32
"""Per-component ring-buffer size for ``component_log_appended`` records.
Larger than the render cap so a component that recovers can be
inspected in retrospect by future tooling; the panel itself only ever
shows the last :data:`_COMPONENT_TAIL_MAX_LINES`."""

_COMPONENT_PHASE_ICONS: dict[str, str] = {
    "pending": "…",
    "starting": "⏳",
    "healthy": "✓",
    "degraded": "⚠",
    "unhealthy": "✗",
    "stopped": "·",
    "dead": "☠",
}
"""Icon per :data:`ComponentPhase`. Unknown phases fall back to ``"?"``
via :meth:`dict.get` so the widget never crashes on a
future-value snapshot."""

_COMPONENT_PHASE_STYLES: dict[str, str] = {
    "pending": "muted",
    "starting": "warn",
    "healthy": "ok",
    "degraded": "warn",
    "unhealthy": "error",
    "stopped": "muted",
    "dead": "error",
}
"""Rich theme token per phase. Themes live in :mod:`tolokaforge.dx._display`."""

_UNHEALTHY_PHASES = frozenset({"degraded", "unhealthy", "dead"})
"""Phases that decisively indicate a component is in trouble. Used by
:meth:`LiveRunDisplay._enter_interactive_mode` and by the layout's
mid-run resurfacing rule for the Components widget."""

_STABLE_PHASES = frozenset({"healthy", "stopped"})
"""Phases where the component is in a steady state. When the phase is
here AND no failure log entries are attached, the tail stays collapsed;
outside of it (``pending`` / ``starting`` / ``degraded`` / ``unhealthy``
/ ``dead``) any tail entries expand beneath the row. This is what makes
a retry-in-progress component surface its recent errors without waiting
for the outer retry to give up."""


def _now() -> datetime:
    """Wall-clock accessor used for all timestamp assignment inside the display.

    Extracted so tests can monkey-patch it to a deterministic factory when
    they need a strictly-ordered sequence of ``last_update_ts`` values."""
    return datetime.now()


class _LogSink(logging.Handler):
    """Root-logger handler installed for the lifetime of :class:`LiveRunDisplay`.

    Three routing paths, chosen per-record:

    1. **Component-tagged records** — ``record.component_id`` is set (via
       ``extra={"component_id": ...}`` at the emitter, or an explicit
       ``LogRecord.component_id`` assignment). Routed to the display's
       :meth:`component_log_appended` handler so the record lands in that
       component's bounded tail buffer, rendered beneath the component's
       row in the Components widget when the widget expands it. **Does
       not** append to the general ring, **does not** ``print_above`` —
       component chatter never scrolls over the panel regardless of level.
       This is the generic escape hatch: any subsystem that owns a
       monitored component can tag its records and get a compact
       visualisation for free, at the natural log level.
    2. **Global WARNING+ records** — ``print_above`` renders them above
       the Live panel via the Live-owned console. Kept for records that
       aren't tied to any component (root-level errors, unclassified
       failures).
    3. **Everything else** — appended to the general 500-entry ring
       (:attr:`buffer`) for the per-trial log-view widget and post-mortem
       introspection; not rendered above the panel.

    Historical note: an earlier version wrote WARNING+ directly to the
    stream captured before Rich patched ``sys.stderr``. That bypasses
    Live's coordination — Live decrements its cursor by its own last
    render height, so raw writes to stderr between refreshes cause the
    panel to re-append below the log line instead of overwriting in
    place (visible bug: stacks of duplicate panels). Routing WARNING+
    through the Live-owned console fixes it.
    """

    def __init__(
        self,
        *,
        print_above: Callable[[str], None],
        formatter: logging.Formatter | None,
        buffer: deque[logging.LogRecord],
        route_component_log: Callable[[str, str, str, float], None] | None = None,
    ) -> None:
        super().__init__()
        self._print_above = print_above
        if formatter is not None:
            self.setFormatter(formatter)
        self.buffer = buffer
        self._route_component_log = route_component_log

    def emit(self, record: logging.LogRecord) -> None:
        # Stamp the active trial identity so the per-trial log view can filter,
        # but never overwrite a ``trial_id`` an emitter set explicitly (e.g. the
        # Docker logging pipeline's ``extra={"trial_id": ...}``).
        if getattr(record, "trial_id", None) is None:
            ctx_trial_id = TRIAL_ID_CTXVAR.get()
            if ctx_trial_id is not None:
                record.trial_id = ctx_trial_id  # type: ignore[attr-defined]

        # Component-tagged records take a separate path: they populate
        # the component's tail buffer instead of the general ring, and
        # they NEVER print_above regardless of level — the whole point of
        # the tag is to keep the noise compacted under the component's
        # row rather than scrolling above the panel.
        component_id = getattr(record, "component_id", None)
        if component_id is not None and self._route_component_log is not None:
            try:
                self._route_component_log(
                    component_id,
                    record.levelname,
                    record.getMessage(),
                    record.created,
                )
            except Exception:  # noqa: BLE001 — handlers must never raise past logging
                self.handleError(record)
            return

        self.buffer.append(record)
        if record.levelno < logging.WARNING:
            return
        # Trial-scoped records surface via the focused pane's ``l`` per-trial
        # log-tail widget (which filters ``_log_buffer`` by ``record.trial_id``);
        # keep them out of the ``print_above`` channel so a chatty trial —
        # tool errors, retry warnings — can't destabilise the panel with
        # scroll. Records without a trial id (root-level failures, unclassified
        # noise) still print above at WARNING+ so real orchestrator-level
        # problems remain immediately visible.
        if getattr(record, "trial_id", None) is not None:
            return
        try:
            self._print_above(self.format(record))
        except Exception:  # noqa: BLE001 — handlers must never raise past logging
            self.handleError(record)


def _capture_dangerous_streams() -> tuple[TextIO, ...]:
    """Snapshot the four terminal streams whose ``StreamHandler`` binding
    bypasses Rich Live's cursor coordination.

    Must be called before ``Live.__enter__``: Rich installs a redirect proxy
    that re-binds the ``sys.stderr`` and ``sys.stdout`` *names* for the Live
    lifetime, but chatty libraries (e.g. litellm) captured the raw stream
    *objects* at import time. The sweep in
    :meth:`LiveRunDisplay._sweep_child_bypass_handlers` needs those raw
    references — comparing against the post-Live proxy would miss the leak.
    ``sys.__stderr__`` / ``sys.__stdout__`` are included for completeness so
    a handler bound to the interpreter's original streams is also caught.
    """
    return (sys.stderr, sys.stdout, sys.__stderr__, sys.__stdout__)


_STDERR_PROBE_ENV_VAR = "TOLOKAFORGE_STDERR_PROBE"
"""Env var pointing at a log file for :class:`_StderrProbe`.

Set to a file path to activate the diagnostic tap in
:meth:`LiveRunDisplay.__enter__`; unset (the default) means the probe is
never installed and there is zero production overhead.
"""


_INTERACTIVE_PANEL_ENV_VAR = "TOLOKAFORGE_INTERACTIVE_PANEL"
"""Escape hatch for the interactive keyboard listener.

Set to ``"0"`` to keep the panel in auto-follow-only mode even on a TTY —
for terminal-compat issues or operators who prefer the pre-listener
behaviour. Any other value (or unset) leaves the listener enabled when
the other two TTY / platform guards pass.
"""


class _StderrProbe:
    """Diagnostic tap on the underlying stderr stream's ``write`` method.

    Wraps :data:`sys.stderr` — the stream object resolved *before* Rich
    Live installs its redirect proxy — so every raw ``write(chunk)`` call
    is recorded to ``path`` with an ISO-8601 timestamp, the caller's
    ``file:line``, ``repr(chunk)[:200]``, and the top five stack frames.
    The wrapped stream's original ``write`` still receives every chunk,
    so terminal output is unaffected.

    Wrapping the stream **object** — not re-binding the ``sys.stderr``
    name — is load-bearing: libraries like litellm install
    ``StreamHandler(sys.stderr)`` at import time, capturing the stream
    object into their handler. A later re-bind of the ``sys.stderr``
    name (e.g. by Rich Live's redirect proxy) would miss the leak this
    probe exists to catch. Install this probe at the very top of
    :meth:`LiveRunDisplay.__enter__`, before ``self._live.__enter__()``,
    so ``sys.stderr`` still points at the process's real stream.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: Any = None
        self._original_write: Callable[[str], int] | None = None
        self._log_fh: TextIO | None = None

    def __enter__(self) -> _StderrProbe:
        self._log_fh = self._path.open("a", encoding="utf-8")
        stream = sys.stderr
        original_write = stream.write
        self._stream = stream
        self._original_write = original_write
        log_fh = self._log_fh

        def tap(chunk: str) -> int:
            frame = sys._getframe(1)
            caller_loc = f"{frame.f_code.co_filename}:{frame.f_lineno}"
            stack = traceback.extract_stack(frame, limit=5)
            stack_lines = "".join(
                f"    {frm.filename}:{frm.lineno} in {frm.name}\n" for frm in stack
            )
            timestamp = datetime.now(timezone.utc).isoformat()
            log_fh.write(f"{timestamp} | {caller_loc} | {repr(chunk)[:200]}\n{stack_lines}")
            log_fh.flush()
            return original_write(chunk)

        stream.write = tap  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._stream is not None and self._original_write is not None:
            self._stream.write = self._original_write  # type: ignore[method-assign]
            self._stream = None
            self._original_write = None
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None


@dataclass
class _TrialCard:
    """Per-trial state feeding the left pane and right pane.

    Only lifecycle events (``trial_started`` / ``trial_completed`` /
    ``trial_failed`` / ``judgment_scored``) bump :attr:`last_update_ts`.
    ``trial_progress`` mutates ``turn_count`` + ``last_event_kind`` +
    counters but leaves ``last_update_ts`` untouched, so focus does not
    alternate on per-turn ticks. The in-flight LLM fields (``llm_role`` /
    ``llm_provider_model`` / ``llm_call_start_ts`` / ``llm_retry_state``)
    are set by :meth:`LiveRunDisplay.llm_call_started` /
    :meth:`llm_retry_scheduled` and cleared by
    :meth:`llm_call_finished` (also cleared on terminal transitions), so
    the focused pane reflects the wire state of the current attempt only.
    """

    trial_id: str
    task_id: str
    trial_index: int
    status: str  # "running" | "completed" | "failed"
    total_index: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    score: float | None = None
    binary_pass: bool | None = None
    error: str | None = None
    last_update_ts: datetime = field(default_factory=_now)
    turn_count: int = 0
    last_event_kind: str = "started"
    containers: list[ContainerSnapshot] | None = None
    agent_model: str | None = None
    user_model: str | None = None
    llm_role: LLMCallRole | None = None
    llm_provider_model: str | None = None
    llm_call_start_ts: datetime | None = None
    llm_retry_state: tuple[int, float, str] | None = None


@dataclass(frozen=True)
class _FocusedPaneSnapshot:
    """Under-lock snapshot of the focused card for :meth:`LiveRunDisplay._render_right_pane`.

    Extracted so the render helper can drop the lock before drawing —
    Rich renderable construction is not lock-critical and holding the
    lock across it would serialise every 4-Hz refresh against event
    handlers. Frozen because the render path never mutates the snapshot.
    """

    turn_count: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    last_event_kind: str
    status: str
    error: str | None
    task_id: str
    trial_index: int
    containers: list[ContainerSnapshot] | None
    agent_model: str | None
    llm_role: LLMCallRole | None
    llm_provider_model: str | None
    llm_call_start_ts: datetime | None
    llm_retry_state: tuple[int, float, str] | None


_RETRY_MAX_ATTEMPTS = 5
"""Static coupling to the engine's ``stop_after_attempt(5)`` — the seam
does not carry ``max_attempts``, so the ``/5`` label in the retry line
mirrors the runner's fixed cap. Change together with the runner constant."""


def _format_call_state_line(
    *,
    llm_role: LLMCallRole | None,
    llm_provider_model: str | None,
    llm_call_start_ts: datetime | None,
    llm_retry_state: tuple[int, float, str] | None,
) -> str | None:
    """Render the in-flight LLM line for the focused pane.

    Precedence: retry state wins over waiting state (a scheduled retry is
    strictly more informative — it names the failure reason and the
    remaining backoff). Returns ``None`` when no call is in flight.
    """
    if llm_retry_state is not None:
        attempt, next_in_s, reason = llm_retry_state
        return f"↻ retry {attempt}/{_RETRY_MAX_ATTEMPTS} after {next_in_s:.0f}s ({reason})"
    if llm_role is not None and llm_call_start_ts is not None:
        elapsed = (_now() - llm_call_start_ts).total_seconds()
        provider_model = llm_provider_model or ""
        return f"⏳ waiting on {llm_role}: {provider_model} — {elapsed:.1f}s"
    return None


def _clear_llm_call_state(card: _TrialCard) -> None:
    """Drop the four in-flight LLM fields on ``card``.

    Fired at every terminal edge — ``llm_call_finished`` (attempt returned
    or raised), ``trial_completed`` / ``trial_failed`` (trial finished
    without a matching finish event) — so the focused pane never shows a
    stale ``⏳ waiting on …`` line after the call is over.
    """
    card.llm_role = None
    card.llm_provider_model = None
    card.llm_call_start_ts = None
    card.llm_retry_state = None


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


_HEALTH_GLYPHS: dict[str, str] = {
    "healthy": "✓",
    "unhealthy": "✗",
    "starting": "⋯",
    "created": "⋯",
    "no_probe": "~",
    "unknown": "~",
}


def _health_glyph(status: str) -> str:
    """Return the compact health glyph for a service/container status.

    ``✓`` healthy, ``⋯`` starting/created, ``✗`` unhealthy, ``~`` no
    probe / unknown. Unknown inputs fall through to ``~`` — a status
    string we don't recognise is treated as "no readable probe".
    """
    return _HEALTH_GLYPHS.get(status, "~")


def _summarise_ports(ports: dict[int, int]) -> str:
    """Render a compact ``container_port→host_port`` port list.

    Empty ports render as an empty string so callers can skip a bare
    ``-`` column when the service publishes nothing.
    """
    if not ports:
        return ""
    return ", ".join(f"{container}→{host}" for container, host in sorted(ports.items()))


def _service_to_component(service: ServiceSnapshot, *, phase: str) -> ComponentSnapshot:
    """Adapter: lift a ``ServiceSnapshot`` into a ``ComponentSnapshot``.

    Used by :meth:`LiveRunDisplay.phase_changed`'s services-list branch so
    legacy callers that only fire ``phase_changed(services=[…])`` still
    populate the Components widget. The ``phase`` argument is the run
    phase (``"starting_services"`` / ``"services_ready"``), not the
    component phase — the docker container status is mapped independently.
    """
    status = service.get("status", "")
    comp_phase = _map_docker_status_to_component_phase(status, phase)
    return ComponentSnapshot(
        id=build_component_id("engine", "docker.service", service["name"]),
        kind="docker.service",
        phase=comp_phase,
        detail=(
            f"{status} · {_summarise_ports(service.get('ports', {}))}".rstrip(" ·")
            if status
            else None
        ),
        owner="engine",
    )


def _container_to_component(container: ContainerSnapshot, *, trial_id: str) -> ComponentSnapshot:
    """Adapter: lift a per-trial ``ContainerSnapshot`` into a ``ComponentSnapshot``.

    Trial containers become first-class components under
    ``owner="trial/<trial_id>"`` so they inherit the components model's
    per-row log buffer + ``l``-toggle-on-focus surface. They do NOT
    appear in the top-level Engine Components widget — that widget
    filters to ``owner == "engine"``. They render in the per-trial
    "Infrastructure" sub-panel inside the Focused pane instead.

    ``detail`` carries the docker state + health + published ports so
    the operator sees per-container context at a glance without a
    separate ports column.
    """
    health = container.get("health")
    state = container.get("state", "unknown")
    comp_phase: ComponentPhase
    if health == "healthy":
        comp_phase = "healthy"
    elif health == "unhealthy":
        comp_phase = "unhealthy"
    elif health == "starting":
        comp_phase = "starting"
    elif state == "running":
        comp_phase = "healthy"
    elif state in ("exited", "dead"):
        comp_phase = "dead"
    else:
        comp_phase = "pending"
    ports_summary = _summarise_ports(container.get("ports", {}))
    detail_parts = [state]
    if health:
        detail_parts.append(health)
    if ports_summary:
        detail_parts.append(ports_summary)
    return ComponentSnapshot(
        id=build_component_id(
            f"trial/{trial_id}", "container", container.get("service", "unknown")
        ),
        kind="container",
        phase=comp_phase,
        detail=" · ".join(detail_parts),
        owner=f"trial/{trial_id}",
    )


def _map_docker_status_to_component_phase(docker_status: str, run_phase: str) -> ComponentPhase:
    """Map docker container ``status`` ∈ compose-status vocabulary to a
    :data:`ComponentPhase`.

    The ``run_phase`` context matters: ``"starting_services"`` +
    ``status="created"`` means ``pending`` (declared but not started
    yet). ``"services_ready"`` + ``status="created"`` means the container
    is idle by design — in ``per_trial`` / task-declared-stack mode the
    orchestrator only builds the engine images and never starts the
    engine containers themselves; the per-trial stacks own runtime.
    That state is ``stopped`` (steady non-running), not ``unhealthy``.
    """
    if docker_status == "running":
        return "healthy"
    if docker_status in ("exited", "dead"):
        return "dead"
    if docker_status == "unhealthy":
        return "unhealthy"
    if docker_status in ("created", "not_created"):
        return "pending" if run_phase == "starting_services" else "stopped"
    if docker_status in ("starting", "restarting", "paused"):
        return "starting"
    return "pending"


def _component_tail_visible(
    component: ComponentSnapshot,
    log_buffers: dict[str, deque[tuple[float, str, str]]],
    logs_shown: frozenset[str] = frozenset(),
) -> bool:
    """Should the component's log tail render beneath its row?

    Tail expands whenever the buffer has entries AND either:
    (a) the component is NOT in a stable phase (``healthy`` / ``stopped``);
        rationale — a ``starting`` component with retry errors is worth
        showing NOW, not after the outer retry gives up. Or:
    (b) the operator has explicitly toggled logs on for this component
        (via the ``l`` key on the focused component); rationale — the
        operator asked for it. Even a stable healthy component reveals
        its buffered history when the toggle is on.

    Focus alone does NOT expand the tail — the operator selects the row
    with ``[`` / ``]`` (highlight only) and then presses ``l`` to reveal
    its history. This keeps navigation cheap on rows.
    """
    tail = log_buffers.get(component["id"])
    if not tail:
        return False
    if component["id"] in logs_shown:
        return True
    return component["phase"] not in _STABLE_PHASES


def _components_desired_height(
    components: dict[str, ComponentSnapshot],
    log_buffers: dict[str, deque[tuple[float, str, str]]],
    logs_shown: frozenset[str] = frozenset(),
) -> int:
    """Row count the Components widget wants: one per component +
    N per component whose tail is currently expanded + 2 border rows.

    Callers use this to size the layout region; the widget's own render
    always fills exactly its granted rows (Rich crop-from-bottom).
    """
    if not components:
        return 0
    row_count = len(components)
    for comp in components.values():
        if _component_tail_visible(comp, log_buffers, logs_shown):
            tail = log_buffers.get(comp["id"])
            if tail:
                row_count += min(len(tail), _COMPONENT_TAIL_MAX_LINES)
    return row_count + 2  # +2 for the Panel's top/bottom border


def _render_components_table(
    components: dict[str, ComponentSnapshot],
    log_buffers: dict[str, deque[tuple[float, str, str]]],
    focused_id: str | None = None,
    logs_shown: frozenset[str] = frozenset(),
) -> Text:
    """Render the components status widget as a flat multi-line ``Text``.

    Rows: ``[icon] [id]  [phase]  [detail]``. Components with a visible
    tail (see :func:`_component_tail_visible`) get their last
    :data:`_COMPONENT_TAIL_MAX_LINES` log lines indented beneath the
    row, prefixed with ``└─``. Empty component set renders the "(no
    components tracked)" placeholder.

    The focused row is highlighted with a leading ``▶ `` marker and a
    full-row reverse-video style so it stands out regardless of theme.

    Kept flat (``Text`` inside ``Panel``, no Rich ``Table``) for the same
    reason as :func:`_render_services_table` — Table row-drop under a
    tight ``Layout(size=…)`` cap.

    Sort order: primary by ``owner`` (``None`` last), secondary by ``id``
    so the widget renders deterministically across refreshes.
    """
    if not components:
        return Text("(no components tracked)", style="muted")
    rows = sorted(
        components.values(),
        key=lambda c: (c.get("owner") or "￿", c["id"]),
    )
    id_w = min(60, max((len(c["id"]) for c in rows), default=8))
    phase_w = max((len(c["phase"]) for c in rows), default=8)
    text = Text()
    for component in rows:
        phase = component["phase"]
        icon = _COMPONENT_PHASE_ICONS.get(phase, "?")
        style = _COMPONENT_PHASE_STYLES.get(phase, "muted")
        detail = component.get("detail") or ""
        # Focused row: ▶ marker + reverse-video so the whole line
        # inverts fg/bg. This reads as "selected" on every terminal
        # theme without depending on a specific background colour.
        is_focused = focused_id is not None and component["id"] == focused_id
        prefix = "▶ " if is_focused else "  "
        row_style = "reverse bold" if is_focused else style
        line = f"{prefix}{icon}  {component['id']:<{id_w}}  {phase:<{phase_w}}  {detail}".rstrip()
        text.append(line + "\n", style=row_style)
        if _component_tail_visible(component, log_buffers, logs_shown):
            tail = log_buffers[component["id"]]
            for _ts, level, message in list(tail)[-_COMPONENT_TAIL_MAX_LINES:]:
                text.append(
                    f"    └─ [{level}] {message}\n",
                    style="muted",
                )
    # Strip the trailing newline so the Text hugs its Panel border.
    if text.plain.endswith("\n"):
        text.right_crop(1)
    return text


def _render_services_table(services: list[ServiceSnapshot]) -> Text:
    """Compact one-line-per-service renderable for the startup-window widget.

    Kept flat (no Rich ``Table``) because a Table inside a ``Panel`` inside a
    fixed-height ``Layout`` region can silently drop data rows when Rich's
    expand/ratio math clashes with a tight `size=` cap; a plain ``Text`` block
    always renders every line it contains.

    Format per row: ``  {glyph}  {name:<w}  {status:<12}  {ports}``.
    """
    if not services:
        return Text("(no services declared)", style="muted")
    name_w = max((len(s["name"]) for s in services), default=8)
    lines: list[str] = []
    for svc in services:
        glyph = _health_glyph(svc.get("status", "unknown"))
        status = svc.get("status", "")
        ports = _summarise_ports(svc.get("ports", {}))
        lines.append(f"  {glyph}  {svc['name']:<{name_w}}  {status:<12}  {ports}".rstrip())
    return Text("\n".join(lines))


def _docker_boot_records(records: Iterable[logging.LogRecord]) -> list[logging.LogRecord]:
    """Return the boot-window docker milestone records, in input order.

    The trailing dot is load-bearing: it excludes the bare
    ``tolokaforge.docker`` namespace logger and any sibling like
    ``tolokaforge.dockerx``.
    """
    return [r for r in records if r.name.startswith("tolokaforge.docker.")]


def _render_boot_log_tail(
    records: Iterable[logging.LogRecord], max_lines: int = _BOOT_LOG_MAX_LINES
) -> Panel:
    """Render the last ``max_lines`` ``tolokaforge.docker.*`` records as a Panel.

    Timestamps are rendered in UTC so the byte output is stable across
    dev-box and CI timezones. ``int(record.msecs)`` truncates rather than
    rounds — ``f"{999.6:03.0f}"`` would overflow to ``"1000"`` and misalign
    the column; ``int()`` caps at 999.

    Mirrors :func:`_render_services_table`'s flat-``Text``-inside-``Panel``
    shape: a Rich ``Table`` inside a tight fixed-height ``Layout`` can
    silently drop rows. The internal :func:`_docker_boot_records` call is
    idempotent — pre-filtered input passes through unchanged, and
    unfiltered input is filtered here — so this helper is safe to invoke
    with either shape.
    """
    filtered = _docker_boot_records(records)
    tail = filtered[-max_lines:]
    lines: list[str] = []
    for record in tail:
        short_name = record.name.rsplit(".", 1)[-1]
        stamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        stamp = f"{stamp}.{int(record.msecs):03d}"
        lines.append(f"{stamp} | {short_name} | {record.getMessage()}")
    return Panel(
        Text("\n".join(lines)),
        title="Boot log",
        border_style="muted",
        padding=(0, 1),
    )


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


_TRIAL_LOG_TAIL_MAX = 20
"""Number of most-recent per-trial log records shown in the focused pane's
log view. Kept small so the pane stays a fixed-height tail, not a scrollback."""


def _log_level_style(levelno: int) -> str:
    """Map a log level to a THEME token for the per-trial log view.

    ``DEBUG`` → ``muted``, ``INFO`` → ``default`` (unstyled), ``WARNING`` →
    ``warn``, ``ERROR`` / ``CRITICAL`` → ``error``. Levels between the standard
    thresholds take the style of the nearest lower threshold.
    """
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warn"
    if levelno >= logging.INFO:
        return "default"
    return "muted"


def _render_trial_log_tail(records: list[logging.LogRecord], *, width: int) -> RenderableType:
    """Render the last :data:`_TRIAL_LOG_TAIL_MAX` records as level-styled lines.

    ``records`` is already filtered to the focused trial and in chronological
    order. Each line is ``HH:MM:SS  LEVEL  logger.name  message`` (UTC stamp for
    stable output across timezones), truncated to ``width``. Empty input renders
    a dim placeholder.
    """
    if not records:
        return Text("(no log records yet for this trial)", style="muted")
    lines: list[Text] = []
    for record in records[-_TRIAL_LOG_TAIL_MAX:]:
        stamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        line = f"{stamp}  {record.levelname}  {record.name}  {record.getMessage()}"
        if len(line) > width:
            line = line[: width - 1] + "…"
        lines.append(Text(line, style=_log_level_style(record.levelno)))
    return Group(*lines)


_ESC_SEQUENCES: dict[str, str] = {
    "\x1b[A": "k",
    "\x1bOA": "k",
    "\x1b[B": "j",
    "\x1bOB": "j",
    "\x1b[C": "j",
    "\x1bOC": "j",
    "\x1b[D": "k",
    "\x1bOD": "k",
    "\x1b[H": "H",
    "\x1bOH": "H",
    "\x1b[F": "L",
    "\x1bOF": "L",
}
_ESC_PREFIXES: frozenset[str] = frozenset({"\x1b", "\x1b[", "\x1bO"})


class _KeyboardListener:
    """Daemon-thread keyboard listener for :class:`LiveRunDisplay`.

    Reads single characters from ``stdin`` in POSIX cbreak mode and
    dispatches them to callbacks on the owning display. Guarded off on
    non-TTY stdin, on Windows (different terminal-input model), and when
    :data:`_INTERACTIVE_PANEL_ENV_VAR` is ``"0"``; in every guarded-off
    case ``__enter__`` is a no-op and the panel keeps its auto-follow-only
    behaviour.

    Cbreak (not raw) mode preserves ``Ctrl-C`` — killing the run still
    works. ``select`` with a 100 ms timeout keeps the thread interruptible
    so ``__exit__`` can join it within the 1 s bound.
    """

    def __init__(self, display: LiveRunDisplay, stdin: TextIO | None = None) -> None:
        self._display = display
        self._stdin: TextIO = stdin if stdin is not None else sys.stdin
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._original_termios: list[Any] | None = None
        self._enabled: bool = False
        self._fd: int | None = None
        # Buffers a partially-read ESC sequence (arrow / Home / End) across
        # bytes; empty when not mid-sequence.
        self._pending_esc: str = ""

    def enabled(self) -> bool:
        """True when :meth:`__enter__` actually started the input thread."""
        return self._enabled

    def _should_start(self) -> bool:
        if os.environ.get(_INTERACTIVE_PANEL_ENV_VAR) == "0":
            return False
        if sys.platform == "win32":
            return False
        try:
            if not self._stdin.isatty():
                return False
        except (ValueError, OSError):
            return False
        return True

    def __enter__(self) -> _KeyboardListener:
        if not self._should_start():
            return self
        try:
            fd = self._stdin.fileno()
            self._original_termios = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (termios.error, OSError, ValueError):
            # Exotic pty / container where ``isatty()`` lies: leave the panel
            # in auto-follow-only mode instead of aborting the Live setup.
            self._original_termios = None
            self._enabled = False
            return self
        self._fd = fd
        self._enabled = True
        self._thread = threading.Thread(
            target=self._run, name="tolokaforge-panel-input", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        try:
            if thread is not None:
                thread.join(timeout=1.0)
        finally:
            if self._original_termios is not None:
                fd = self._stdin.fileno()
                termios.tcsetattr(fd, termios.TCSADRAIN, self._original_termios)
                self._original_termios = None
            self._enabled = False

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.1)
            except (ValueError, OSError):
                # stdin closed or otherwise invalidated mid-run — exit
                # the thread; termios restoration still runs in __exit__.
                return
            if not ready:
                # A lone ESC with no follow-up byte is a bare ESC keypress,
                # not the start of a sequence — drop it so it can't merge
                # with the next keypress into a phantom arrow key.
                self._pending_esc = ""
                continue
            try:
                chunk = os.read(self._fd, 32)
            except (ValueError, OSError):
                return
            if not chunk:
                # EOF — the read end is exhausted (pipe closed); exit cleanly
                # rather than spin on a fd that will never block again.
                return
            self._consume_bytes(chunk.decode("utf-8", errors="replace"))

    def _consume_bytes(self, chars: str) -> None:
        for ch in chars:
            if not self._pending_esc and ch == "\x1b":
                self._pending_esc = "\x1b"
                continue
            if not self._pending_esc:
                self._dispatch(ch)
                continue
            self._pending_esc += ch
            mapped = _ESC_SEQUENCES.get(self._pending_esc)
            if mapped is not None:
                self._pending_esc = ""
                self._dispatch(mapped)
            elif self._pending_esc not in _ESC_PREFIXES:
                # Unknown or over-long sequence — drop the whole buffer rather
                # than dispatch a byte that was only part of a CSI/SS3 run.
                self._pending_esc = ""

    def _dispatch(self, key: str) -> None:
        if key == "j":
            self._display._nav_next_trial()
        elif key == "k":
            self._display._nav_prev_trial()
        elif key == "H":
            self._display._nav_first_trial()
        elif key == "L":
            self._display._nav_last_trial()
        elif key == "f":
            self._display._toggle_auto_follow()
        elif key == "l":
            self._display._toggle_log_pane()
        elif key == "[":
            self._display._nav_prev_component()
        elif key == "]":
            self._display._nav_next_component()
        elif key == "\t":
            self._display._nav_switch_component_panel()


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
        # ``True`` → focus tracks the most-recent lifecycle event (default,
        # matches the pre-listener behaviour). Flipped to ``False`` by the
        # nav callbacks; flipped back by :meth:`_toggle_auto_follow`.
        self._auto_follow: bool = True
        # ``True`` → the focused pane body shows the focused trial's log tail
        # instead of its summary. Toggled by ``l`` via :meth:`_toggle_log_pane`;
        # orthogonal to :attr:`_auto_follow`.
        self._show_logs_pane: bool = False
        # POSIX keyboard listener; set in :meth:`__enter__` when the guards
        # pass. ``None`` when stdin is not a TTY / on Windows / when the
        # escape-hatch env var is set.
        self._keyboard: _KeyboardListener | None = None
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
        # Latest services snapshot from ``phase_changed`` — populated on
        # ``starting_services`` and ``services_ready`` transitions. Feeds
        # the mid-region widget until trials dispatch.
        self._services: list[ServiceSnapshot] | None = None
        # Component monitoring table. Populated by the four
        # ``component_*`` events plus adapter shims lifting
        # ``phase_changed(services=…)`` (engine) and
        # ``trial_provisioned(containers=…)`` (per-trial) records into
        # the same model. Engine-owned rows render in the top-level
        # Engine Components widget; per-trial rows render in the
        # Focused pane's Infrastructure sub-panel scoped to the
        # currently-focused trial. One row per stable component id;
        # last-write-wins on update.
        self._components: dict[str, ComponentSnapshot] = {}
        # Per-component log tail. Bounded ring per id; rendered only when
        # the component is in an unhealthy phase, kept for post-mortem
        # otherwise. Values are ``(ts, level, message)`` tuples so the
        # renderer needs no LogRecord fields.
        self._component_log_buffers: dict[str, deque[tuple[float, str, str]]] = {}
        # Focused component id — the operator's ``[`` / ``]`` selection.
        # ``None`` when nothing is selected. Highlighted with a leading
        # ``▶ `` marker + reverse-video row style; the tail does not
        # auto-expand on focus (see :attr:`_component_logs_shown`).
        self._focused_component_id: str | None = None
        # Component ids whose log tail the operator has explicitly
        # toggled on via ``l`` while focused. Independent of phase:
        # a healthy or stopped component with an entry in this set
        # renders its buffered tail; an unhealthy component always
        # renders its tail regardless. Cleared when the component is
        # unregistered.
        self._component_logs_shown: set[str] = set()
        # Top-of-panel error banner. Populated once the first auth-shaped
        # ``trial_failed`` fires so a bad key doesn't hide as ``fail 1``
        # in the counters. Tuple: (title, message, hint | None).
        self._banner: tuple[str, str, str | None] | None = None
        # Ring buffer feeding the (future) log pane and available now via
        # :meth:`log_records`. Populated by :class:`_LogSink` for the
        # lifetime of the Live context.
        self._log_buffer: deque[logging.LogRecord] = deque(maxlen=_LOG_BUFFER_MAX)
        # Sentinel handlers we removed on ``__enter__`` and re-install on
        # ``__exit__``, paired with the ``_LogSink`` that replaced each.
        self._replaced_log_handlers: list[tuple[logging.Handler, _LogSink]] = []
        # Child-logger handlers pointing at a captured terminal stream: removed
        # for the Live lifetime by the ``__enter__`` sweep, restored on
        # ``__exit__``. See :meth:`_sweep_child_bypass_handlers` for why.
        self._removed_child_handlers: list[tuple[logging.Logger, logging.Handler]] = []
        # ``_LogSink`` instances installed on ``propagate=False`` child loggers
        # so their records surface through the panel instead of being silently
        # dropped once their bypass handler is removed. Removed on ``__exit__``.
        self._added_child_sinks: list[tuple[logging.Logger, _LogSink]] = []
        # Env-gated diagnostic tap on the real stderr stream, populated
        # in ``__enter__`` when ``TOLOKAFORGE_STDERR_PROBE`` is set.
        self._stderr_probe: _StderrProbe | None = None
        self._layout: Layout = self._build_layout()

    @classmethod
    def for_mode(
        cls,
        mode: DisplayMode,
        *,
        cost_budget_usd: float | None = None,
    ) -> AbstractContextManager[object]:
        """Return a display context manager matched to ``mode``.

        - :attr:`DisplayMode.RICH` — a fresh :class:`LiveRunDisplay`.
        - :attr:`DisplayMode.PLAIN` / :attr:`DisplayMode.LOG` /
          :attr:`DisplayMode.NONE` — a :class:`_NoopDisplayCtx`.

        The caller passes ``mode = ctx.obj["display_mode"]`` (a resolved
        :class:`DisplayMode` enum) so this method never re-parses the flag
        or env var. ``cost_budget_usd`` — when the CLI resolved a cost
        cap — enables the amber@80 % / red@100 % styling on the bottom-bar
        cost segment.
        """
        if mode is DisplayMode.RICH:
            return cls(cost_budget_usd=cost_budget_usd)
        return _NoopDisplayCtx()

    @property
    def events(self) -> RunDisplayEvents:
        """The event sink the caller threads into the orchestrator."""
        return self

    def log_records(self) -> tuple[logging.LogRecord, ...]:
        """Return a snapshot of the panel-scoped log ring buffer.

        Callers get a stable, immutable view — the underlying ``deque``
        continues to rotate as new records arrive.
        """
        with self._lock:
            return tuple(self._log_buffer)

    def __enter__(self) -> LiveRunDisplay:
        probe_path = os.environ.get(_STDERR_PROBE_ENV_VAR)
        if probe_path is not None:
            self._stderr_probe = _StderrProbe(Path(probe_path))
            self._stderr_probe.__enter__()
        # Snapshot the four terminal stream objects BEFORE Rich Live installs
        # its redirect proxy: chatty libraries (litellm) captured the raw
        # stream at import time into their ``StreamHandler`` instances, so the
        # sweep below compares handler stream identity against these captured
        # references — not against the post-Live ``sys.stderr`` proxy name.
        dangerous_streams = _capture_dangerous_streams()
        self._live = make_live(self._layout, refresh_per_second=self._refresh_per_second)
        self._live.__enter__()
        # Route WARNING+ log records through Live's own console. Rich Live
        # intercepts prints on the console it owns, temporarily lifts the
        # cursor above the live region, prints, then re-renders — so the
        # panel stays anchored. Writing to the raw stderr stream instead
        # bypasses that coordination and causes duplicate-panel stacking.
        live_console = self._live.console

        def _print_above(line: str) -> None:
            live_console.print(line, markup=False, highlight=False)

        root = logging.getLogger()
        # In production, ``configure_root_logging`` guarantees at most one
        # sentinel-tagged handler at any time; iterating defensively keeps
        # the replacement correct if an embedder or test suite has installed
        # additional sentinel handlers.
        for handler in list(root.handlers):
            if not getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
                continue
            sink = _LogSink(
                print_above=_print_above,
                formatter=handler.formatter,
                buffer=self._log_buffer,
                route_component_log=self._route_component_log,
            )
            sink.setLevel(handler.level)
            setattr(sink, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
            root.removeHandler(handler)
            root.addHandler(sink)
            self._replaced_log_handlers.append((handler, sink))
        self._sweep_child_bypass_handlers(dangerous_streams, _print_above)
        self._keyboard = _KeyboardListener(self)
        self._keyboard.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._keyboard is not None:
            self._keyboard.__exit__(*exc_info)
            self._keyboard = None
        for logger_obj, sink in self._added_child_sinks:
            logger_obj.removeHandler(sink)
        self._added_child_sinks = []
        for logger_obj, handler in self._removed_child_handlers:
            logger_obj.addHandler(handler)
        self._removed_child_handlers = []
        root = logging.getLogger()
        for original, sink in self._replaced_log_handlers:
            root.removeHandler(sink)
            root.addHandler(original)
        self._replaced_log_handlers = []
        if self._live is not None:
            self._live.__exit__(*exc_info)
            self._live = None
        if self._stderr_probe is not None:
            self._stderr_probe.__exit__(*exc_info)
            self._stderr_probe = None

    def _sweep_child_bypass_handlers(
        self,
        dangerous_streams: tuple[TextIO, ...],
        print_above: Callable[[str], None],
    ) -> None:
        """Remove every non-root logger handler that would bypass Rich Live.

        Iterates ``logging.root.manager.loggerDict``, skipping ``PlaceHolder``
        entries (which lack ``.handlers``), and removes each handler that is a
        ``logging.StreamHandler`` whose ``.stream`` is one of the four captured
        terminal streams. Propagating loggers rely on the root ``_LogSink`` to
        surface their records; non-propagating loggers additionally receive a
        fresh ``_LogSink`` so their records are not silently dropped once the
        bypass handler is gone.
        """
        manager = logging.root.manager
        for logger_obj in list(manager.loggerDict.values()):
            if not isinstance(logger_obj, logging.Logger):
                continue
            bypass_handlers = [
                h
                for h in list(logger_obj.handlers)
                if isinstance(h, logging.StreamHandler)
                and any(getattr(h, "stream", None) is s for s in dangerous_streams)
            ]
            if not bypass_handlers:
                continue
            for handler in bypass_handlers:
                logger_obj.removeHandler(handler)
                self._removed_child_handlers.append((logger_obj, handler))
            if logger_obj.propagate:
                continue
            sink = _LogSink(
                print_above=print_above,
                formatter=None,
                buffer=self._log_buffer,
                route_component_log=self._route_component_log,
            )
            logger_obj.addHandler(sink)
            self._added_child_sinks.append((logger_obj, sink))

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

    def phase_changed(
        self,
        *,
        phase: str,
        detail: str | None = None,
        services: list[ServiceSnapshot] | None = None,
    ) -> None:
        """Record a pre-``run_started`` milestone (Docker startup, etc.).

        The bottom bar reads :attr:`_current_phase` when ``_total_trials == 0``
        and renders ``"Starting services… (detail)"`` instead of the empty
        ``0/0 · 0 running · …`` line. When ``services`` is supplied the
        panel adds a mid-region widget with one row per service and, via
        the components-model adapter, lifts each ``ServiceSnapshot`` into
        a ``ComponentSnapshot`` so callers that only fire the legacy path
        still populate the Components widget. Once ``run_started`` fires,
        phase state is cleared and the bar switches to counters.
        """
        with self._lock:
            self._current_phase = phase
            self._current_phase_detail = detail
            if services is not None:
                self._services = list(services)
                for svc in services:
                    self._ingest_component_locked(_service_to_component(svc, phase=phase))
            self._refresh_live_locked()

    def component_registered(self, *, snapshot: ComponentSnapshot) -> None:
        """Announce a component the panel should start tracking."""
        with self._lock:
            self._ingest_component_locked(snapshot)
            self._refresh_live_locked()

    def component_status_changed(self, *, snapshot: ComponentSnapshot) -> None:
        """Update a component's phase / detail. Unknown ids implicit-register."""
        with self._lock:
            self._ingest_component_locked(snapshot)
            self._refresh_live_locked()

    def component_log_appended(
        self,
        *,
        component_id: str,
        level: str,
        message: str,
        ts: float,
    ) -> None:
        """Attach a log line to the component's tail buffer.

        Kept out of the panel's general log ring so component chatter never
        scrolls above the panel. Rendered beneath the component's row
        whenever the buffer is non-empty and the component is not in a
        stable phase (``healthy`` / ``stopped``) — see
        :func:`_component_tail_visible`.
        """
        with self._lock:
            buf = self._component_log_buffers.setdefault(
                component_id,
                deque(maxlen=_COMPONENT_TAIL_BUFFER_MAX),
            )
            buf.append((ts, level, message))
            # Refresh only if the tail is currently visible for this
            # component; otherwise the buffer grows silently for later
            # inspection. Consult the operator's per-component log
            # toggle so an appended line lands in an already-expanded
            # tail even when the phase is stable.
            snap = self._components.get(component_id)
            if snap is not None and _component_tail_visible(
                snap,
                self._component_log_buffers,
                frozenset(self._component_logs_shown),
            ):
                self._refresh_live_locked()

    def component_unregistered(self, *, component_id: str) -> None:
        """Drop a component from the display's tracking set."""
        with self._lock:
            self._components.pop(component_id, None)
            self._component_log_buffers.pop(component_id, None)
            self._component_logs_shown.discard(component_id)
            if self._focused_component_id == component_id:
                self._focused_component_id = None
            self._refresh_live_locked()

    def _route_component_log(self, component_id: str, level: str, message: str, ts: float) -> None:
        """Callback wired into :class:`_LogSink` so any ``logging`` record
        tagged with ``extra={"component_id": ...}`` lands in that
        component's tail buffer instead of scrolling above the panel.

        The tag is the opt-in mechanism for any subsystem that owns a
        monitored component: gRPC clients, Docker service startup,
        HealthProbe retry loops, future k8s / SSH reporters. Emitters
        keep their log records at the natural level (WARNING / ERROR
        etc.) — the sink handles the visualisation switch.
        """
        # Delegate to the public event handler so the two ingestion paths
        # (Protocol event + logging-record routing) share one code path.
        self.component_log_appended(component_id=component_id, level=level, message=message, ts=ts)

    def _ingest_component_locked(self, snapshot: ComponentSnapshot) -> None:
        """Upsert one snapshot into :attr:`_components`. Caller MUST hold the lock.

        Same-id updates overwrite in place — this is what keeps per-attempt
        polling from scrolling the log stream. The
        :func:`_service_to_component` / :func:`_container_to_component`
        adapters route through here so the wire shape is normalised
        regardless of the source.
        """
        self._components[snapshot["id"]] = snapshot

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
        with self._lock:
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
            self._trials[trial_id] = card
            self._running += 1
            if self._auto_follow:
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

    def trial_provisioned(
        self,
        *,
        trial_id: str,
        containers: list[ContainerSnapshot],
        endpoints: dict[str, str],  # noqa: ARG002 — future log-pane field
    ) -> None:
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            card.containers = list(containers)
            # Lift each container into the components model under
            # ``owner="trial/<trial_id>"``. The top-level Engine
            # Components widget filters to ``owner == "engine"`` and
            # ignores these — but the Focused pane's Infrastructure
            # sub-panel filters to the currently-focused trial's
            # containers and renders them with the same focus /
            # log-tail machinery as the top widget, so ``[``/``]`` +
            # ``l`` can drill into any container the same way they
            # drill into an engine service.
            for container in containers:
                self._ingest_component_locked(_container_to_component(container, trial_id=trial_id))
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
            _clear_llm_call_state(card)
            if was_running and self._running > 0:
                self._running -= 1
            self._completed += 1
            if self._auto_follow:
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
            _clear_llm_call_state(card)
            if was_running and self._running > 0:
                self._running -= 1
            self._failed += 1
            if self._auto_follow:
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

    def llm_call_started(
        self,
        *,
        trial_id: str,
        role: LLMCallRole,
        provider: str,
        model: str,
        attempt: int,  # noqa: ARG002 — reserved for future retry-attempt labelling
    ) -> None:
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            card.llm_role = role
            card.llm_provider_model = f"{provider}/{model}"
            card.llm_call_start_ts = _now()
            card.llm_retry_state = None
            self._refresh_live_locked()

    def llm_call_finished(
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
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            _clear_llm_call_state(card)
            self._refresh_live_locked()

    def llm_retry_scheduled(
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
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            card.llm_retry_state = (attempt, next_attempt_in_s, reason)
            self._refresh_live_locked()

    def judgment_scored(self, *, trial_id: str, score: float, binary_pass: bool) -> None:
        with self._lock:
            card = self._trials.get(trial_id) or self._lazy_card_locked(trial_id)
            card.score = score
            card.binary_pass = binary_pass
            card.last_event_kind = "judged"
            card.last_update_ts = _now()
            if self._auto_follow:
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

    # Keyboard-nav callbacks -----------------------------------------

    def _nav_next_trial(self) -> None:
        """Focus the next visible trial (in :meth:`_visible_cards` order)."""
        with self._lock:
            self._focus_at_offset_locked(1)

    def _nav_prev_trial(self) -> None:
        """Focus the previous visible trial (in :meth:`_visible_cards` order)."""
        with self._lock:
            self._focus_at_offset_locked(-1)

    def _nav_next_component(self) -> None:
        """Focus the next inspectable component (engine + focused trial's containers)."""
        with self._lock:
            self._nav_component_focus_locked(1)

    def _nav_prev_component(self) -> None:
        """Focus the previous inspectable component (engine + focused trial's containers)."""
        with self._lock:
            self._nav_component_focus_locked(-1)

    def _engine_component_ids_locked(self) -> list[str]:
        """Sorted engine-owned component ids. Caller MUST hold :attr:`_lock`."""
        rows = [comp for comp in self._components.values() if comp.get("owner") == "engine"]
        rows.sort(key=lambda c: c["id"])
        return [c["id"] for c in rows]

    def _focused_trial_container_ids_locked(self) -> list[str]:
        """Sorted container ids of the currently-focused trial. Empty when
        no trial is focused or the focused trial has no containers.
        Caller MUST hold :attr:`_lock`.
        """
        if self._focused_trial_id is None:
            return []
        owner = f"trial/{self._focused_trial_id}"
        rows = [comp for comp in self._components.values() if comp.get("owner") == owner]
        rows.sort(key=lambda c: c["id"])
        return [c["id"] for c in rows]

    def _inspectable_component_ids_locked(self) -> list[str]:
        """All component ids the operator can focus RIGHT NOW — union of
        the engine group and the focused-trial container group. Used as
        the seed set when the operator presses ``[`` / ``]`` with no
        prior focus. Caller MUST hold :attr:`_lock`.
        """
        return self._engine_component_ids_locked() + self._focused_trial_container_ids_locked()

    def _current_component_group_locked(self) -> str | None:
        """Group of the currently-focused component: ``"engine"`` /
        ``"trial"`` / ``None`` (no focus, or focus points at a stale id).
        Caller MUST hold :attr:`_lock`.
        """
        cid = self._focused_component_id
        if cid is None:
            return None
        comp = self._components.get(cid)
        if comp is None:
            return None
        owner = comp.get("owner") or ""
        if owner == "engine":
            return "engine"
        if owner.startswith("trial/"):
            return "trial"
        return None

    def _ids_for_group_locked(self, group: str) -> list[str]:
        """Sorted component ids in ``group`` (``"engine"`` or ``"trial"``).
        Caller MUST hold :attr:`_lock`.
        """
        if group == "engine":
            return self._engine_component_ids_locked()
        if group == "trial":
            return self._focused_trial_container_ids_locked()
        return []

    def _nav_component_focus_locked(self, delta: int) -> None:
        """Move :attr:`_focused_component_id` by ``delta`` **within its
        current group** (engine or focused-trial containers).

        ``[`` / ``]`` are now within-panel walks; ``Tab`` (see
        :meth:`_nav_switch_component_panel_locked`) is the between-panels
        jump. Wraps at each group's boundaries. When no component is
        focused yet, seed from the engine group (first row on ``]``,
        last row on ``[``); if that group is empty, fall back to the
        focused-trial container group. Caller MUST hold :attr:`_lock`.
        """
        group = self._current_component_group_locked()
        if group is not None:
            ids = self._ids_for_group_locked(group)
            if not ids:
                return
            idx = ids.index(self._focused_component_id) if self._focused_component_id in ids else 0
            idx = (idx + delta) % len(ids)
            self._focused_component_id = ids[idx]
            self._refresh_live_locked()
            return
        # No current focus — seed from the engine group when possible,
        # else the focused-trial container group.
        for candidate in ("engine", "trial"):
            ids = self._ids_for_group_locked(candidate)
            if ids:
                self._focused_component_id = ids[0 if delta >= 0 else -1]
                self._refresh_live_locked()
                return

    def _nav_switch_component_panel_locked(self) -> None:
        """Jump component focus between the two panels (engine ↔ trial).

        Lands on the first row of the target group. When the target
        group is empty, stay put — there is nothing to jump to. When
        no component is focused yet, seed the engine group's first row
        (Tab from a clean start behaves like ``]``). Caller MUST hold
        :attr:`_lock`.
        """
        current = self._current_component_group_locked()
        target = "trial" if current == "engine" else "engine"
        ids = self._ids_for_group_locked(target)
        if not ids:
            if current is None:
                fallback = self._ids_for_group_locked("engine") or self._ids_for_group_locked(
                    "trial"
                )
                if fallback:
                    self._focused_component_id = fallback[0]
                    self._refresh_live_locked()
            return
        self._focused_component_id = ids[0]
        self._refresh_live_locked()

    def _nav_switch_component_panel(self) -> None:
        """Public entry for the ``Tab`` handler. Delegates under :attr:`_lock`."""
        with self._lock:
            self._nav_switch_component_panel_locked()

    def _nav_first_trial(self) -> None:
        """Focus the first visible trial in :meth:`_visible_cards` order."""
        with self._lock:
            visible = self._visible_cards()
            if not visible:
                return
            self._auto_follow = False
            self._focused_trial_id = visible[0].trial_id
            self._clear_stale_container_focus_locked()
            self._refresh_live_locked()

    def _nav_last_trial(self) -> None:
        """Focus the last visible trial in :meth:`_visible_cards` order."""
        with self._lock:
            visible = self._visible_cards()
            if not visible:
                return
            self._auto_follow = False
            self._focused_trial_id = visible[-1].trial_id
            self._clear_stale_container_focus_locked()
            self._refresh_live_locked()

    def _toggle_auto_follow(self) -> None:
        """Flip :attr:`_auto_follow`; when re-enabling, snap to newest event.

        Re-enabling picks the card with the maximum ``last_update_ts`` (the
        card auto-follow would have selected on the most-recent lifecycle
        event) so ``f`` reads as "resume tracking" rather than "leave focus
        parked on the manual pick".
        """
        with self._lock:
            self._auto_follow = not self._auto_follow
            if self._auto_follow and self._trials:
                newest = max(self._trials.values(), key=lambda c: c.last_update_ts)
                self._focused_trial_id = newest.trial_id
                self._clear_stale_container_focus_locked()
            self._refresh_live_locked()

    def _toggle_log_pane(self) -> None:
        """Context-sensitive log toggle.

        When a component row is currently focused (via ``[`` / ``]``),
        flip that component's entry in :attr:`_component_logs_shown` so
        its buffered tail expands or collapses beneath its row. This
        works for any phase — a stable healthy row still reveals its
        history on demand.

        When no component is focused, fall through to the pre-existing
        trial-log behaviour: flip :attr:`_show_logs_pane` so the
        focused-trial pane swaps between its structured summary and
        the trial's log tail. :attr:`_auto_follow` is left untouched
        in both branches.
        """
        with self._lock:
            focused = self._focused_component_id
            if focused is not None and focused in self._components:
                if focused in self._component_logs_shown:
                    self._component_logs_shown.discard(focused)
                else:
                    self._component_logs_shown.add(focused)
            else:
                self._show_logs_pane = not self._show_logs_pane
            self._refresh_live_locked()

    def _focus_at_offset_locked(self, delta: int) -> None:
        """Move focus by ``delta`` positions in :meth:`_visible_cards` order.

        ``delta = +1`` for ``j``, ``-1`` for ``k``. When the current focus
        is not in the visible window (or unset), the offset is applied from
        the first visible card so the first ``j`` press lands on the second
        visible trial. Caller MUST already hold :attr:`_lock`.
        """
        visible = self._visible_cards()
        if not visible:
            return
        self._auto_follow = False
        current_ids = [card.trial_id for card in visible]
        try:
            current_index = current_ids.index(self._focused_trial_id or "")
        except ValueError:
            current_index = 0
        new_index = max(0, min(len(visible) - 1, current_index + delta))
        self._focused_trial_id = visible[new_index].trial_id
        self._clear_stale_container_focus_locked()
        self._refresh_live_locked()

    def _clear_stale_container_focus_locked(self) -> None:
        """Drop ``_focused_component_id`` when it points at a container
        of a trial that is no longer focused. Engine focus is preserved.
        Caller MUST hold :attr:`_lock`.
        """
        cid = self._focused_component_id
        if cid is None:
            return
        comp = self._components.get(cid)
        if comp is None:
            self._focused_component_id = None
            return
        owner = comp.get("owner") or ""
        if not owner.startswith("trial/"):
            return
        if owner != f"trial/{self._focused_trial_id}":
            self._focused_component_id = None

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
            total_index=0,
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

    def _viewport_rows(self) -> int:
        """Return the current terminal height in rows.

        Reads ``self._live.console.height`` when the Live context is
        active (the size Rich actually renders into); falls back to
        Rich's default 25 lines otherwise. Used by :meth:`_build_layout`
        to cap the trials-pane height so a 3-trial run doesn't waste
        vertical space.
        """
        if self._live is not None:
            return self._live.console.height
        return 25

    def _build_layout(self) -> Layout:
        """Build the layout tree with a viewport-locked total height.

        Rich ``Live`` in inline (``screen=False``) mode redraws in place ONLY
        when the total rendered height stays constant across refreshes; a
        renderable that grows across the terminal boundary triggers a
        re-anchor and stacks copies (visible bug: multiple panel snapshots
        pile up as services region appears/disappears).

        So: total = ``viewport - 1``. Optional regions (banner, services,
        boot_log) appear by *stealing* rows from ``main``, not by
        lengthening the overall renderable. The invariant holds every
        refresh.

        Row composition (top → bottom):

        - Optional ``banner`` (size 5) — first auth-shaped failure.
        - Optional ``components`` (size ``rows_needed + 2`` — one line
          per component + one indented line per unhealthy-tail entry +
          2 border rows). Visible when any component is tracked and
          either the run is in its startup window OR at least one
          component is currently in an unhealthy phase (surfaces mid-run
          infrastructure failures without keeping the widget always-on).
          Replaces the pre-M11.2 Services widget; ``ServiceSnapshot``
          rows still populate it via the adapter shim in
          :func:`_service_to_component`.
        - Optional ``boot_log`` (size ``min(len(filtered), _BOOT_LOG_MAX_LINES)
          + 2`` desired, clamped down to whatever rows are left after
          ``components`` and ``main``'s floor of 5; dropped entirely when
          the clamp leaves < 3 rows). Only during the startup window when
          ``_docker_boot_records(_log_buffer)`` is non-empty.
        - ``main`` (fills the leftover; min 5) — trials + focused split.
        - ``bottom`` (size 1) — spinner + phase / counters.

        The boot-log clamp preserves the stable-height invariant:
        when boot_log is present, the sum of every row's size is exactly
        ``total``. When absent, the sum matches the pre-boot-log layout
        byte-for-byte.
        """
        layout = Layout()
        with self._lock:
            banner = self._banner
            components = dict(self._components)
            component_log_buffers = {
                cid: deque(buf, maxlen=buf.maxlen)
                for cid, buf in self._component_log_buffers.items()
            }
            in_startup = self._total_trials == 0
            focused_component_id = self._focused_component_id
            logs_shown = frozenset(self._component_logs_shown)
            # Top-level components widget is engine-only: tolokaforge's
            # own docker services + the gRPC runner client (rows whose
            # ``owner == "engine"``). Per-trial containers live in the
            # per-trial "Infrastructure" sub-panel inside the Focused
            # pane instead — one place per concern, no duplication.
            engine_components = {
                cid: comp for cid, comp in components.items() if comp.get("owner") == "engine"
            }
            show_engine = bool(engine_components)
            boot_filtered: list[logging.LogRecord] = (
                _docker_boot_records(self._log_buffer) if in_startup else []
            )
        viewport = self._viewport_rows()
        total = max(12, viewport - 1)
        banner_h = 5 if banner is not None else 0
        engine_h = (
            _components_desired_height(engine_components, component_log_buffers, logs_shown)
            if show_engine
            else 0
        )
        bottom_h = 1
        desired_boot_log_h = (
            min(len(boot_filtered), _BOOT_LOG_MAX_LINES) + 2 if boot_filtered else 0
        )
        # ``budget`` is the row count boot-log may steal from ``main``
        # without pushing ``main`` below its floor of 5. A bordered Panel
        # needs ≥ 3 rows to render at least one content line; below that
        # the region drops entirely so we never emit a zero-content
        # bordered box.
        budget = total - banner_h - engine_h - bottom_h - 5
        boot_log_h = min(desired_boot_log_h, budget) if desired_boot_log_h else 0
        if boot_log_h < 3:
            boot_log_h = 0
        main_h = max(5, total - banner_h - engine_h - boot_log_h - bottom_h)
        row_defs: list[Layout] = []
        if banner is not None:
            row_defs.append(Layout(name="banner", size=banner_h))
        if show_engine:
            row_defs.append(Layout(name="engine_components", size=engine_h))
        if boot_log_h > 0:
            row_defs.append(Layout(name="boot_log", size=boot_log_h))
        row_defs.append(Layout(name="main", size=main_h))
        row_defs.append(Layout(name="bottom", size=bottom_h))
        layout.split_column(*row_defs)
        if banner is not None:
            layout["banner"].update(self._render_banner(banner))
        if show_engine:
            layout["engine_components"].update(
                Panel(
                    _render_components_table(
                        engine_components,
                        component_log_buffers,
                        focused_component_id,
                        logs_shown,
                    ),
                    title="Engine Components",
                    padding=(0, 1),
                )
            )
        if boot_log_h > 0:
            layout["boot_log"].update(
                _render_boot_log_tail(boot_filtered, max_lines=boot_log_h - 2)
            )
        layout["main"].split_row(
            Layout(name="trials", ratio=2),
            Layout(name="focused", ratio=3),
        )
        layout["trials"].update(self._render_left_pane())
        layout["focused"].update(self._render_right_pane())
        layout["bottom"].update(self._render_bottom_bar())
        return layout

    def _main_region_size(self, *, reserved: int) -> int:
        """Return the trials/focused row height for the current state.

        Sized to ``min(cards + 2, viewport - reserved - 1)`` so:

        - 3 running trials in a 40-row terminal give a 5-row pane instead
          of ~35 rows of empty column.
        - Many trials in a small terminal still fit — the cap prevents
          rows from being clipped by the bottom bar.
        - Two rows of headroom cover the panel border (top + bottom).
        """
        visible = self._visible_cards()
        viewport = self._viewport_rows()
        available = max(viewport - reserved - 1, 3)
        desired = len(visible) + 2
        # At least three rows so the empty-state ``(no trials yet)`` line
        # still renders inside its border.
        return max(3, min(desired, available))

    def _render_banner(self, banner: tuple[str, str, str | None]) -> Panel:
        title, message, hint = banner
        lines = [message]
        if hint:
            lines.append(f"[warn]Hint:[/warn] {hint}")
        body = Text.from_markup("\n".join(lines))
        return Panel(body, title=f"[error]✗ {title}[/error]", border_style="error")

    def _render_left_pane(self) -> Panel:
        glyphs = {"running": "⏳", "completed": "✓", "failed": "✗"}
        visible = self._visible_cards()
        with self._lock:
            total_trials = self._total_trials
        index_width = max(len(str(total_trials)), 1) if total_trials else 1
        rendered: list[str] = []
        has_markup = False
        for card in visible:
            glyph = glyphs.get(card.status, "•")
            human_index = card.total_index + 1
            prefix = f"[{human_index:>{index_width}}/{total_trials or '?'}]"
            base = f"{glyph} {prefix} {card.task_id} · {card.trial_index}"
            if card.status == "failed" and card.error:
                err = _truncate_error(card.error, width=40)
                rendered.append(f"{base}  [error]{err}[/error]")
                has_markup = True
            else:
                rendered.append(base)
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
            show_logs = self._show_logs_pane
            log_tail: list[logging.LogRecord] = (
                [r for r in self._log_buffer if getattr(r, "trial_id", None) == trial_id]
                if show_logs and trial_id is not None
                else []
            )
            snapshot: _FocusedPaneSnapshot | None = (
                _FocusedPaneSnapshot(
                    turn_count=card.turn_count,
                    prompt_tokens=card.prompt_tokens,
                    completion_tokens=card.completion_tokens,
                    cost_usd=card.cost_usd,
                    last_event_kind=card.last_event_kind,
                    status=card.status,
                    error=card.error,
                    task_id=card.task_id,
                    trial_index=card.trial_index,
                    containers=(list(card.containers) if card.containers is not None else None),
                    agent_model=card.agent_model,
                    llm_role=card.llm_role,
                    llm_provider_model=card.llm_provider_model,
                    llm_call_start_ts=card.llm_call_start_ts,
                    llm_retry_state=card.llm_retry_state,
                )
                if card is not None
                else None
            )
            # Per-trial container components + their log-tail snapshot,
            # used by the Infrastructure sub-panel below to render
            # focus + expanded log tail alongside each container row.
            trial_owner = f"trial/{trial_id}" if trial_id is not None else None
            trial_components: dict[str, ComponentSnapshot] = (
                {
                    cid: comp
                    for cid, comp in self._components.items()
                    if comp.get("owner") == trial_owner
                }
                if trial_owner is not None
                else {}
            )
            trial_component_log_buffers: dict[str, deque[tuple[float, str, str]]] = {
                cid: deque(buf, maxlen=buf.maxlen)
                for cid, buf in self._component_log_buffers.items()
                if cid in trial_components
            }
            focused_component_id = self._focused_component_id
            logs_shown = frozenset(self._component_logs_shown)
            visible = self._visible_cards()
            visible_total = len(visible)
            position = next(
                (i + 1 for i, c in enumerate(visible) if c.trial_id == trial_id),
                None,
            )
        if snapshot is None:
            if show_logs:
                body = Text("(log stream enabled — waiting for first trial)", style="dim")
            else:
                body = Text("(waiting for first trial)")
            return Panel(body, title="Focused trial")
        title = (
            f"Focused trial · {position}/{visible_total}"
            if position is not None
            else "Focused trial"
        )
        if show_logs:
            width = self._live.console.width if self._live is not None else 120
            return Panel(_render_trial_log_tail(log_tail, width=width), title=title)
        if snapshot.status == "failed" and snapshot.error:
            hint = _derive_hint(snapshot.error)
            parts = [
                f"[error]FAILED[/error]  {snapshot.task_id} · {snapshot.trial_index}",
                "",
                _truncate_error(snapshot.error, width=200),
            ]
            if hint:
                parts.append("")
                parts.append(f"[warn]Hint:[/warn] {hint}")
            body: Text | Table = Text.from_markup("\n".join(parts))
            return Panel(body, title=title)
        summary_lines: list[str] = []
        if snapshot.agent_model is not None:
            summary_lines.append(f"model: {snapshot.agent_model}")
        summary_lines.append(
            f"turn {snapshot.turn_count} · "
            f"in {_format_tokens(snapshot.prompt_tokens)} / "
            f"out {_format_tokens(snapshot.completion_tokens)} tok · "
            f"{_format_cost(snapshot.cost_usd)} · "
            f"last: {snapshot.last_event_kind}"
        )
        call_line = _format_call_state_line(
            llm_role=snapshot.llm_role,
            llm_provider_model=snapshot.llm_provider_model,
            llm_call_start_ts=snapshot.llm_call_start_ts,
            llm_retry_state=snapshot.llm_retry_state,
        )
        if call_line is not None:
            summary_lines.append(call_line)
        summary = Text("\n".join(summary_lines))
        if not trial_components:
            return Panel(summary, title=title)
        # Focused trial has a compose stack: append a compact
        # "Infrastructure" sub-panel under the summary. Rich Group renders
        # both children in sequence within the outer Panel. The sub-panel
        # uses the same components-table renderer as the top Engine
        # Components widget so ``[``/``]`` focus + ``l`` tail expansion
        # behave identically in both places.
        infra_panel = Panel(
            _render_components_table(
                trial_components,
                trial_component_log_buffers,
                focused_component_id,
                logs_shown,
            ),
            title="Infrastructure",
            border_style="muted",
        )
        return Panel(Group(summary, infra_panel), title=title)

    def _render_bottom_bar(self) -> RenderableType:
        with self._lock:
            # Startup window: no trials yet AND a phase event has fired.
            # Renders an animated spinner + "Starting services…" line so the
            # user has a live indicator that work is progressing during the
            # 10-30s Docker-boot / runtime-connect window. ``Spinner`` re-renders
            # every Live tick (4 fps) — same cadence Claude Code's "thinking"
            # indicator uses.
            if self._total_trials == 0 and self._current_phase is not None:
                phase_line = _format_phase_line(self._current_phase, self._current_phase_detail)
                return Spinner("dots", text=Text(phase_line, style="muted"), style="cyan")
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
            manual_nav_active = not self._auto_follow and self._total_trials > 0
        line = _format_bottom_bar(stats)
        # ``Text.from_markup`` interprets ``[warn]…[/warn]`` / ``[error]…[/error]``
        # against the shared theme. The "default" path stays on ``Text(...)``
        # so the pre-B3 goldens (unset-budget baseline) remain byte-identical.
        if not manual_nav_active:
            if cost_style == "default":
                return Text(line)
            return Text.from_markup(line)
        # Manual-nav hint prepends a literal bracketed binding legend — the
        # prefix must be escape()'d so Rich does not consume it as an unknown
        # style tag.
        hint = "[j/k or ↑↓ nav · H/L first/last · f follow · l logs] "
        return Text.from_markup(_escape_markup(hint) + line)


__all__ = [
    "LiveRunDisplay",
    "RunDisplayEvents",
]
