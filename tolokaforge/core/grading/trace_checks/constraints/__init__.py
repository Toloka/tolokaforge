"""The 10-member constraint-operator vocabulary.

Each operator returns a :class:`~tolokaforge.core.grading.trace_checks.truth._Truth`
— the Kleene fold's atomic input. The dispatch registry lives in
:mod:`~tolokaforge.core.grading.trace_checks.dispatch`; this package groups the
operator implementations by the private helpers they share.
"""

from tolokaforge.core.grading.trace_checks.constraints.logical import (
    _all_of,
    _any_of,
    _negate,
)
from tolokaforge.core.grading.trace_checks.constraints.ordering import (
    _before,
    _immediately_before,
)
from tolokaforge.core.grading.trace_checks.constraints.presence import (
    _absent,
    _count,
    _present,
)
from tolokaforge.core.grading.trace_checks.constraints.windows import (
    _absent_before,
    _absent_between,
)

__all__ = [
    "_absent",
    "_absent_before",
    "_absent_between",
    "_all_of",
    "_any_of",
    "_before",
    "_count",
    "_immediately_before",
    "_negate",
    "_present",
]
