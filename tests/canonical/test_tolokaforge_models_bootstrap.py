"""Bootstrap lock for the sibling ``tolokaforge_models`` wheel.

Freezes the three module-level constants (``__version__``,
``__api_version__``, ``minimum_engine_version``) against the engine's
installed ``[project].version``. Any drift on either surface breaks the
milestone's byte-identical acceptance test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib
from packaging.specifiers import SpecifierSet
from packaging.version import Version

import tolokaforge_models

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_models_wheel_constants_match_declared_contract() -> None:
    assert tolokaforge_models.__version__ == "1.0.0"
    assert tolokaforge_models.__api_version__ == 1
    assert tolokaforge_models.minimum_engine_version == ">=0.17,<0.18"


def test_models_minimum_engine_version_contains_installed_engine() -> None:
    engine_pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    engine_version = Version(engine_pyproject["project"]["version"])
    specifier = SpecifierSet(tolokaforge_models.minimum_engine_version)
    assert engine_version in specifier, (
        f"engine version {engine_version} falls outside tolokaforge_models.minimum_engine_version "
        f"{tolokaforge_models.minimum_engine_version!r}"
    )
