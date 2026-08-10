"""Canonical test — the bundled ``model_presets.yaml`` conforms to the same
overlay validator operators run against their own preset files.

``_load_bundled_presets`` reads the wheel-bundled YAML without running it
through ``_validate_overlay`` (validation is skipped at runtime because the
file is a build-time artefact, not operator input). Locking that invariant at
test time gives the same fail-loud guarantee: a typo in a ``params:`` key or a
missing ``name:`` in a ``{name, params}`` slot in the shipped file breaks this
test rather than a downstream capability-construction call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.llm.presets import _load_bundled_presets, _validate_overlay

pytestmark = pytest.mark.canonical


def test_bundled_model_presets_pass_overlay_validation() -> None:
    bundled_path = (
        Path(__file__).parent.parent.parent / "tolokaforge" / "core" / "data" / "model_presets.yaml"
    )
    data = _load_bundled_presets()
    _validate_overlay(data, str(bundled_path))
