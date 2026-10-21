"""Locks the declared litellm range to releases whose own mcp requirement our mcp range meets.

litellm declares its mcp requirement only behind its ``proxy`` / ``mcp`` extras, which
tolokaforge does not request, so ``pip install tolokaforge`` resolves litellm and mcp
independently. A litellm release built against mcp 2 then installs next to our mcp 1
and fails with an ``ImportError`` on the first LLM call. The resolver cannot catch it,
so this test does: every litellm release the declared range admits must fall in a line
of ``_LITELLM_MCP_LINES`` whose mcp range overlaps ours.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

pytestmark = pytest.mark.canonical

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


@dataclass(frozen=True)
class LitellmMcpLine:
    litellm: SpecifierSet
    mcp: SpecifierSet


# Each line's mcp range is the ``Requires-Dist`` its releases publish on PyPI.
# The last line is open-ended: releases after 1.106.2 are assumed to stay on mcp 2.
_LITELLM_MCP_LINES = (
    LitellmMcpLine(SpecifierSet(">=1.93.0,<1.103.0"), SpecifierSet(">=1.28.1,<2.0")),
    LitellmMcpLine(SpecifierSet(">=1.103.0,<2.0.0"), SpecifierSet(">=2.2.0,<3")),
)

_LITELLM_GRID = tuple(Version(f"1.{minor}.{patch}") for minor in range(200) for patch in range(20))
_MCP_GRID = tuple(
    Version(f"{major}.{minor}.{patch}")
    for major in range(1, 4)
    for minor in range(41)
    for patch in range(6)
)


def _declared_specifier(name: str) -> SpecifierSet:
    dependencies = tomllib.loads(_PYPROJECT.read_text())["project"]["dependencies"]
    matches = [req for req in map(Requirement, dependencies) if req.name == name]
    assert len(matches) == 1, f"expected one {name!r} in [project].dependencies, got {matches}"
    return matches[0].specifier


def test_every_admitted_litellm_release_has_a_known_mcp_requirement() -> None:
    litellm = _declared_specifier("litellm")
    unmapped = [
        str(v)
        for v in _LITELLM_GRID
        if v in litellm and not any(v in line.litellm for line in _LITELLM_MCP_LINES)
    ]
    assert not unmapped, (
        f"litellm {litellm} admits releases outside _LITELLM_MCP_LINES "
        f"({unmapped[0]} .. {unmapped[-1]}); add their PyPI mcp requirement to the table"
    )


def test_declared_litellm_range_excludes_releases_needing_an_mcp_we_exclude() -> None:
    litellm = _declared_specifier("litellm")
    mcp = _declared_specifier("mcp")
    conflicts = [
        f"litellm {admitted[0]} .. {admitted[-1]} requires mcp {line.mcp}"
        for line in _LITELLM_MCP_LINES
        if (admitted := [v for v in _LITELLM_GRID if v in litellm and v in line.litellm])
        and not any(v in mcp and v in line.mcp for v in _MCP_GRID)
    ]
    assert not conflicts, (
        f"declared litellm {litellm} admits releases whose own metadata needs an mcp range "
        f"disjoint from our mcp {mcp}; pip would install them beside our mcp and the first "
        f"LLM call would fail on import. Cap litellm below them: {conflicts}"
    )
