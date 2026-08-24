"""Structural lock: ``composite.py`` does not import reference impls.

The composite reaches every sub-component through its Protocol via a
resolved instance passed as a kwarg. It must never name the reference
impl at module load time — that would defeat the plug-in seam by pinning
one implementation into the dispatch surface.

Reads the composite module's source and asserts, via ``ast``, that none of
the shipping reference-impl names appear on any ``import`` statement's
imported symbol list. Later stages extend this list as each seam lands
(``evaluate_transcript_rules``, per-operator trace impls,
``JsonpathStateCheckBackend``, …). Stage 2 lands the first assertion —
``LLMJudge`` is not imported from :mod:`tolokaforge.core.grading.judge`.

Complements the ``.importlinter`` contract Stage 5 adds — the linter
enforces module-level forbid rules across the codebase; this test locks
the composite specifically, at unit-tier cost, so a regression trips
even when the linter is not yet run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_COMPOSITE_PATH = (
    Path(__file__).resolve().parents[2] / "tolokaforge" / "core" / "grading" / "composite.py"
)


def _imported_names(module_source: str) -> set[tuple[str, str]]:
    """Return the set of ``(from_module, imported_name)`` pairs.

    Covers both ``from X import Y`` (yielding ``(X, Y)``) and
    ``import X`` / ``import X as Z`` (yielding ``(X, X)`` — the imported
    module is its own name at the callsite). Ignores relative imports
    (they cannot reach a reference-impl module by rule).
    """
    tree = ast.parse(module_source)
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                pairs.add((node.module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                pairs.add((alias.name, alias.name))
    return pairs


def test_composite_does_not_import_llm_judge() -> None:
    """The rubric-evaluator seam owns the ``LLMJudge`` reach — the composite
    must not import it directly.
    """
    imports = _imported_names(_COMPOSITE_PATH.read_text())
    assert ("tolokaforge.core.grading.judge", "LLMJudge") not in imports, (
        "composite.py must not import LLMJudge — the RubricEvaluator seam owns "
        "the reach through the reference impl."
    )
