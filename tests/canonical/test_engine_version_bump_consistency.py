"""Locks agreement between the engine wheel version and the installed
:mod:`tolokaforge_models` wheel's ``minimum_engine_version``.

Two invariants:

* ``[project].version`` in ``pyproject.toml`` is at least the current
  minor-series floor (``0.17.0``) — the engine cannot drift below the
  version the models-wheel cutover targets.
* The engine version satisfies
  :data:`tolokaforge_models.minimum_engine_version` — the installed
  models wheel and engine wheel must agree at ``uv sync`` time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

import tolokaforge_models

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


def test_engine_version_satisfies_models_wheel_floor() -> None:
    engine_version = _load_engine_version()
    floor = SpecifierSet(tolokaforge_models.minimum_engine_version)

    assert engine_version in floor, (
        f"engine [project].version {engine_version} does not satisfy "
        f"tolokaforge_models.minimum_engine_version="
        f"{tolokaforge_models.minimum_engine_version!r}; the installed "
        f"models wheel would refuse to resolve against the engine wheel"
    )
