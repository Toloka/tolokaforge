"""Guard the "one wire-timeline recipe, two dispatchers" invariant.

Both the runner-side ``_grade_time_views`` and the grader-side
``GraderCompositeDispatch.grade`` reach the timeline through
:func:`~tolokaforge.core.grading.trace_timeline.build_timeline_from_wire` —
one function, one recipe body, and exactly two callers. A third caller in
the tree, or a dispatcher that re-inlines the raw
``build_trial_timeline(decode_transcript_wire(``-chain, would silently
re-collapse the seam.

Text-level assertions rather than an AST pass — the goal is to catch a
copy-paste of the recipe into a third dispatcher, not to prove semantic
equivalence, and a text sweep is what a reviewer would run.

Symmetric with ``tests/canonical/test_fold_defined_once.py`` (composite
fold).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "tolokaforge"
_TIMELINE_MODULE = _PACKAGE_ROOT / "core" / "grading" / "trace_timeline.py"
_RUNNER_SITE = _PACKAGE_ROOT / "runner" / "service.py"
_GRADER_SITE = _PACKAGE_ROOT / "grader" / "composite_dispatch.py"


def _iter_package_python_files() -> list[Path]:
    return sorted(p for p in _PACKAGE_ROOT.rglob("*.py") if p.is_file())


def test_build_timeline_from_wire_is_defined_exactly_once_in_the_pure_module() -> None:
    """The function name appears as a ``def`` header in exactly one file."""
    def_pattern = re.compile(r"^def\s+build_timeline_from_wire\b", re.MULTILINE)
    definitions = [
        path
        for path in _iter_package_python_files()
        if def_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert definitions == [_TIMELINE_MODULE], (
        f"Expected exactly one ``def build_timeline_from_wire`` in "
        f"{_TIMELINE_MODULE.relative_to(_REPO_ROOT)}, "
        f"found: {[p.relative_to(_REPO_ROOT) for p in definitions]}"
    )


def test_build_timeline_from_wire_has_exactly_two_call_sites_outside_the_module() -> None:
    """One call site in the runner service, one in the grader dispatch — no other.

    ``core.grading.combine`` and ``core.grading.trace_replay`` legitimately
    call :func:`build_trial_timeline` directly on non-wire inputs (stored
    trajectory data). A ``build_timeline_from_wire`` callsite outside the
    two dispatcher modules would signal recipe leakage into a codepath the
    wrapper was not designed for.
    """
    call_pattern = re.compile(r"build_timeline_from_wire\(")
    call_sites = [
        path
        for path in _iter_package_python_files()
        if path != _TIMELINE_MODULE and call_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert sorted(call_sites) == sorted([_RUNNER_SITE, _GRADER_SITE]), (
        f"Expected exactly two callers of build_timeline_from_wire( "
        f"({_RUNNER_SITE.relative_to(_REPO_ROOT)}, {_GRADER_SITE.relative_to(_REPO_ROOT)}); "
        f"found: {[p.relative_to(_REPO_ROOT) for p in call_sites]}"
    )


def test_dispatchers_do_not_re_inline_the_raw_wire_recipe() -> None:
    """Runner and grader reach through the wrapper, not around it.

    ``build_trial_timeline(decode_transcript_wire(`` is the raw composed
    recipe :func:`build_timeline_from_wire` was extracted from. Either
    dispatcher re-inlining it — even once — would fork the recipe and
    re-split the seam.

    :mod:`tolokaforge.core.grading.combine` and
    :mod:`tolokaforge.core.grading.trace_replay` remain free to call
    :func:`build_trial_timeline` directly with non-wire inputs — those are
    stored-trajectory reconstructions, not wire recipes.
    """
    forbidden = re.compile(r"build_trial_timeline\(\s*decode_transcript_wire\(")
    for path in (_RUNNER_SITE, _GRADER_SITE):
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), (
            f"{path.relative_to(_REPO_ROOT)} re-inlines the raw "
            f"``build_trial_timeline(decode_transcript_wire(`` chain — "
            f"route it through build_timeline_from_wire instead."
        )
