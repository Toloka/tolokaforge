"""Every shipped reply contract is pinned to the generation that dates it.

A contract is a graded input: change the text and a score stops meaning what the
last one meant. ``GENERATION`` is the number that says so, and ``_DIGESTS``
records what each generation renders, so an edit that skips the bump reds
against its own generation's row.

``_DIGESTS`` is hand-edited and has no regeneration mechanism. A
``--update-canon`` snapshot would let the very edit this module exists to catch
be blessed by the commit that made it.
"""

from __future__ import annotations

import hashlib

import pytest

from tolokaforge.core.agent_prompt_contract import (
    CONTRACTS,
    GENERATION,
    OBSERVATION_WINDOW_CLAUSE,
    observation_window_clause,
)

pytestmark = pytest.mark.canonical

# The retention clause is contract text too — composed in only when the loop
# drops observations — so it is pinned beside the contracts from the generation
# that introduced it, under its own name. A window of zero renders its own
# sentence, pinned separately.
_CLAUSE = "observation_window_clause"
_ZERO_CLAUSE = "observation_window_clause_zero"

# Superseded rows stay: each records what produced every bundle stamped with it.
_DIGESTS: dict[int, dict[str, str]] = {
    1: {
        "reasoning_agent": "bbb3866142459b82ed4679d347ad29995955bb976323cc17a71b88e171fd8b22",
    },
    2: {
        "reasoning_agent": "3ace74270f351201175b69a37c88ff0e1bd955794e4e6714184d0247ff19df55",
    },
    3: {
        "reasoning_agent": "b2515d8f3a545bd1c13720697839d521364c89eb39f32440ef1f355ba8175a3d",
        _CLAUSE: "e5d5a3ab07c8e1cf5eec0d1dadfc8e8f2a32c8a855591c37bd669c1c0bb231c9",
        _ZERO_CLAUSE: "90db515f763da478ed55ca380393a8e99ce4b4f63e49548d006694c5a2e0b174",
    },
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rendered() -> dict[str, str]:
    rendered = {name: _digest(text) for name, text in CONTRACTS.items()}
    rendered[_CLAUSE] = _digest(OBSERVATION_WINDOW_CLAUSE)
    rendered[_ZERO_CLAUSE] = _digest(observation_window_clause(0))
    return rendered


def test_each_contract_renders_what_its_generation_recorded() -> None:
    assert GENERATION in _DIGESTS, (
        f"agent_prompt_contract.GENERATION is {GENERATION} and _DIGESTS records "
        f"generations {sorted(_DIGESTS)}. A bump opens a generation and carries its "
        "row in the same commit, listing every shipped contract's digest — including "
        "the ones the bump did not touch. Keep the superseded rows."
    )

    expected = _DIGESTS[GENERATION]
    actual = _rendered()
    moved = sorted(n for n in set(actual) | set(expected) if expected.get(n) != actual.get(n))

    assert actual == expected, (
        f"generation {GENERATION} and CONTRACTS disagree on {', '.join(moved)} — a name "
        "only one of them carries is either a shipped contract with no row or a row "
        "naming a contract nothing ships. Either the edit was unintended and belongs "
        "reverted, or it opens a new generation — then GENERATION and a new _DIGESTS "
        "row move together. This manifest is hand-edited: --update-canon does not "
        "touch it."
    )
