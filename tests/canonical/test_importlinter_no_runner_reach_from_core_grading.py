"""Structural lock: the ``.importlinter`` contract that fences
``core.grading`` from reaching the runner-side grade RPC or the narrow
runner-wire helpers keeps the exact shape ADR-0040 depends on.

Parses ``.importlinter`` with :mod:`configparser` and asserts:

* the section ``importlinter:contract:no-runner-reach-from-core-grading``
  is present,
* ``source_modules`` names ``tolokaforge.core.grading`` alone,
* ``forbidden_modules`` names exactly the runner grade-RPC module and
  the narrow ``tolokaforge.runner.grading`` (state-diff + wire-sentinel
  projection surface),
* ``allow_indirect_imports = false``, so a transitive path back into
  a forbidden target trips the contract, not just a direct import.

A silent narrowing of the shape (dropping the strict flag, dropping a
forbidden target) would let the ADR-0040 boundary re-collapse without
tripping ``lint-imports`` — this test locks the shape at canonical tier
so a shape drift fails collection rather than only surfacing later.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_IMPORTLINTER_PATH = Path(__file__).resolve().parents[2] / ".importlinter"
_SECTION = "importlinter:contract:no-runner-reach-from-core-grading"


def _parsed_importlinter() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(_IMPORTLINTER_PATH)
    return parser


def _multiline_values(raw: str) -> frozenset[str]:
    return frozenset(line.strip() for line in raw.splitlines() if line.strip())


def test_contract_section_is_present() -> None:
    parser = _parsed_importlinter()
    assert parser.has_section(_SECTION), (
        f".importlinter is missing the {_SECTION!r} section — the contract "
        "that fences core.grading from the runner-side grade RPC and the "
        "narrow runner-wire helpers must be declared."
    )


def test_source_modules_is_core_grading_alone() -> None:
    parser = _parsed_importlinter()
    sources = _multiline_values(parser.get(_SECTION, "source_modules"))
    assert sources == {"tolokaforge.core.grading"}, (
        f"{_SECTION} source_modules must fence tolokaforge.core.grading alone, "
        f"got {sorted(sources)!r}"
    )


def test_forbidden_modules_are_the_two_runner_targets() -> None:
    parser = _parsed_importlinter()
    forbidden = _multiline_values(parser.get(_SECTION, "forbidden_modules"))
    expected = {"tolokaforge.runner.service", "tolokaforge.runner.grading"}
    assert forbidden == expected, (
        f"{_SECTION} forbidden_modules must be exactly {sorted(expected)!r} — "
        f"got {sorted(forbidden)!r}. Adding a new target extends the shape "
        "the fold-in ledger extraction depends on; dropping one silently "
        "narrows the ADR-0040 boundary."
    )


def test_allow_indirect_imports_is_false() -> None:
    parser = _parsed_importlinter()
    raw = parser.get(_SECTION, "allow_indirect_imports")
    assert raw.strip().lower() == "false", (
        f"{_SECTION} allow_indirect_imports must be false — a transitive path "
        f"back into a forbidden target must trip the contract, not just a "
        f"direct import. Got {raw!r}."
    )
