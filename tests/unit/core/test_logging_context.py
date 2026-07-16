"""Unit tests for :mod:`tolokaforge.core.logging_context`.

Lock the ``ContextVar`` set/reset contract that the panel's per-trial log
view relies on: the value is visible inside the scope, restored on exit, and
the reset uses a token so nested scopes unwind to their enclosing trial.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.logging_context import TRIAL_ID_CTXVAR, trial_id_scope

pytestmark = pytest.mark.unit


def test_trial_id_scope_sets_and_resets_ctxvar() -> None:
    assert TRIAL_ID_CTXVAR.get() is None
    with trial_id_scope("a:0"):
        assert TRIAL_ID_CTXVAR.get() == "a:0"
    assert TRIAL_ID_CTXVAR.get() is None

    # Reset holds even when the body raises (try/finally shape).
    with pytest.raises(RuntimeError, match="boom"):
        with trial_id_scope("a:1"):
            assert TRIAL_ID_CTXVAR.get() == "a:1"
            raise RuntimeError("boom")
    assert TRIAL_ID_CTXVAR.get() is None


def test_trial_id_scope_supports_nesting() -> None:
    with trial_id_scope("a:0"):
        assert TRIAL_ID_CTXVAR.get() == "a:0"
        with trial_id_scope("b:0"):
            assert TRIAL_ID_CTXVAR.get() == "b:0"
        # Exiting the inner scope restores the enclosing trial, not None —
        # this is why the reset uses a token rather than set(None).
        assert TRIAL_ID_CTXVAR.get() == "a:0"
    assert TRIAL_ID_CTXVAR.get() is None
