"""Guard the "one fold definition, two dispatchers" invariant.

Both the runner-side ``_grade_trial_async`` and the grader-side
``_run_composite`` reduce their component scores through
:class:`~tolokaforge.core.grading.composite_fold.CompositeFold` — one class,
one ``finalise`` method, and exactly two callers. A third caller of
``finalise(``, or direct dispatcher calls to the two symbols
``finalise`` wraps, would silently re-collapse the seam.

Text-level assertions rather than an AST pass — the goal is to catch a
copy-paste of the fold into a third dispatcher, not to prove semantic
equivalence, and a text sweep is what a reviewer would run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "tolokaforge"
_FOLD_MODULE = _PACKAGE_ROOT / "core" / "grading" / "composite_fold.py"
_RUNNER_SITE = _PACKAGE_ROOT / "runner" / "service.py"
_GRADER_SITE = _PACKAGE_ROOT / "grader" / "composite_dispatch.py"


def _iter_package_python_files() -> list[Path]:
    return sorted(p for p in _PACKAGE_ROOT.rglob("*.py") if p.is_file())


def test_composite_fold_class_is_defined_exactly_once_in_the_pure_module() -> None:
    """The class name appears as a ``class`` header in exactly one file."""
    class_pattern = re.compile(r"^class\s+CompositeFold\b", re.MULTILINE)
    definitions = [
        path
        for path in _iter_package_python_files()
        if class_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert definitions == [_FOLD_MODULE], (
        f"Expected exactly one ``class CompositeFold`` in {_FOLD_MODULE.relative_to(_REPO_ROOT)}, "
        f"found: {[p.relative_to(_REPO_ROOT) for p in definitions]}"
    )


def test_finalise_has_exactly_two_call_sites_outside_the_fold_module() -> None:
    """One call site in the runner service, one in the grader dispatch — no other."""
    call_pattern = re.compile(r"CompositeFold\.finalise\(")
    call_sites = [
        path
        for path in _iter_package_python_files()
        if path != _FOLD_MODULE and call_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert sorted(call_sites) == sorted([_RUNNER_SITE, _GRADER_SITE]), (
        f"Expected exactly two callers of CompositeFold.finalise( "
        f"({_RUNNER_SITE.relative_to(_REPO_ROOT)}, {_GRADER_SITE.relative_to(_REPO_ROOT)}); "
        f"found: {[p.relative_to(_REPO_ROOT) for p in call_sites]}"
    )


def test_dispatchers_do_not_call_the_finalise_pieces_directly() -> None:
    """Runner and grader reach through ``finalise``, not around it.

    ``compose_trial_verdict`` and ``build_grade_reasons`` are internals of the fold
    once ``finalise`` exists — either dispatcher calling them directly would
    fork the reason-segment order and re-split the seam.

    :mod:`tolokaforge.core.grading.rubric_migration` remains free to call
    ``compose_trial_verdict`` directly: it composes verdicts by historical
    column and swallows ``MissingComponentWeight`` into an ``UnrecomputedTrial``
    sentinel, so folding it through ``finalise`` would mean feeding empty
    strings and discarding most output.
    """
    forbidden = re.compile(r"\b(compose_trial_verdict|build_grade_reasons)\(")
    for path in (_RUNNER_SITE, _GRADER_SITE):
        text = path.read_text(encoding="utf-8")
        matches = forbidden.findall(text)
        assert not matches, (
            f"{path.relative_to(_REPO_ROOT)} calls {sorted(set(matches))} directly — "
            f"route it through CompositeFold.finalise instead."
        )
