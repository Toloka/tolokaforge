"""Verify Runner gRPC -1.0 sentinel converts to None for unconfigured components.

In the proto, GradeComponents float fields default to 0.0, so we use -1.0
to signal "this component was not evaluated for this trial." On the
Python side we want proper None so downstream consumers (analytics,
output_writer) can distinguish "did not run" from "ran and scored 0.0".
"""

from __future__ import annotations

import pytest

from tolokaforge.core.shared_stack_runtime import _proto_score_to_optional

pytestmark = pytest.mark.unit


def test_negative_sentinel_becomes_none():
    assert _proto_score_to_optional(-1.0) is None
    assert _proto_score_to_optional(-0.5) is None
    assert _proto_score_to_optional(-100.0) is None


def test_zero_remains_zero():
    """Score 0.0 is a real failure score, not a sentinel — stays 0.0."""
    assert _proto_score_to_optional(0.0) == 0.0


def test_positive_score_passthrough():
    assert _proto_score_to_optional(0.75) == 0.75
    assert _proto_score_to_optional(1.0) == 1.0
