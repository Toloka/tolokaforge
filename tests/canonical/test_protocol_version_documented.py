"""The documented wire-protocol version agrees with the constant the gate reads.

``ENGINE_PROTOCOL_VERSION`` decides which engines a runner image registers, and
every in-tree caller reads the constant — so a bump nobody documented reds
nothing, while ``docs/GRPC_PROTOCOL.md`` keeps telling an operator that the
newest version is the previous one and never names what the new one changed.
The doc's § Version lock is that second source: it carries one sentence per
version, and the set of versions it names is what this pins.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tolokaforge.runner.protocol import ENGINE_PROTOCOL_VERSION

pytestmark = pytest.mark.canonical

_DOC = Path(__file__).resolve().parents[2] / "docs" / "GRPC_PROTOCOL.md"
_HEADING = "#### Version lock"
_VERSION_SENTENCE = re.compile(r"^Version (\d+) is the first\b", re.MULTILINE)


def _documented_versions() -> list[int]:
    """Versions named by the section's one-sentence-per-version convention."""
    lines = _DOC.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip() == _HEADING]
    assert len(starts) == 1, f"{_HEADING!r} appears {len(starts)} times in {_DOC.name}"

    span: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("#"):
            break
        span.append(line)
    return [int(match) for match in _VERSION_SENTENCE.findall("\n".join(span))]


def test_the_version_lock_section_names_exactly_the_versions_that_exist() -> None:
    """A version the section skips leaves a bump nobody documented; one above the
    constant describes a gate nothing enforces."""
    documented = _documented_versions()

    assert sorted(documented) == list(range(1, ENGINE_PROTOCOL_VERSION + 1)), (
        f"{_DOC.name} § Version lock names {sorted(documented)}; the versions that "
        f"exist are 1..{ENGINE_PROTOCOL_VERSION}. The section documents each in a "
        "sentence opening 'Version N is the first', so an empty list means the sweep "
        "read none of them"
    )
