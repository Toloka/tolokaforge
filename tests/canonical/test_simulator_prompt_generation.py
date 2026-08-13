"""The simulator's rendered prompt body is pinned to the stamp that dates it.

``docs/OUTPUT_FORMAT.md`` § Schema Version Stamps makes
``Trajectory.simulator_schema_version`` bump on any revision to the LLM
user-simulator prompt body or to the conversation context the simulator sees.
The first of those triggers is mechanical here: ``_PROMPT_DIGESTS`` records what
each generation renders, so a prompt-body edit that skips the bump reds against
its own generation's row.

``_PROMPT_DIGESTS`` is hand-edited and has no regeneration mechanism. A
``--update-canon`` snapshot would let the very edit this module exists to catch
be blessed by the commit that made it.

The three renderings are the coverage boundary: between them they exercise the
opening line, the ``Instruction:`` framing, the ``Rules:`` block and the
tool-guidance block — every conditional segment ``_build_system_prompt`` has. A
fourth conditional segment needs a fourth rendering here, or it moves unguarded.
"""

from __future__ import annotations

import hashlib

import pytest

from tolokaforge.core.llm import UserSimulator
from tolokaforge.core.models import Trajectory

pytestmark = pytest.mark.canonical

_GENERATION: int = Trajectory.model_fields["simulator_schema_version"].default

# Superseded rows stay: each records what produced every bundle stamped with it.
_PROMPT_DIGESTS: dict[int, dict[str, str]] = {
    2: {
        "without_tools": "c9dde5c50aad6c3da91f74d23fa1a6bb35785184ec02e8fe407dcb04e2d4ec2e",
        "with_tools": "38506901e12c14cc8ab9b152ad7a9129c2c2d70ed3c9432eb2bdbd79e5cb4ea1",
        "with_backstory": "1911fd104903dc7699680d8de534caaf7c501564d24432caae7c84f188e46dd4",
    },
    3: {
        "without_tools": "1d362493cb6f5d18d9dd156f082f3e2454eaaaf20c2533b09aafb0083d18b9d5",
        "with_tools": "793f035428ea659b40836f2ab068192034dfc362722929f0f66a6a7ae311552d",
        "with_backstory": "db4863aed49129ebb429e7b14066f78de275e8138a4c6bd2724a50457943f8b6",
    },
}


# The rendering names live here alone: every generation's row carries exactly
# these keys, and a row keyed by anything else names itself in the comparison
# below rather than passing unread.
_RENDERINGS: dict[str, UserSimulator] = {
    "without_tools": UserSimulator(backstory=None, tool_schemas=None),
    "with_tools": UserSimulator(backstory=None, tool_schemas=[{}]),
    "with_backstory": UserSimulator(backstory="BACKSTORY", tool_schemas=None),
}


def _rendered_digests() -> dict[str, str]:
    return {
        name: hashlib.sha256(sim._build_system_prompt().encode("utf-8")).hexdigest()
        for name, sim in _RENDERINGS.items()
    }


def test_the_prompt_body_renders_what_its_generation_recorded() -> None:
    """Each of the stamp's two triggers reds one assertion, with its own remedy.

    They are sequential rather than two tests because only one of them can be
    true of a given commit: a conversation-context revision bumps the stamp
    without touching the prompt body, so the missing row is the whole finding
    and a digest comparison against a generation that recorded nothing would
    report a prompt change that did not happen.
    """
    assert _GENERATION in _PROMPT_DIGESTS, (
        f"Trajectory.simulator_schema_version is {_GENERATION} and _PROMPT_DIGESTS "
        f"records generations {sorted(_PROMPT_DIGESTS)}. A bump opens a generation and "
        "carries its row in the same commit. A bump made for a conversation-context "
        "revision leaves the prompt body untouched, so its row repeats generation "
        f"{max(_PROMPT_DIGESTS)}'s three digests verbatim; a bump made for a "
        "prompt-body revision carries the digests the rewritten body renders. Keep the "
        "superseded rows either way."
    )

    expected = _PROMPT_DIGESTS[_GENERATION]
    actual = _rendered_digests()
    names = set(actual) | set(expected)
    moved = sorted(name for name in names if expected.get(name) != actual.get(name))

    assert actual == expected, (
        f"generation {_GENERATION} and UserSimulator._build_system_prompt disagree on "
        f"{', '.join(moved)} — a name only one of them carries is a manifest row keyed "
        "by a rendering nothing builds. Either the prompt-body edit was "
        "unintended and belongs reverted, or it opens a new generation — then "
        "Trajectory.simulator_schema_version and a new _PROMPT_DIGESTS row move "
        "together. This manifest is hand-edited: --update-canon does not touch it."
    )
