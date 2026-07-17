"""Shared LLM-availability helper.

Two callers today: :mod:`intervener.pipeline.drafter` and
:mod:`intervener.tools.reference.analyze`. Both need the same rule —
"Anthropic if the key is set AND the package is importable, else fall
back to a heuristic". Deduplicated here so a future change (add OpenAI,
require a stricter key check, etc.) lands in one place.
"""

from __future__ import annotations

import os

__all__ = ["llm_available"]


def llm_available() -> bool:
    """True iff we have both an Anthropic API key and the client package."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True
