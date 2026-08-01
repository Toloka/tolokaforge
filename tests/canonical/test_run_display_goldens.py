"""Byte-level canonical SVG goldens for :class:`LiveRunDisplay`.

Each golden pins the exact ``Console.export_svg`` output that Rich produces
after a fixed event sequence is replayed into a fresh :class:`LiveRunDisplay`
under a frozen clock. Two widths are covered — 80 columns and 120 columns —
to keep the AC's "renders correctly on 80- and 120-column terminals"
assertion honest under Rich version drift.

**Golden regeneration.** SVG bytes are locked byte-for-byte. When the
panel layout changes (new pane, new bottom-bar field, new glyph) the
goldens must be regenerated in the *same* commit as the code change:

    uv run pytest tests/canonical/test_run_display_goldens.py --update-canon

The test drives the layout directly (``recorder.print(display._build_layout())``)
rather than entering Rich's ``Live`` mainloop — the auto-refresh thread
introduces non-determinism, and every field the golden pins is populated
by the render helpers already exercised through :meth:`_build_layout`.

Determinism knobs:

* ``_now`` in :mod:`tolokaforge.dx.live_panel` is monkey-patched to a
  monotonic sequence — every event's ``last_update_ts`` is strictly
  ordered so the left-pane sort is stable.
* ``unique_id="tolokaforge-run-display"`` fixes the CSS class prefix Rich
  otherwise derives from the recorded content (a hash that would flip on
  any glyph or byte change).
* ``theme=DEFAULT_TERMINAL_THEME`` pins the palette Rich embeds in the
  ``<style>`` block.
* ``color_system="truecolor"`` and ``force_terminal=True`` on the
  recorder ``Console`` bypass the ambient terminal's capability probe.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console
from rich.terminal_theme import DEFAULT_TERMINAL_THEME

from tolokaforge.core.run_display_events import ServiceSnapshot
from tolokaforge.dx import live_panel
from tolokaforge.dx._display import THEME
from tolokaforge.dx.live_panel import LiveRunDisplay

pytestmark = pytest.mark.canonical

GOLDEN_DIR = Path(__file__).parent / "golden" / "run_display"

FIXED_ORIGIN = datetime(2026, 7, 15, 12, 0, 0)

SVG_UNIQUE_ID = "tolokaforge-run-display"

_WIDTHS: tuple[int, ...] = (80, 120)


def _monotonic_clock() -> Iterator[datetime]:
    """Every ``_now()`` call returns ``FIXED_ORIGIN + n·1s``.

    Strictly-increasing so ``_visible_cards`` and the focus follow order
    the trials the same way every run.
    """
    for n in itertools.count():
        yield FIXED_ORIGIN + timedelta(seconds=n)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace :func:`live_panel._now` with a monotonic per-call sequence."""
    seq = _monotonic_clock()
    monkeypatch.setattr(live_panel, "_now", lambda: next(seq))


def _replay_events(display: LiveRunDisplay) -> None:
    """Twenty events covering every state transition the panel renders.

    The sequence is deliberately mid-run — four completions, one failure,
    two still-running trials — so the golden exercises every branch of
    the left-pane glyph map, the right-pane focused-trial summary, and
    the bottom-bar counters simultaneously.

    The four completions cover all three verdicts a completed trial can
    carry: ``task_a`` / ``task_b`` pass, ``task_f`` fails, and ``task_g``
    carries none at all. ``task_g`` fires no ``judgment_scored`` — an
    ungraded trial has no verdict to publish.

    ``task_f`` and ``task_g`` fire no ``trial_progress``, which is what
    keeps :data:`_BASE_TOTAL_COST` — the hand-written sum of the four
    progress deltas below — equal to the cumulative cost this sequence
    reaches. They also complete *before* ``task_b`` so auto-follow leaves
    focus on a trial with populated token and cost counters.
    """

    display.run_started(total_trials=50, initial_completed=0)
    display.trial_started(trial_id="task_a:0", task_id="task_a", trial_index=0, total_index=0)
    display.trial_progress(
        trial_id="task_a:0",
        prompt_tokens_delta=1200,
        completion_tokens_delta=340,
        cost_delta_usd=0.008,
    )
    display.trial_started(trial_id="task_b:0", task_id="task_b", trial_index=0, total_index=1)
    display.trial_progress(
        trial_id="task_b:0",
        prompt_tokens_delta=41200,
        completion_tokens_delta=6800,
        cost_delta_usd=0.87,
    )
    display.judgment_scored(trial_id="task_a:0", score=0.85, binary_pass=True)
    display.trial_completed(trial_id="task_a:0", binary_pass=True, score=0.85)
    display.trial_started(trial_id="task_c:0", task_id="task_c", trial_index=0, total_index=2)
    display.trial_failed(
        trial_id="task_c:0",
        error="LLMApiTimeoutError",
        retryable=False,
    )
    display.trial_started(trial_id="task_d:0", task_id="task_d", trial_index=0, total_index=3)
    display.trial_progress(
        trial_id="task_d:0",
        prompt_tokens_delta=500,
        completion_tokens_delta=120,
        cost_delta_usd=0.002,
    )
    display.trial_started(trial_id="task_e:0", task_id="task_e", trial_index=0, total_index=4)
    display.trial_progress(
        trial_id="task_e:0",
        prompt_tokens_delta=8200,
        completion_tokens_delta=1450,
        cost_delta_usd=0.14,
    )
    display.trial_started(trial_id="task_f:0", task_id="task_f", trial_index=0, total_index=5)
    display.judgment_scored(trial_id="task_f:0", score=0.10, binary_pass=False)
    display.trial_completed(trial_id="task_f:0", binary_pass=False, score=0.10)
    display.trial_started(trial_id="task_g:0", task_id="task_g", trial_index=0, total_index=6)
    display.trial_completed(trial_id="task_g:0", binary_pass=None, score=None)
    display.judgment_scored(trial_id="task_b:0", score=0.72, binary_pass=True)
    display.trial_completed(trial_id="task_b:0", binary_pass=True, score=0.72)


def _render_svg(
    width: int,
    *,
    cost_budget_usd: float | None = None,
    extra_cost_delta_usd: float = 0.0,
) -> str:
    """Render the panel to SVG.

    ``cost_budget_usd`` — when set, wires the amber@80 % / red@100 % cost
    meter and the bottom-bar cost segment renders in the ``warn`` or
    ``error`` theme token. ``extra_cost_delta_usd`` — extra cumulative
    cost injected via one final ``trial_progress`` after the base event
    sequence so the SVG captures a specific budget-utilisation regime.
    """
    recorder = Console(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        theme=THEME,
    )
    display = LiveRunDisplay(
        refresh_per_second=1000,
        max_trial_rows=20,
        cost_budget_usd=cost_budget_usd,
    )
    _replay_events(display)
    if extra_cost_delta_usd:
        display.trial_progress(
            trial_id="task_b:0",
            prompt_tokens_delta=0,
            completion_tokens_delta=0,
            cost_delta_usd=extra_cost_delta_usd,
        )
    recorder.print(display._build_layout())
    return recorder.export_svg(
        title="tolokaforge run",
        theme=DEFAULT_TERMINAL_THEME,
        unique_id=SVG_UNIQUE_ID,
    )


@pytest.mark.parametrize("width", _WIDTHS, ids=[f"width{w}" for w in _WIDTHS])
def test_run_display_panel_svg(
    request: pytest.FixtureRequest,
    frozen_clock: None,
    width: int,
) -> None:
    """The rendered SVG matches ``panel_{width}.svg`` byte-for-byte."""

    actual = _render_svg(width)
    golden_path = GOLDEN_DIR / f"panel_{width}.svg"

    if request.config.getoption("--update-canon"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return

    assert golden_path.exists(), (
        f"Golden missing: {golden_path.relative_to(GOLDEN_DIR.parent.parent.parent)}. "
        "Run `uv run pytest tests/canonical/test_run_display_goldens.py --update-canon`."
    )
    expected = golden_path.read_text(encoding="utf-8")
    if actual != expected:
        pytest.fail(
            f"SVG golden drift for panel_{width}.svg — re-run with "
            "`--update-canon` if the change is intentional, then review the "
            "diff before committing."
        )


# Base cumulative cost after `_replay_events`: 0.008 (task_a) + 0.87
# (task_b) + 0.002 (task_d) + 0.14 (task_e) = 1.02. To land inside the
# amber band (>= 80 %, < 100 %) at ``cost_budget_usd = 1.5``, aim for
# ~1.30 total → extra 0.28. For the red band (>= 100 %) at the same
# budget, aim for ~1.65 → extra 0.63.
_BASE_TOTAL_COST = 0.008 + 0.87 + 0.002 + 0.14


@pytest.mark.parametrize(
    ("style_label", "extra_cost"),
    [
        ("amber", 1.30 - _BASE_TOTAL_COST),
        ("red", 1.65 - _BASE_TOTAL_COST),
    ],
)
def test_run_display_panel_svg_with_budget(
    request: pytest.FixtureRequest,
    frozen_clock: None,
    style_label: str,
    extra_cost: float,
) -> None:
    """The panel renders the cost segment in ``warn`` / ``error`` when the
    cumulative cost crosses 80 % / 100 % of the budget."""

    width = 80
    cost_budget_usd = 1.5
    actual = _render_svg(width, cost_budget_usd=cost_budget_usd, extra_cost_delta_usd=extra_cost)
    golden_path = GOLDEN_DIR / f"panel_{width}_budget_{style_label}.svg"

    if request.config.getoption("--update-canon"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return

    assert golden_path.exists(), (
        f"Golden missing: {golden_path.relative_to(GOLDEN_DIR.parent.parent.parent)}. "
        "Run `uv run pytest tests/canonical/test_run_display_goldens.py --update-canon`."
    )
    expected = golden_path.read_text(encoding="utf-8")
    if actual != expected:
        pytest.fail(
            f"SVG golden drift for panel_{width}_budget_{style_label}.svg — "
            "re-run with `--update-canon` if the change is intentional, "
            "then review the diff before committing."
        )


# ---------------------------------------------------------------------------
# Boot-window golden: panel state before ``run_started`` with docker records
# buffered — locks the "Boot log" region's byte-level rendering.
# ---------------------------------------------------------------------------

# ``1784205296.0`` renders as ``12:34:56`` UTC on 2026-07-16. Later records
# step forward by whole seconds so the ``HH:MM:SS`` column advances by one
# every row. ``msecs`` values are chosen so each row's ``.mmm`` segment
# differs and the ``int()`` truncation contract is exercised on non-zero
# fractional millis. ``created`` is a wall-clock float independent of the
# ``frozen_clock`` fixture (which patches :func:`live_panel._now`); the
# helper renders these bytes deterministically because it formats
# ``record.created`` in UTC.
_BOOT_LOG_BASE_EPOCH = 1784205296.0

# Widest realistic docker short-name (17 chars) — locks the column so a
# future logger with a shorter or longer name would surface as a golden
# byte-drift. The five records fill the boot-log region at its full
# un-clamped height (``min(5, _BOOT_LOG_MAX_LINES) + 2 == 7`` rows) so the
# golden is diffed against the region's maximum content.
_BOOT_LOG_RECORDS: tuple[tuple[str, float, float, str], ...] = (
    ("tolokaforge.docker.stack", 0.0, 100.0, "starting engine stack"),
    ("tolokaforge.docker.builder", 1.0, 250.0, "Building images for 2 services"),
    ("tolokaforge.docker.container", 2.0, 500.0, "Starting container 'runner'"),
    (
        "tolokaforge.docker.wait_for_services",
        3.0,
        750.0,
        "Waiting for 'runner' to be ready",
    ),
    ("tolokaforge.docker.container", 4.0, 900.0, "Service 'runner' is healthy"),
)


def _replay_boot_state(display: LiveRunDisplay) -> None:
    """Drive ``display`` into the pre-``run_started`` boot window.

    Emits a ``phase_changed`` event (matching the orchestrator's
    ``starting_services`` call site) and pushes a fixed sequence of real
    :class:`logging.LogRecord`s into ``_log_buffer`` with pinned
    ``created`` / ``msecs`` so the rendered ``HH:MM:SS.mmm`` bytes are
    deterministic. Real ``LogRecord``s (not mocks) exercise the same
    ``%``-formatting path :class:`_LogSink` sees in production.
    """
    services: list[ServiceSnapshot] = [
        {"name": "runner", "status": "starting", "ports": {50051: 50051}, "role": "engine"},
        {"name": "db", "status": "starting", "ports": {8000: 8000}, "role": "engine"},
    ]
    display.phase_changed(phase="starting_services", detail="docker compose up", services=services)
    for name, created_offset, msecs, message in _BOOT_LOG_RECORDS:
        display._log_buffer.append(
            logging.makeLogRecord(
                {
                    "name": name,
                    "levelno": logging.INFO,
                    "levelname": "INFO",
                    "msg": message,
                    "args": None,
                    "created": _BOOT_LOG_BASE_EPOCH + created_offset,
                    "msecs": msecs,
                    "pathname": __file__,
                    "lineno": 0,
                }
            )
        )


def _render_boot_log_svg(width: int) -> str:
    """Render the boot-window panel to SVG at ``width`` columns.

    Unlike the mid-run renderer, the boot-window layout activates
    ``border_style="muted"`` on the boot-log panel and the ``muted`` /
    ``cyan`` styles on the phase-spinner bottom bar. Both are semantic
    theme tokens that resolve through :data:`THEME`, so the recorder
    ``Console`` must have that theme installed or Rich raises
    :class:`rich.errors.MissingStyle`.
    """
    recorder = Console(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        theme=THEME,
    )
    display = LiveRunDisplay(refresh_per_second=1000, max_trial_rows=20)
    _replay_boot_state(display)
    recorder.print(display._build_layout())
    return recorder.export_svg(
        title="tolokaforge run",
        theme=DEFAULT_TERMINAL_THEME,
        unique_id=SVG_UNIQUE_ID,
    )


@pytest.mark.parametrize("width", _WIDTHS, ids=[f"width{w}" for w in _WIDTHS])
def test_run_display_boot_log_panel_svg(
    request: pytest.FixtureRequest,
    frozen_clock: None,
    width: int,
) -> None:
    """The boot-window SVG matches ``panel_boot_log_{width}.svg`` byte-for-byte."""

    actual = _render_boot_log_svg(width)
    golden_path = GOLDEN_DIR / f"panel_boot_log_{width}.svg"

    if request.config.getoption("--update-canon"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return

    assert golden_path.exists(), (
        f"Golden missing: {golden_path.relative_to(GOLDEN_DIR.parent.parent.parent)}. "
        "Run `uv run pytest tests/canonical/test_run_display_goldens.py --update-canon`."
    )
    expected = golden_path.read_text(encoding="utf-8")
    if actual != expected:
        pytest.fail(
            f"SVG golden drift for panel_boot_log_{width}.svg — re-run with "
            "`--update-canon` if the change is intentional, then review the "
            "diff before committing."
        )


# ---------------------------------------------------------------------------
# In-flight retry golden — locks the "↻ retry N/5 after Ns (reason)" line
# ---------------------------------------------------------------------------
#
# Elapsed seconds in the ``⏳ waiting on …`` variant are clock-dependent, so
# only the retry frame (clock-free) can be pinned as a byte-level golden. The
# waiting-with-elapsed variant is locked via unit test in
# :file:`tests/unit/test_run_display.py` under a monkey-patched ``_now``.


def _replay_inflight_retry(display: LiveRunDisplay) -> None:
    """Drive ``display`` into an in-flight retry frame on a running trial.

    Emits a minimal sequence ending with ``llm_retry_scheduled`` so the
    focused pane renders the retry line and the model-identity header
    (``agent_model`` is carried on ``trial_started``). The trial stays
    ``running`` — no completion event fires — so the retry state
    survives to render time.
    """
    display.run_started(total_trials=1, initial_completed=0)
    display.trial_started(
        trial_id="task_a:0",
        task_id="task_a",
        trial_index=0,
        total_index=0,
        agent_model="openrouter/anthropic/claude-sonnet-4-6",
        user_model="openrouter/openai/gpt-5.4",
    )
    display.llm_call_started(
        trial_id="task_a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
    )
    display.llm_retry_scheduled(
        trial_id="task_a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=2,
        next_attempt_in_s=8.0,
        reason="APIConnectionError",
    )


def _render_inflight_retry_svg(width: int) -> str:
    recorder = Console(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        theme=THEME,
    )
    display = LiveRunDisplay(refresh_per_second=1000, max_trial_rows=20)
    _replay_inflight_retry(display)
    recorder.print(display._build_layout())
    return recorder.export_svg(
        title="tolokaforge run",
        theme=DEFAULT_TERMINAL_THEME,
        unique_id=SVG_UNIQUE_ID,
    )


@pytest.mark.parametrize("width", _WIDTHS, ids=[f"width{w}" for w in _WIDTHS])
def test_run_display_inflight_retry_svg(
    request: pytest.FixtureRequest,
    frozen_clock: None,
    width: int,
) -> None:
    """The in-flight-retry SVG matches ``panel_inflight_retry_{width}.svg`` byte-for-byte."""

    actual = _render_inflight_retry_svg(width)
    golden_path = GOLDEN_DIR / f"panel_inflight_retry_{width}.svg"

    if request.config.getoption("--update-canon"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return

    assert golden_path.exists(), (
        f"Golden missing: {golden_path.relative_to(GOLDEN_DIR.parent.parent.parent)}. "
        "Run `uv run pytest tests/canonical/test_run_display_goldens.py --update-canon`."
    )
    expected = golden_path.read_text(encoding="utf-8")
    if actual != expected:
        pytest.fail(
            f"SVG golden drift for panel_inflight_retry_{width}.svg — re-run with "
            "`--update-canon` if the change is intentional, then review the "
            "diff before committing."
        )


def test_run_display_module_exports_public_surface() -> None:
    """The public surface of :mod:`tolokaforge.dx.live_panel` is stable.

    ``LiveRunDisplay``, ``RunDisplayEvents``, ``_NULL_EVENTS``, and
    ``_TrialCard`` are the four names Stage 2 wires into the orchestrator,
    conductor, and runner. A silent rename would break every emission
    site — pin them here so the failure surfaces before it can drift.

    ``RunDisplayEvents`` must remain ``runtime_checkable`` — the wiring
    tests rely on ``isinstance(display, RunDisplayEvents)`` and a
    non-runtime-checkable Protocol would raise at that call site.
    """

    from tolokaforge.core.run_display_events import (
        _NULL_EVENTS,
        RunDisplayEvents,
        _NullRunDisplayEvents,
    )
    from tolokaforge.dx.live_panel import (
        LiveRunDisplay,
        _TrialCard,
    )

    assert callable(LiveRunDisplay)
    assert isinstance(_NULL_EVENTS, _NullRunDisplayEvents)
    assert isinstance(_NULL_EVENTS, RunDisplayEvents)
    # `runtime_checkable` sets `_is_runtime_protocol = True` on the class.
    assert getattr(RunDisplayEvents, "_is_runtime_protocol", False), (
        "RunDisplayEvents must be decorated with @runtime_checkable — "
        "orchestrator/conductor wiring tests rely on isinstance checks."
    )
    # `_TrialCard` is reachable for tests / follow-up displays.
    assert callable(_TrialCard)
