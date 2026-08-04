"""Resolving markdown cross-references the way GitHub renders them.

The canonical doc locks assert that a link's ``#anchor`` matches a heading actually
present in the target document, so a renamed heading reds a test instead of silently
returning the reader to nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_HEADING = re.compile(r"^#{1,3} ")


def anchor(heading: str) -> str:
    """The anchor a heading answers to.

    Each space becomes its own hyphen and everything that is neither alphanumeric nor
    ``-``/``_`` is dropped, so a heading whose em dash is surrounded by spaces keeps
    the double hyphen those two spaces produce.
    """
    text = heading.lstrip("#").strip().lower()
    return "".join(
        char if char.isalnum() or char in "-_" else "-" if char == " " else "" for char in text
    )


def section(lines: Sequence[str], heading: str, doc_name: str) -> str:
    """``heading``'s own text, up to the next heading of level 1–3."""
    starts = [index for index, line in enumerate(lines) if line.rstrip() == heading]
    assert len(starts) == 1, f"{heading!r} appears {len(starts)} times in {doc_name}"
    body = lines[starts[0] + 1 :]
    ends = [index for index, line in enumerate(body) if _HEADING.match(line)]
    return "\n".join(body[: ends[0]] if ends else body)
