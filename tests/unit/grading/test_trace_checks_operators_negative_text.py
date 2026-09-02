"""The negative-text operators — ``not_contains`` and ``not_regex``.

Both are complements of their positive form *within declared events*: they hold
only where the value the event carries does not match. An absent or ``None``
field satisfies neither — the timeline-contract rule that every operator but
``exists`` is False on ``None`` holds unchanged. The tests below fix that
boundary and the case-sensitive semantics ``not_contains`` inherits from
``contains``.

An uncompilable ``not_regex`` pattern is caught at the authoring gate, not at
Pydantic construction — mirroring ``regex``, which the same gate handles. That
parametric row lives in ``test_grading_authoring_gate.py`` under label
``matcher_not_regex_that_does_not_compile``; the pure-operator semantics are
here.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.trace_check_operator import (
    not_contains_op,
    not_regex_matches,
)

pytestmark = pytest.mark.unit


def test_not_contains_holds_when_the_needle_is_absent() -> None:
    assert not_contains_op("payment succeeded", "failed", {}) is True


def test_not_contains_fails_when_the_needle_is_present() -> None:
    assert not_contains_op("payment failed", "failed", {}) is False


def test_not_contains_on_none_reads_false() -> None:
    """The gate at ``_operator_holds`` short-circuits, so the operator never sees ``None``.

    Called directly the operator answers True (the needle really is absent from
    ``None``), which is why the gate is a dispatch invariant rather than a
    per-operator concern. The evaluator-level reading is locked by
    ``test_an_absent_argument_is_unmatched_rather_than_vacuously_true`` in
    ``test_trace_checks_matchers.py``, which the ``_OPERATOR_ANSWERS`` row for
    ``not_contains`` widens automatically.
    """
    from tolokaforge.core.grading.trace_checks import _operator_holds

    assert _operator_holds("not_contains", None, "anything", {}) is False


def test_not_contains_is_case_sensitive() -> None:
    assert not_contains_op("PAYMENT FAILED", "failed", {}) is True


def test_not_regex_holds_when_the_pattern_does_not_match() -> None:
    assert not_regex_matches("REF-1234", "^PAY-", {}) is True


def test_not_regex_fails_when_the_pattern_matches() -> None:
    assert not_regex_matches("PAY-1234", "^PAY-", {}) is False


def test_not_regex_on_none_reads_false() -> None:
    from tolokaforge.core.grading.trace_checks import _operator_holds

    assert _operator_holds("not_regex", None, "any", {}) is False


def test_not_regex_on_a_non_string_value_reads_false() -> None:
    """A non-string value has no regex reading — the same shape ``regex`` refuses.

    Numbers, lists, and mappings cannot be searched by ``re.search`` without a
    coercion the timeline contract does not have; both regex operators guard
    on ``isinstance(value, str)`` for that reason. The complementary shape —
    the operator held True on a non-string because "no match" reads through —
    would let an author write ``not_regex: "^PAY-"`` against ``args.body``
    (a mapping) and score every trial True, which is the vacuous truth the
    timeline contract forbids.
    """
    assert not_regex_matches(123, "^PAY-", {}) is False
    assert not_regex_matches(["PAY-1"], "^PAY-", {}) is False
