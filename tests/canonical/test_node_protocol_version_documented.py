"""The documented trial-node protocol version agrees with the constant the gate reads.

``NODE_PROTOCOL_VERSION`` decides which engines a node serves and which nodes an
engine dials. ``docs/GRPC_PROTOCOL.md`` § Node version lock carries one sentence
per version, and the set of versions it names is what this pins.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tolokaforge.trial_node.protocol import NODE_PROTOCOL_VERSION

pytestmark = pytest.mark.canonical

_DOC = Path(__file__).resolve().parents[2] / "docs" / "GRPC_PROTOCOL.md"
_HEADING = "#### Node version lock"
_VERSION_SENTENCE = re.compile(r"^Node version (\d+) is the first\b", re.MULTILINE)


def _documented_versions() -> list[int]:
    lines = _DOC.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip() == _HEADING]
    assert len(starts) == 1, f"{_HEADING!r} appears {len(starts)} times in {_DOC.name}"
    span: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("#"):
            break
        span.append(line)
    return [int(match) for match in _VERSION_SENTENCE.findall("\n".join(span))]


def test_the_node_version_lock_names_exactly_the_versions_that_exist() -> None:
    documented = _documented_versions()

    assert sorted(documented) == list(range(1, NODE_PROTOCOL_VERSION + 1)), (
        f"{_DOC.name} § Node version lock names {sorted(documented)}; the versions that "
        f"exist are 1..{NODE_PROTOCOL_VERSION}. Each is documented in a sentence "
        "opening 'Node version N is the first'."
    )
