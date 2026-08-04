"""``docs/NATIVE_ADAPTER.md`` answers the grading-vocabulary question by pointer.

The doc's grading section sends the reader to ``docs/GRADING.md`` § Substrate Parity
(manifest-derived: which keys exist and which substrate consumes each) and to
``docs/REFERENCE.md`` § grading.yaml (worked key syntax) instead of carrying its own
enumeration. A pointer is load-bearing state: a renamed heading in the destination or
a broken link in this file silently returns the reader to no answer, so both are
resolved here against the documents as they actually are. Line-number citations are
refused outright — a moved line rots silently, a named symbol is greppable.

Scoped to ``docs/NATIVE_ADAPTER.md`` alone: the same link check over all of ``docs/``
fails on the pre-existing dead-link backlog (#883, which owns the widening).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.utils.doc_anchors import anchor, section

pytestmark = pytest.mark.canonical

_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
_NATIVE_DOC = _DOCS_DIR / "NATIVE_ADAPTER.md"
_GRADING_DOC = _DOCS_DIR / "GRADING.md"
_REFERENCE_DOC = _DOCS_DIR / "REFERENCE.md"

_GRADING_SECTION_HEADING = "## grading.yaml Example"
_SUBSTRATE_PARITY_HEADING = "## Substrate Parity"
_REFERENCE_GRADING_HEADING = "### grading.yaml"

_ANY_HEADING = re.compile(r"^#{1,6} ")
_LINK_TARGET = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINE_CITATION = re.compile(r"\w+\.(?:py|md|yaml|json):\d+")


def _unfenced(doc: Path) -> list[str]:
    """``doc``'s lines with fenced code blocks dropped.

    A fenced sample legitimately shows link-shaped YAML or a ``file.yaml:12``
    illustration; only rendered prose carries the doc's own links and citations.
    """
    lines = []
    inside_fence = False
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            inside_fence = not inside_fence
            continue
        if not inside_fence:
            lines.append(line)
    assert not inside_fence, f"unclosed code fence in {doc.name}"
    return lines


def _anchor_into(doc: Path, heading: str) -> str:
    """The anchor ``heading`` answers to, after proving ``doc`` still carries it."""
    headings = [line.rstrip() for line in _unfenced(doc) if _ANY_HEADING.match(line)]
    assert heading in headings, f"{heading!r} is not a heading in {doc.name}"
    return anchor(heading)


def test_every_link_in_the_native_adapter_doc_resolves_and_no_citation_names_a_line():
    """Each link lands, whichever side of the file boundary its target is on.

    An intra-file ``#anchor`` must name a heading in this file — the class the doc's
    own table of contents lives in, so a heading rename that forgets the TOC reds
    here. A relative target must be a path that exists. And no inline code span may
    cite ``<file>:<line>``: the line moves, the citation stays, and the doc asserts
    something about code nobody can find.
    """
    lines = _unfenced(_NATIVE_DOC)
    own_anchors = {anchor(line) for line in lines if _ANY_HEADING.match(line)}

    targets = _LINK_TARGET.findall("\n".join(lines))
    assert targets, f"{_NATIVE_DOC.name} carries links; an empty scan is a broken scan"
    for target in targets:
        if target.startswith("#"):
            resolves = target[1:] in own_anchors
            assert resolves, f"({target}) names no heading in {_NATIVE_DOC.name}"
        else:
            path = (_DOCS_DIR / target.split("#", 1)[0]).resolve()
            assert path.exists(), f"({target}) points at {path}, which does not exist"

    citations = [
        span for line in lines for span in _CODE_SPAN.findall(line) if _LINE_CITATION.search(span)
    ]
    assert citations == [], (
        f"line-number citations in {_NATIVE_DOC.name} rot silently — name the symbol "
        f"instead: {citations}"
    )


def test_the_native_adapter_doc_points_at_the_grading_schema_by_a_resolvable_anchor():
    """The grading section's answer is the two pointers, and both anchors resolve.

    ``GRADING.md`` § Substrate Parity answers which keys exist and where;
    ``REFERENCE.md`` § grading.yaml shows worked key syntax. Each anchor is derived
    from the heading actually present in the destination, so renaming either heading
    reds this test instead of orphaning the reader.
    """
    grading_section = section(
        _NATIVE_DOC.read_text(encoding="utf-8").splitlines(),
        _GRADING_SECTION_HEADING,
        _NATIVE_DOC.name,
    )
    parity_link = f"(GRADING.md#{_anchor_into(_GRADING_DOC, _SUBSTRATE_PARITY_HEADING)})"
    reference_link = f"(REFERENCE.md#{_anchor_into(_REFERENCE_DOC, _REFERENCE_GRADING_HEADING)})"

    assert parity_link in grading_section, (
        f"{_GRADING_SECTION_HEADING!r} must send the reader to GRADING.md § Substrate "
        f"Parity via {parity_link}"
    )
    assert reference_link in grading_section, (
        f"{_GRADING_SECTION_HEADING!r} must send the reader to REFERENCE.md § "
        f"grading.yaml via {reference_link}"
    )
