"""The ``_HANDLERS`` registry mapping :class:`TraceConstraintKind` to its operator."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from tolokaforge.core.grading.trace_checks.constraints import (
    _absent,
    _absent_before,
    _absent_between,
    _all_of,
    _any_of,
    _before,
    _count,
    _immediately_before,
    _negate,
    _present,
)
from tolokaforge.core.grading.trace_checks.resolver import _Resolver
from tolokaforge.core.grading.trace_checks.truth import _Truth
from tolokaforge.core.models import OnMissing, TraceConstraintKind

_HANDLERS: Mapping[TraceConstraintKind, Callable[[Any, _Resolver, OnMissing], _Truth]] = {
    TraceConstraintKind.PRESENT: _present,
    TraceConstraintKind.ABSENT: _absent,
    TraceConstraintKind.COUNT: _count,
    TraceConstraintKind.BEFORE: _before,
    TraceConstraintKind.IMMEDIATELY_BEFORE: _immediately_before,
    TraceConstraintKind.ABSENT_BEFORE: _absent_before,
    TraceConstraintKind.ABSENT_BETWEEN: _absent_between,
    TraceConstraintKind.ALL_OF: _all_of,
    TraceConstraintKind.ANY_OF: _any_of,
    TraceConstraintKind.NEGATE: _negate,
}
