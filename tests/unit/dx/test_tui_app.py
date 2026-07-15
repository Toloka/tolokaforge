"""Behaviour tests for :class:`tolokaforge.dx.tui.TextualRunApp`.

Drives the app with :meth:`textual.app.App.run_test` and asserts widget
state after firing :class:`RunDisplayEvents` methods. No screenshots —
Textual screen-diffs are higher-flake than Rich SVG goldens; behaviour
assertions are the contract.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
from textual.widgets import DataTable, ListItem, Static, TabbedContent

from tolokaforge.core.logging import _TOLOKAFORGE_ROOT_HANDLER_SENTINEL
from tolokaforge.core.run_display_events import (
    ContainerSnapshot,
    RunDisplayEvents,
    ServiceSnapshot,
)
from tolokaforge.dx._display import DisplayMode
from tolokaforge.dx.live_panel import LiveRunDisplay
from tolokaforge.dx.tui import (
    FocusedTrialView,
    HelpScreen,
    RunStatusBar,
    TextualRunApp,
    TrialListView,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Protocol shape + entry-gate wiring
# ---------------------------------------------------------------------------


def test_textual_app_satisfies_run_display_events_protocol() -> None:
    assert isinstance(TextualRunApp(), RunDisplayEvents)


def test_for_mode_full_returns_textual_app() -> None:
    ctx = LiveRunDisplay.for_mode(DisplayMode.FULL)
    assert isinstance(ctx, TextualRunApp)


def test_for_mode_full_falls_back_to_rich_when_textual_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate `import tolokaforge.dx.tui` failing.

    Uses the ``sys.modules`` sentinel trick: stashing ``None`` at the
    dotted key makes any ``import tolokaforge.dx.tui`` raise
    :class:`ImportError`. ``for_mode`` catches, logs at WARNING, and
    returns the Rich :class:`LiveRunDisplay` — verified below.
    """
    import sys

    monkeypatch.setitem(sys.modules, "tolokaforge.dx.tui", None)
    ctx = LiveRunDisplay.for_mode(DisplayMode.FULL)
    assert isinstance(ctx, LiveRunDisplay)
    assert not isinstance(ctx, TextualRunApp)


# ---------------------------------------------------------------------------
# Event-to-state — verified without run_test (synchronous state mutation)
# ---------------------------------------------------------------------------


def test_events_before_mount_buffer_into_pending() -> None:
    app = TextualRunApp()
    app.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    app.trial_progress(
        trial_id="a:0",
        prompt_tokens_delta=100,
        completion_tokens_delta=50,
        cost_delta_usd=0.01,
    )
    # Before mount, events land in `_pending` — no mutations to `_trials` yet.
    assert len(app._pending) == 2
    assert app._trials == {}


def test_kwarg_only_enforcement() -> None:
    app = TextualRunApp()
    with pytest.raises(TypeError):
        app.trial_started("x:0", "x", 0, 0)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Pilot-driven behaviour tests
# ---------------------------------------------------------------------------


async def test_run_started_paints_status_bar() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        app.run_started(total_trials=10, initial_completed=0)
        await pilot.pause()
        status = app.query_one("#status", RunStatusBar)
        text = str(status.render())
        assert "0/10" in text


async def test_trial_started_populates_trial_list() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        app.run_started(total_trials=3, initial_completed=0)
        app.trial_started(trial_id="a:0", task_id="task_a", trial_index=0, total_index=0)
        app.trial_started(trial_id="b:0", task_id="task_b", trial_index=0, total_index=1)
        app.trial_started(trial_id="c:0", task_id="task_c", trial_index=0, total_index=2)
        await pilot.pause()
        view = app.query_one("#trials", TrialListView)
        items = [item for item in view.children if isinstance(item, ListItem)]
        assert len(items) == 3
        assert view.index == 2  # focus follows the newest trial_started


async def test_j_key_moves_selection_down() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        app.run_started(total_trials=5, initial_completed=0)
        for i in range(5):
            app.trial_started(trial_id=f"t{i}:0", task_id=f"t{i}", trial_index=0, total_index=i)
        await pilot.pause()
        view = app.query_one("#trials", TrialListView)
        # Reset cursor to the top for a deterministic keypress test.
        view.index = 0
        await pilot.pause()
        view.focus()
        await pilot.press("j")
        await pilot.pause()
        assert view.index == 1
        await pilot.press("j")
        await pilot.pause()
        assert view.index == 2


async def test_k_key_moves_selection_up() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        app.run_started(total_trials=5, initial_completed=0)
        for i in range(5):
            app.trial_started(trial_id=f"t{i}:0", task_id=f"t{i}", trial_index=0, total_index=i)
        await pilot.pause()
        view = app.query_one("#trials", TrialListView)
        view.index = 3
        await pilot.pause()
        view.focus()
        await pilot.press("k")
        await pilot.pause()
        assert view.index == 2


async def test_numeric_keys_switch_tabs() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        tabs = app.query_one("#tabs", TabbedContent)
        assert tabs.active == "overview"
        await pilot.press("2")
        await pilot.pause()
        assert tabs.active == "logs"
        await pilot.press("3")
        await pilot.pause()
        assert tabs.active == "services"
        await pilot.press("4")
        await pilot.pause()
        assert tabs.active == "infra"
        await pilot.press("5")
        await pilot.pause()
        assert tabs.active == "errors"
        await pilot.press("1")
        await pilot.pause()
        assert tabs.active == "overview"


async def test_l_key_jumps_to_logs_tab() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        tabs = app.query_one("#tabs", TabbedContent)
        await pilot.press("l")
        await pilot.pause()
        assert tabs.active == "logs"


async def test_help_screen_opens_and_closes() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


async def test_phase_changed_populates_services_tab() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        services: list[ServiceSnapshot] = [
            {"name": "runner", "status": "healthy", "ports": {50051: 50051}, "role": "engine"},
            {"name": "db-service", "status": "starting", "ports": {8000: 8000}, "role": "engine"},
        ]
        app.phase_changed(phase="services_ready", detail="docker compose up", services=services)
        await pilot.pause()
        table = app.query_one("#services-body", DataTable)
        assert table.row_count == 2


async def test_trial_provisioned_populates_infra_tab_for_focused_trial() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        app.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
        containers: list[ContainerSnapshot] = [
            {
                "name": "trial-runner",
                "service": "runner",
                "state": "running",
                "health": "healthy",
                "ports": {50051: 50051},
            },
            {
                "name": "trial-db",
                "service": "db",
                "state": "running",
                "health": "healthy",
                "ports": {5432: 5432},
            },
        ]
        app.trial_provisioned(
            trial_id="a:0",
            containers=containers,
            endpoints={"runner": "http://localhost:50051"},
        )
        await pilot.pause()
        table = app.query_one("#infra-body", DataTable)
        assert table.row_count == 2


async def test_trial_failed_shows_error_in_focused_pane() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        app.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
        app.trial_failed(trial_id="a:0", error="LLMApiTimeoutError: timeout", retryable=False)
        await pilot.pause()
        pane = app.query_one("#focused", FocusedTrialView)
        rendered = str(pane.render())
        assert "LLMApiTimeoutError" in rendered
        assert "failed" in rendered.lower()


async def test_auth_shaped_failure_populates_overview_banner() -> None:
    app = TextualRunApp()
    async with app.run_test() as pilot:
        app.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
        app.trial_failed(
            trial_id="a:0",
            error="openai.AuthenticationError: Invalid API key",
            retryable=False,
        )
        await pilot.pause()
        overview = app.query_one("#overview-body", Static)
        rendered = str(overview.render())
        assert "Auth failure" in rendered


# ---------------------------------------------------------------------------
# Log-sink integration — LOGS + ERRORS tabs feed off the same buffer
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_root_handlers() -> object:
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    try:
        yield None
    finally:
        root.handlers = saved


def _make_log_record(level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="tolokaforge.probe",
        level=level,
        pathname=__file__,
        lineno=0,
        msg=message,
        args=None,
        exc_info=None,
    )


async def test_log_records_flow_into_logs_and_errors_tabs(
    _clean_root_handlers: object,
) -> None:
    fake_stream = io.StringIO()
    handler = logging.StreamHandler(fake_stream)
    setattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    root = logging.getLogger()
    root.addHandler(handler)

    app = TextualRunApp()
    app._install_log_sink()
    probe = logging.getLogger("tolokaforge.probe")
    original_level = probe.level
    probe.setLevel(logging.DEBUG)
    try:
        async with app.run_test() as pilot:
            probe.warning("routed warning")
            probe.info("routed info")
            # Force a refresh — RichLog writes happen in `_refresh_ui`.
            app._apply_event("run_started", {"total_trials": 1, "initial_completed": 0})
            await pilot.pause()
            records = app.log_records()
            assert any(r.getMessage() == "routed warning" for r in records)
            assert any(r.getMessage() == "routed info" for r in records)
    finally:
        probe.setLevel(original_level)
        app._restore_log_sink()


# ---------------------------------------------------------------------------
# Context-manager wiring — matches LiveRunDisplay.for_mode consumers
# ---------------------------------------------------------------------------


def test_events_property_returns_self() -> None:
    app = TextualRunApp()
    assert app.events is app


def test_run_finished_sets_finished_flag() -> None:
    app = TextualRunApp()
    app.run_finished(output_dir=Path("/tmp/run"))
    # Pre-mount: state lives in _pending; the flag flips once drained.
    # Verify the payload is queued.
    kinds = [k for k, _ in app._pending]
    assert "run_finished" in kinds
