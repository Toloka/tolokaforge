"""Structural lock: ``composite.py`` does not import reference impls.

The composite reaches every sub-component through its Protocol via a
resolved instance passed as a kwarg. It must never name the reference
impl at module load time — that would defeat the plug-in seam by pinning
one implementation into the dispatch surface.

Reads the composite module's source and asserts, via ``ast``, that none of
the shipping reference-impl names appear on any ``import`` statement's
imported symbol list. Six sub-component seams cover the six reference-impl
holdings the composite is fenced from: :class:`LLMJudge`, :class:`LLMClient`,
:class:`LLMJudgeRubricEvaluator`, :class:`LiteLLMJudgeModelProvider`,
:class:`DefaultTranscriptRuleMatcher`, and the two state-check backends
:class:`JsonpathStateCheckBackend` + :class:`DbProbesStateCheckBackend`.
The underlying utility functions those reference impls wrap
(:func:`evaluate_transcript_rules`, :func:`evaluate_jsonpath_checks`) are
forbidden by name for the same reason.
Utility symbols the composite legitimately reuses —
:func:`scored_transcript_rules` (events-less-trial gate),
:func:`transcript_rules_author_keys` (accounting),
:func:`render_state_diff` (state-diff compute helper the hoisted
``build_judge_state_diff`` calls) — are NOT forbidden.

Complements the ``.importlinter`` ``composite-sub-component-seams``
contract — the linter enforces module-level forbid rules across the
codebase; this test locks the composite specifically, at unit-tier cost,
so a regression trips even when the linter is not yet run.
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


def test_composite_does_not_import_evaluate_transcript_rules() -> None:
    """The transcript-rule-matcher seam owns the ``evaluate_transcript_rules``
    reach — the composite must not import it directly. The gate + accounting
    utilities from the same module (``scored_transcript_rules`` and
    ``transcript_rules_author_keys``) remain permitted.
    """
    imports = _imported_names(_COMPOSITE_PATH.read_text())
    assert (
        "tolokaforge.core.grading.transcript",
        "evaluate_transcript_rules",
    ) not in imports, (
        "composite.py must not import evaluate_transcript_rules — the "
        "TranscriptRuleMatcher seam owns the reach through the reference impl."
    )
    assert (
        "tolokaforge.core.grading.transcript",
        "scored_transcript_rules",
    ) in imports, (
        "composite.py must keep scored_transcript_rules — it is the "
        "events-less-trial gate that runs above the matcher."
    )


def test_composite_does_not_import_any_reference_impl_symbol() -> None:
    """Every seam's reference-impl symbol is fenced by name.

    Six seams, six reference-impl holdings; three underlying utility
    functions those impls wrap. A direct import of any of these would
    silently re-collapse the seam it belongs to.
    """
    imports = _imported_names(_COMPOSITE_PATH.read_text())
    imported_symbol_names = {name for _module, name in imports}
    forbidden = {
        # Reference impls the four `default_*.py` + two `judge.py`/`llm.client` modules hold
        "LLMJudge",
        "LLMClient",
        "LLMJudgeRubricEvaluator",
        "LiteLLMJudgeModelProvider",
        "DefaultTranscriptRuleMatcher",
        "JsonpathStateCheckBackend",
        "DbProbesStateCheckBackend",
        # Underlying utility functions each reference impl wraps
        "evaluate_transcript_rules",
        "evaluate_jsonpath_checks",
    }
    leaked = forbidden & imported_symbol_names
    assert not leaked, (
        f"composite.py imports forbidden reference-impl symbols {sorted(leaked)!r}. "
        "Every sub-component seam requires the reference impl reach only through "
        "its resolved-instance kwarg, never through a direct import."
    )
