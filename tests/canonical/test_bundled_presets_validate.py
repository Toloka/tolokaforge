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

import pytest

from tolokaforge.core.llm.presets import _load_bundled_presets, _validate_overlay
from tolokaforge.core.model_data import bundled_presets_path

pytestmark = pytest.mark.canonical


def test_bundled_model_presets_pass_overlay_validation() -> None:
    data = _load_bundled_presets()
    _validate_overlay(data, str(bundled_presets_path()))
