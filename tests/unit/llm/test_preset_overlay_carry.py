"""Carrying a preset overlay by content rather than by file path.

The single-box CLI installs the operator's ``engine.presets_file`` overlay
process-globally through :func:`set_overlay_path` (a file read). A detached
library trial unit runs in another process with no such file, so the overlay
travels as data and installs through :func:`set_overlay_data`. These pin that
the content install merges identically to the path install, validates its
input, and takes precedence when both are set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.llm.presets import (
    build_capabilities,
    get_overlay_data,
    resolve_policy_names,
    set_overlay_data,
    set_overlay_path,
)

pytestmark = pytest.mark.unit

# An overlay that flips the ``content_policy`` of one made-up model family from
# the bundled default (``openai``) to ``anthropic`` — a change the policy-name
# fingerprint reports, so a lookup proves whether the overlay is active.
_OVERLAY: dict[str, Any] = {
    "presets": {
        "carry_demo": {
            "match": ["carry-demo/*"],
            "content_policy": "anthropic",
        }
    }
}


def _content_policy() -> str:
    return resolve_policy_names(build_capabilities("carry-demo/model", "carry"))["content_policy"]


def test_bundled_default_without_any_overlay() -> None:
    assert _content_policy() == "openai"


def test_content_install_applies_the_overlay() -> None:
    set_overlay_data(_OVERLAY)
    assert _content_policy() == "anthropic"


def test_clearing_the_content_install_returns_to_the_bundled_defaults() -> None:
    set_overlay_data(_OVERLAY)
    assert _content_policy() == "anthropic"
    set_overlay_data(None)
    assert _content_policy() == "openai"


def test_get_overlay_data_round_trips_the_installed_content() -> None:
    assert get_overlay_data() is None
    set_overlay_data(_OVERLAY)
    assert get_overlay_data() == _OVERLAY


def test_content_install_matches_the_file_path_install(tmp_path: Path) -> None:
    """Parity: the same overlay installed as content or as a file merges identically."""
    set_overlay_data(_OVERLAY)
    via_data = _content_policy()
    set_overlay_data(None)

    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(_OVERLAY))
    set_overlay_path(str(overlay_file))
    via_path = _content_policy()

    assert via_data == via_path == "anthropic"


def test_carried_content_wins_over_a_path_overlay(tmp_path: Path) -> None:
    """When both are set, the by-value payload a node carries takes precedence."""
    path_overlay = {
        "presets": {"carry_demo": {"match": ["carry-demo/*"], "content_policy": "nova"}}
    }
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(path_overlay))
    set_overlay_path(str(overlay_file))
    set_overlay_data(_OVERLAY)

    assert _content_policy() == "anthropic"


def test_content_install_validates_its_input() -> None:
    """A malformed carried overlay fails at install, as a file overlay fails at load."""
    with pytest.raises(ValueError, match="unknown content_policy"):
        set_overlay_data({"presets": {"bad": {"match": ["x/*"], "content_policy": "nonsense"}}})
