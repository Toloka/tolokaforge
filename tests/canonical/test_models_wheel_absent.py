"""Lock the ``tolokaforge-models`` missing-install failure surface.

:func:`tolokaforge.core.model_data._check_minimum_engine_version` is the
install-time gate the engine fires at
:mod:`tolokaforge.core.llm.presets` import. When
:mod:`tolokaforge_models` is not importable — a user who ran
``pip install tolokaforge`` without letting the transitive dep resolve,
or an editable checkout with a broken workspace — the gate must raise
:class:`RuntimeError` naming the actionable install command
(``pip install tolokaforge-models``), not the raw ``ImportError`` the
naked ``import tolokaforge_models`` would surface.

Because ``tolokaforge-models`` is a hard dep of ``tolokaforge`` the
ambient venv always has it installed; ``sys.modules["tolokaforge_models"]
= None`` is the standard idiom for making a subsequent
``import tolokaforge_models`` fail with :class:`ImportError` even when
the module is otherwise installable (Python's import machinery treats a
``None`` entry as a "known missing" sentinel).
"""

from __future__ import annotations

import sys

import pytest

from tolokaforge.core.model_data import _check_minimum_engine_version

pytestmark = pytest.mark.canonical


def test_missing_models_wheel_raises_actionable_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "tolokaforge_models", None)

    with pytest.raises(RuntimeError) as excinfo:
        _check_minimum_engine_version()

    message = str(excinfo.value)
    assert "pip install tolokaforge-models" in message, message
    assert "tolokaforge-models >= 1.0.0" in message, message
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_present_models_wheel_does_not_raise() -> None:
    """The happy path — the installed models wheel version satisfies the
    engine floor, so the check is silent."""
    _check_minimum_engine_version()
