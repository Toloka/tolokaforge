"""Bootstrap lock for the sibling ``tolokaforge_models`` wheel.

The wheel starts life carrying byte-identical copies of the three
model-data files still shipped by the engine. This test freezes that
byte-identity invariant plus the three module-level constants
(``__version__``, ``__api_version__``, ``minimum_engine_version``)
against the engine's installed ``[project].version``. Any drift on
either surface breaks the milestone's byte-identical acceptance
test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import tomllib
from packaging.specifiers import SpecifierSet
from packaging.version import Version

import tolokaforge_models

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_DATA_ROOT = _REPO_ROOT / "tolokaforge" / "core" / "data"
_MODELS_DATA_ROOT = _REPO_ROOT / "tolokaforge_models" / "data"

_DATA_FILES = ("pricing.json", "model_presets.yaml", "providers.yaml")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("basename", _DATA_FILES)
def test_models_data_file_matches_engine_byte_for_byte(basename: str) -> None:
    engine_hash = _sha256(_ENGINE_DATA_ROOT / basename)
    models_hash = _sha256(_MODELS_DATA_ROOT / basename)
    assert engine_hash == models_hash, (
        f"{basename} drifted between the engine copy and the tolokaforge_models copy — "
        f"engine={engine_hash} models={models_hash}"
    )


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
