"""Unit — :func:`build_capabilities` reads the numeric knobs from the preset.

The additive numeric slots on :class:`ModelCapabilities` land through the
same overlay-driven ``cfg.get(...)`` path in :func:`build_capabilities`. This
file pins that ``tool_output_max_chars`` is read from the effective preset
block, and that an omitted key resolves to the ``None`` default so every
existing preset stays on the pre-opt-in baseline.

The single-purpose canonical file
``tests/canonical/test_tool_output_max_chars_preset_routing.py`` pins the
bundled preset that opts in; here we only assert the plumbing.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.presets import build_capabilities, set_overlay_path

pytestmark = pytest.mark.unit


def test_build_capabilities_reads_tool_output_max_chars(
    write_overlay,
    overlay_isolation,
) -> None:
    path = write_overlay(
        {
            "presets": {
                "cap_reader_probe": {
                    "match": ["capreader/*"],
                    "tool_output_max_chars": 16384,
                }
            }
        }
    )
    set_overlay_path(path)
    caps = build_capabilities("capreader/foo-1")
    assert caps.tool_output_max_chars == 16384


def test_build_capabilities_default_tool_output_max_chars_is_none(
    write_overlay,
    overlay_isolation,
) -> None:
    path = write_overlay(
        {
            "presets": {
                "no_cap_probe": {
                    "match": ["nocap/*"],
                }
            }
        }
    )
    set_overlay_path(path)
    caps = build_capabilities("nocap/foo-1")
    assert caps.tool_output_max_chars is None
