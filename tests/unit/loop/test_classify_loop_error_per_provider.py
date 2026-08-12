"""``classify_loop_error`` consults caller-supplied patterns.

``RATE_LIMIT`` fires only on typed-429 evidence; ``ERROR`` (with the
rate-limit-shaped explanatory message) fires when only the text tier
matches. The tier structure is per-provider now — patterns come in as an
argument, not from a module-level constant.
"""

from __future__ import annotations

import pytest
from litellm.exceptions import RateLimitError

from tolokaforge.core.llm.providers import (
    DEFAULT_RATE_LIMIT_PATTERNS,
    compile_rate_limit_patterns,
)
from tolokaforge.core.loop import classify_loop_error
from tolokaforge.core.models import TerminationReason, TrialStatus

pytestmark = pytest.mark.unit


_DEFAULT_COMPILED = compile_rate_limit_patterns(DEFAULT_RATE_LIMIT_PATTERNS)


def _typed_rate_limit() -> RateLimitError:
    return RateLimitError(message="quota", llm_provider="openrouter", model="anthropic/claude")


def test_typed_429_evidence_produces_rate_limit() -> None:
    """A typed litellm/openai 429 must exclude the trial from the denominator,
    so ``TerminationReason.RATE_LIMIT`` is what the classifier returns even if
    the patterns list is empty."""
    decision = classify_loop_error(_typed_rate_limit(), ())

    assert decision.reason is TerminationReason.RATE_LIMIT
    assert decision.status is TrialStatus.ERROR


def test_text_only_rate_limit_shape_counts_as_error_not_rate_limit() -> None:
    """No typed evidence — the trial COUNTS but its terminal message names why
    it was not classified as a rate limit. Requires the caller to supply the
    default HTTP-429 pattern list; with an empty ``patterns`` argument the
    branch is unreachable."""
    exc = RuntimeError("LLM API call failed: Error code: 429")

    with_patterns = classify_loop_error(exc, _DEFAULT_COMPILED)
    assert with_patterns.reason is TerminationReason.ERROR
    assert "Rate-limit-shaped error" in with_patterns.system_message

    without_patterns = classify_loop_error(exc, ())
    # No patterns means the text tier never fires; the ``API`` in the message
    # still routes to ``API_ERROR`` (the branch below the text tier).
    assert without_patterns.reason is TerminationReason.API_ERROR


def test_bespoke_pattern_reaches_the_text_tier() -> None:
    """A provider-specific text shape is what the per-provider seam exists for.
    A synthetic pattern list matches on its own prose without needing typed
    evidence."""
    import re

    patterns = (re.compile(r"\bacme_quota_exceeded\b"),)
    decision = classify_loop_error(RuntimeError("acme_quota_exceeded"), patterns)

    assert decision.reason is TerminationReason.ERROR
    assert "Rate-limit-shaped error" in decision.system_message


def test_plain_error_without_typed_or_text_evidence_terminates_as_error() -> None:
    decision = classify_loop_error(ValueError("some other failure"), _DEFAULT_COMPILED)

    assert decision.reason is TerminationReason.ERROR
    assert decision.status is TrialStatus.ERROR
