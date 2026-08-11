"""Locks the loud-fail contract on
:func:`tolokaforge.core.llm.presets._load_bundled_presets`.

The consumer's job is to reject empty or non-mapping content in the bundled
``model_presets.yaml``. The accessor already raises
:class:`FileNotFoundError` when the file is absent; that error propagates
through the consumer unchanged.

Tests redirect the accessor at a scratch tree via ``monkeypatch`` on
``tolokaforge.core.model_data._DATA_ROOT`` — the internal seam the
accessors resolve their targets under.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core import model_data as md
from tolokaforge.core.llm.presets import _load_bundled_presets

pytestmark = pytest.mark.unit


def test_load_bundled_presets_raises_file_not_found_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing bundled preset file surfaces the accessor's raise."""
    monkeypatch.setattr(md, "_DATA_ROOT", tmp_path)

    with pytest.raises(FileNotFoundError, match="model_presets.yaml"):
        _load_bundled_presets()


def test_load_bundled_presets_raises_value_error_on_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty YAML file must raise :class:`ValueError` at the consumer layer."""
    (tmp_path / "model_presets.yaml").write_text("")
    monkeypatch.setattr(md, "_DATA_ROOT", tmp_path)

    with pytest.raises(ValueError, match="is empty"):
        _load_bundled_presets()


def test_load_bundled_presets_raises_value_error_on_non_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A YAML list at the top level must raise :class:`ValueError`."""
    (tmp_path / "model_presets.yaml").write_text("- a\n- b\n")
    monkeypatch.setattr(md, "_DATA_ROOT", tmp_path)

    with pytest.raises(ValueError, match="must be a mapping"):
        _load_bundled_presets()
