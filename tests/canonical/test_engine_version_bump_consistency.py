"""Locks agreement between the engine wheel version and the pre-cutover
``MODELS_MINIMUM_ENGINE_VERSION`` sentinel range.

Two invariants:

* ``[project].version`` in ``pyproject.toml`` is at least the current
  minor-series floor (``0.17.0``) — the engine cannot drift below the
  version the models-wheel cutover targets.
* The engine version satisfies
  :data:`tolokaforge.core.model_data.MODELS_MINIMUM_ENGINE_VERSION` — a
  models-wheel installed against this engine's declared sentinel range
  must resolve at ``uv sync``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from tolokaforge.core.model_data import MODELS_MINIMUM_ENGINE_VERSION

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]

_ENGINE_FLOOR = Version("0.17.0")


def _load_engine_version() -> Version:
    """Return the ``[project].version`` declared in ``pyproject.toml``."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # Python 3.10 branch
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    return Version(data["project"]["version"])


def test_engine_version_meets_minor_series_floor() -> None:
    engine_version = _load_engine_version()

    assert engine_version >= _ENGINE_FLOOR, (
        f"engine [project].version {engine_version} is below the current "
        f"minor-series floor {_ENGINE_FLOOR}"
    )


def test_engine_version_satisfies_minimum_engine_version_sentinel() -> None:
    engine_version = _load_engine_version()
    sentinel = SpecifierSet(MODELS_MINIMUM_ENGINE_VERSION)

    assert engine_version in sentinel, (
        f"engine [project].version {engine_version} does not satisfy "
        f"MODELS_MINIMUM_ENGINE_VERSION={MODELS_MINIMUM_ENGINE_VERSION!r}; "
        f"a models-wheel pinning this sentinel would refuse to resolve "
        f"against the engine wheel"
    )
