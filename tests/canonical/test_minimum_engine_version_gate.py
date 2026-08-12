"""Lock the ``minimum_engine_version`` install-time gate.

:func:`tolokaforge.core.model_data._check_minimum_engine_version` refuses
to boot when the installed engine version does not satisfy the
:mod:`tolokaforge_models` wheel's ``minimum_engine_version`` specifier.
The gate is the second half of the install-time validation contract
(the first being the missing-wheel branch — see
``test_models_wheel_absent.py``).

Two behaviour branches:

* The models wheel declares a floor the installed engine cannot meet →
  :class:`RuntimeError` naming both versions.
* The floor is satisfiable (real bundled ``minimum_engine_version``) →
  silent success.

The engine version is resolved via
:func:`tolokaforge.core.model_data._resolve_engine_version`, which tries
the ``tolokaforge`` distribution first and falls back to
``tolokaforge-runner-subset``; a third test locks that fallback branch
so the subset image path stays covered.
"""

from __future__ import annotations

import importlib.metadata

import pytest

import tolokaforge_models
from tolokaforge.core import model_data as md

pytestmark = pytest.mark.canonical


def test_engine_below_floor_raises_naming_both_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tolokaforge_models, "minimum_engine_version", ">=999,<1000", raising=True)

    with pytest.raises(RuntimeError) as excinfo:
        md._check_minimum_engine_version()

    message = str(excinfo.value)
    assert ">=999,<1000" in message, message
    assert tolokaforge_models.__version__ in message, message
    installed = md._resolve_engine_version()
    assert installed in message, message


def test_engine_meets_floor_returns_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tolokaforge_models, "minimum_engine_version", ">=0.1", raising=True)

    md._check_minimum_engine_version()


def test_engine_version_falls_back_to_runner_subset_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subset image has no ``tolokaforge`` distribution installed, only
    ``tolokaforge-runner-subset``. The resolver must find it via the fallback
    candidate rather than defaulting to ``0.0.0+unknown``."""
    real_version = importlib.metadata.version

    def _hide_base_wheel(name: str) -> str:
        if name == "tolokaforge":
            raise importlib.metadata.PackageNotFoundError(name)
        if name == "tolokaforge-runner-subset":
            return "0.17.0"
        return real_version(name)

    monkeypatch.setattr(importlib.metadata, "version", _hide_base_wheel)

    assert md._resolve_engine_version() == "0.17.0"


def test_engine_version_raises_when_no_candidate_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _hide_all(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _hide_all)

    with pytest.raises(RuntimeError, match="engine version cannot be resolved"):
        md._resolve_engine_version()
