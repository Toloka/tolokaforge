"""Locks the accessor-layer contract of
:mod:`tolokaforge.core.model_data`.

The three accessors expose the bundled model-data files' on-disk
locations. Their responsibility split:

* Accessor guarantees the file **exists** (raises
  :class:`FileNotFoundError` otherwise).
* Consumers guarantee the file **parses and is non-empty** (they raise
  :class:`ValueError` on empty / malformed content).

An empty-but-present file is not a supported install shape, but only
the consumer layer raises on it — the accessor stays cheap.

Tests redirect the accessors at a scratch tree via ``monkeypatch`` on
the module-level ``_DATA_ROOT`` constant, which is the internal seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core import model_data as md

pytestmark = pytest.mark.unit


def test_bundled_pricing_path_returns_path_when_file_exists() -> None:
    path = md.bundled_pricing_path()

    assert isinstance(path, Path)
    assert path.name == "pricing.json"
    assert path.is_file()


def test_bundled_presets_path_returns_path_when_file_exists() -> None:
    path = md.bundled_presets_path()

    assert isinstance(path, Path)
    assert path.name == "model_presets.yaml"
    assert path.is_file()


def test_bundled_providers_path_returns_path_when_file_exists() -> None:
    path = md.bundled_providers_path()

    assert isinstance(path, Path)
    assert path.name == "providers.yaml"
    assert path.is_file()


def test_bundled_pricing_path_raises_file_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(md, "_DATA_ROOT", tmp_path)

    with pytest.raises(FileNotFoundError, match="pricing.json"):
        md.bundled_pricing_path()


def test_bundled_presets_path_raises_file_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(md, "_DATA_ROOT", tmp_path)

    with pytest.raises(FileNotFoundError, match="model_presets.yaml"):
        md.bundled_presets_path()


def test_bundled_providers_path_raises_file_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(md, "_DATA_ROOT", tmp_path)

    with pytest.raises(FileNotFoundError, match="providers.yaml"):
        md.bundled_providers_path()


def test_bundled_pricing_path_returns_empty_file_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty-but-present is the consumer's concern, not the accessor's."""
    (tmp_path / "pricing.json").write_text("")
    monkeypatch.setattr(md, "_DATA_ROOT", tmp_path)

    path = md.bundled_pricing_path()

    assert path == tmp_path / "pricing.json"
    assert path.is_file()
