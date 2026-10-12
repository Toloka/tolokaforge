"""The "Available overrides" list in docs/CONFIG.md names exactly the keys the engine accepts.

``models.<role>.capabilities`` refuses every key outside
``_RECOGNISED_OVERRIDE_KEYS``, so a key missing from the list is one an author cannot
discover, and a key the list names but the engine lacks is one it refuses.
"""

import re
from pathlib import Path

import pytest

from tolokaforge.core.llm.presets import _RECOGNISED_OVERRIDE_KEYS

pytestmark = pytest.mark.canonical

_CONFIG_MD = Path(__file__).resolve().parents[2] / "docs" / "CONFIG.md"
_LIST_HEADING = "Available overrides:\n"
_LEADING_KEY = re.compile(r"- `([a-z_]+)` ")


def _documented_override_keys() -> list[str]:
    text = _CONFIG_MD.read_text()
    assert text.count(_LIST_HEADING) == 1, f"expected one {_LIST_HEADING!r} in {_CONFIG_MD}"
    keys = []
    for line in text.split(_LIST_HEADING, 1)[1].splitlines():
        if not line.startswith("- "):
            break
        match = _LEADING_KEY.match(line)
        assert match, f"override bullet does not lead with a backticked key: {line!r}"
        keys.append(match.group(1))
    return keys


def test_the_documented_override_list_is_the_recognised_key_set() -> None:
    documented = _documented_override_keys()
    assert len(documented) == len(set(documented)), f"duplicate bullets: {documented}"
    missing = sorted(_RECOGNISED_OVERRIDE_KEYS - set(documented))
    extra = sorted(set(documented) - _RECOGNISED_OVERRIDE_KEYS)
    assert (missing, extra) == ([], []), (
        f"docs/CONFIG.md 'Available overrides' is missing {missing} and lists {extra}, "
        "which the engine does not accept"
    )
