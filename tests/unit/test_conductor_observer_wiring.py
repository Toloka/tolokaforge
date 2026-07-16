"""Wiring tests for :attr:`ConductorContext.observer_provider` /
:attr:`InProcessConductor.observer_provider` — M1 sub-3.

Covers the shape of the wiring and its no-op default. The end-to-end path
where an observer sees real turn events lives in
``tests/unit/session/test_loop_observer.py`` (M1 sub-2); here we only
prove the plumbing threads through without regressing the sealed default.

* Sealed default (``observer_provider=None``) → :attr:`TrialRunner.loop_observer`
  is ``None``. Existing behavior byte-identical.
* Custom provider returning ``None`` for this trial → also None. Provider is
  called per-trial with the trial_id.
* Custom provider returning an observer → :attr:`TrialRunner.loop_observer`
  is that observer.
* ``observer_provider`` field survives ``**vars(ctx)`` unpacking into
  :class:`InProcessConductor` — the "1:1 kwarg parity" invariant the class
  docstring names.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import ConductorContext, InProcessConductor
from tolokaforge.core.loop import LoopObserver

pytestmark = pytest.mark.unit


class _RecordingObserver:
    """Trivial :class:`LoopObserver` that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def on_turn_start(self, turn_index: int) -> None:
        self.calls.append(("turn_start", (turn_index,), {}))

    def on_assistant_message(self, content: str, has_reasoning: bool) -> None:
        self.calls.append(("assistant", (content, has_reasoning), {}))

    def on_tool_call(self, call_id, tool_name, arguments) -> None:
        self.calls.append(("tool_call", (call_id, tool_name), {}))

    def on_tool_result(self, call_id, tool_name, duration_ms, output, success) -> None:
        self.calls.append(("tool_result", (call_id, tool_name), {}))

    def on_terminal(self, status, termination_reason) -> None:
        self.calls.append(("terminal", (status,), {}))


def _mock_ctx(**overrides) -> ConductorContext:
    """Build a :class:`ConductorContext` with mocked dependencies for
    plumbing tests. Nothing here reaches the trial body — we only exercise
    the observer-provider seam.
    """
    defaults: dict = {
        "adapter": MagicMock(),
        "artifact_writer": MagicMock(),
        "config": MagicMock(),
        "logger": MagicMock(),
        "verbose": False,
        "strict": False,
        "agent_client": MagicMock(),
        "runtime_backend": MagicMock(),
        "trial_grader": MagicMock(),
        "output_dir": Path("/tmp/does-not-matter"),
        "request_limiter": None,
    }
    defaults.update(overrides)
    return ConductorContext(**defaults)


class TestConductorContextObserverProviderField:
    def test_default_is_none(self):
        ctx = _mock_ctx()
        assert ctx.observer_provider is None

    def test_field_survives_vars_unpack_into_conductor(self):
        """The ``**vars(ctx)`` unpack path the orchestrator uses must round-trip
        the new field. Load-bearing per :class:`InProcessConductor`'s docstring.
        """
        observer = _RecordingObserver()

        def provider(trial_id: str) -> LoopObserver | None:
            return observer

        ctx = _mock_ctx(observer_provider=provider)
        conductor = InProcessConductor(**vars(ctx))
        assert conductor.observer_provider is provider


class TestMaybeBuildLoopObserver:
    def test_no_provider_returns_none(self):
        ctx = _mock_ctx()
        conductor = InProcessConductor(**vars(ctx))
        assert conductor._maybe_build_loop_observer("t:0") is None

    def test_provider_returning_none_returns_none(self):
        ctx = _mock_ctx(observer_provider=lambda trial_id: None)
        conductor = InProcessConductor(**vars(ctx))
        assert conductor._maybe_build_loop_observer("t:0") is None

    def test_provider_receives_trial_id_and_returns_observer(self):
        received: list[str] = []
        observer = _RecordingObserver()

        def provider(trial_id: str) -> LoopObserver | None:
            received.append(trial_id)
            return observer

        ctx = _mock_ctx(observer_provider=provider)
        conductor = InProcessConductor(**vars(ctx))
        result = conductor._maybe_build_loop_observer("MAN-34:0")
        assert result is observer
        assert received == ["MAN-34:0"]

    def test_provider_can_return_different_observer_per_trial(self):
        """Two different trial_ids may map to different observers, matching
        the "per-trial session" model on which the Open Agent Loop gate
        depends.
        """
        obs_a = _RecordingObserver()
        obs_b = _RecordingObserver()

        def provider(trial_id: str) -> LoopObserver | None:
            return obs_a if trial_id.startswith("A") else obs_b

        ctx = _mock_ctx(observer_provider=provider)
        conductor = InProcessConductor(**vars(ctx))
        assert conductor._maybe_build_loop_observer("A:0") is obs_a
        assert conductor._maybe_build_loop_observer("B:0") is obs_b


class TestObserverProviderIsExtensionPoint:
    """Extensibility check: the observer type is deliberately generic — a
    non-session observer plugs in the same way a session-based one does.
    Regression guard against future changes that would smuggle
    session-specific coupling into the sealed conductor.
    """

    def test_conductor_accepts_any_loop_observer_no_session_import_required(self):
        """The sealed conductor must not require importing
        :mod:`tolokaforge.session` to use ``observer_provider``. A plain
        :class:`LoopObserver` (not a :class:`SessionLoopObserver`) is
        accepted end-to-end.
        """
        observer = _RecordingObserver()
        ctx = _mock_ctx(observer_provider=lambda tid: observer)
        conductor = InProcessConductor(**vars(ctx))

        # Verify the provider round-trips without invoking anything that
        # would pull tolokaforge.session; the observer we hand in doesn't
        # know about session events at all.
        got = conductor._maybe_build_loop_observer("t:0")
        assert got is observer
        assert not hasattr(got, "_session")  # not a SessionLoopObserver
