"""Value predicates shared by every grader that compares an authored value to a real one.

Substrate-neutral and stdlib-only. A comparison written here is the one comparison
the codebase makes, so a `contains` in a JSONPath state check and a `contains` in a
trace check cannot drift into two readings of the same word.
"""

from __future__ import annotations

from typing import Any

__all__ = ["contains"]


def contains(haystack: Any, needle: Any, ci: bool = False) -> bool:
    """Whether ``needle`` occurs anywhere in ``haystack``, by recursive descent.

    Two strings compare as a substring; a list, tuple or set holds the needle when
    any element does; a dict when any **value** does — keys are never searched. Two
    values of any other shape compare by equality, so ``contains`` over a scalar is
    ``equals``.

    ``ci`` folds case, and reaches only the string comparison: two non-strings have
    no case to fold.
    """
    if isinstance(haystack, str) and isinstance(needle, str):
        return needle.casefold() in haystack.casefold() if ci else needle in haystack
    if isinstance(haystack, list | tuple | set):
        return any(contains(item, needle, ci=ci) for item in haystack)
    if isinstance(haystack, dict):
        return any(contains(value, needle, ci=ci) for value in haystack.values())
    return haystack == needle
