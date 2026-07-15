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
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console
from rich.terminal_theme import DEFAULT_TERMINAL_THEME

from tolokaforge.dx import live_panel
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
    """Fifteen events covering every state transition the panel renders.

    The sequence is deliberately mid-run — two completions, one failure,
    two still-running trials — so the golden exercises every branch of
    the left-pane glyph map, the right-pane focused-trial summary, and
    the bottom-bar counters simultaneously.
    """

    display.run_started(total_trials=50, initial_completed=0)
    display.trial_started(trial_id="task_a:0", task_id="task_a", trial_index=0)
    display.trial_progress(
        trial_id="task_a:0",
        prompt_tokens_delta=1200,
        completion_tokens_delta=340,
        cost_delta_usd=0.008,
    )
    display.trial_started(trial_id="task_b:0", task_id="task_b", trial_index=0)
    display.trial_progress(
        trial_id="task_b:0",
        prompt_tokens_delta=41200,
        completion_tokens_delta=6800,
        cost_delta_usd=0.87,
    )
    display.judgment_scored(trial_id="task_a:0", score=0.85, binary_pass=True)
    display.trial_completed(trial_id="task_a:0", binary_pass=True, score=0.85)
    display.trial_started(trial_id="task_c:0", task_id="task_c", trial_index=0)
    display.trial_failed(
        trial_id="task_c:0",
        error="LLMApiTimeoutError",
        retryable=False,
    )
    display.trial_started(trial_id="task_d:0", task_id="task_d", trial_index=0)
    display.trial_progress(
        trial_id="task_d:0",
        prompt_tokens_delta=500,
        completion_tokens_delta=120,
        cost_delta_usd=0.002,
    )
    display.trial_started(trial_id="task_e:0", task_id="task_e", trial_index=0)
    display.trial_progress(
        trial_id="task_e:0",
        prompt_tokens_delta=8200,
        completion_tokens_delta=1450,
        cost_delta_usd=0.14,
    )
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
