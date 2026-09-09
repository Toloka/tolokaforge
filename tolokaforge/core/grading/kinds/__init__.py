"""``tolokaforge.grader_kinds`` — typed grader-kind package.

Every entry in ``[project.entry-points."tolokaforge.grader_kinds"]``
resolves to a class satisfying :class:`GraderKind`. Two built-ins ship
here: :class:`CompositeGraderKind` (a reference impl over
:class:`~tolokaforge.core.grading.composite_fold.CompositeFold`) and
:class:`TestExecutionGraderKind` (the reference-suite kind that reads
through :meth:`~tolokaforge.core.grading.substrate.GradingSubstrate.run_test_suite`).

Every shipped name registers in both ``tolokaforge.grader_kinds`` and
``tolokaforge.grading_methods`` — the vocabulary is one, the two Protocol
shapes coexist. See ``docs/GRADER_SERVICE.md`` § Extension points.
"""

from tolokaforge.core.grading.kinds._protocol import GraderKind, GraderKindRefusedError
from tolokaforge.core.grading.kinds.composite import CompositeGraderKind
from tolokaforge.core.grading.kinds.test_execution import (
    TestExecutionGraderKind,
    TestExecutionKindConfig,
)

__all__ = [
    "CompositeGraderKind",
    "GraderKind",
    "GraderKindRefusedError",
    "TestExecutionGraderKind",
    "TestExecutionKindConfig",
]
