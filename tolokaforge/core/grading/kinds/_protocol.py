"""``GraderKind`` — typed grader-kind Protocol + narrow refusal exception.

Every entry registered under ``tolokaforge.grader_kinds`` resolves to a
class satisfying :class:`GraderKind`. The class carries a ``NAME`` matching
its entry-point name (so a pyproject typo surfaces at discovery, not at
grade time) and an ``evaluate`` method that produces a
:class:`~tolokaforge.core.models.grade.Grade` from a
:class:`~tolokaforge.core.grading.substrate.GradingSubstrate` — the topology
seam every grader reads through.

``evaluate`` is kwargs-only. Callers bind arguments at dispatch time via
``functools.partial(kind.evaluate, substrate=..., task_config=..., ...)``
so a future field lands mid-list without positional drift on downstream
adapters.

Three exception classes ride the seam:

- :class:`~tolokaforge.core.grading.substrate.SubstrateUnreachableError` —
  the substrate itself is unreachable (gRPC transport failure, snapshot
  missing, ...). The composite dispatch translates this to a
  ``GradingFailedError`` so the trial is booked as ungradeable.
- ``GradingFailedError`` — measured but not gradeable (raised by the
  composite when a sub-component evaluator reports a hard failure).
- :class:`GraderKindRefusedError` (this module) — the kind refuses to
  dispatch on this trial as an *adapter authoring* issue (e.g. the
  test-execution kind on a trial that shipped no exec-capable lifecycle
  tool). The dispatcher catches this narrow exception and maps it to a
  ``GradeTrialResponse(success=False, error=exc.reason)`` — the pre-move
  runner-side tool-absent shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models.grade import Grade
    from tolokaforge.runner.models import RunnerGradingConfig

__all__ = [
    "GraderKind",
    "GraderKindRefusedError",
]


class GraderKindRefusedError(Exception):
    """The kind refuses to dispatch this trial as an adapter-authoring issue.

    Carries the actionable message the runner surfaces on the wire — the
    dispatcher maps this to ``pb2.GradeTrialResponse(success=False,
    error=exc.reason)``. Distinct from
    :class:`~tolokaforge.core.grading.substrate.SubstrateUnreachableError`
    (substrate transport failure) and ``GradingFailedError`` (trial measured
    but ungradeable).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@runtime_checkable
class GraderKind(Protocol):
    """Marker + evaluator Protocol every ``tolokaforge.grader_kinds`` entry resolves to.

    ``NAME`` MUST equal the entry-point name so a downstream typo in
    ``pyproject.toml`` surfaces at discovery, not at grade time.

    ``evaluate`` returns:

    - a :class:`Grade` when the kind produced a verdict, or
    - ``None`` when the kind opted out (empty active set / not applicable
      to this trial). ``None`` is a first-class outcome, not an error.

    ``evaluate`` may raise
    :class:`~tolokaforge.core.grading.substrate.SubstrateUnreachableError`
    (substrate transport failure) or :class:`GraderKindRefusedError` (the
    kind refuses to dispatch on this trial as an adapter-authoring issue).
    """

    NAME: ClassVar[str]

    def evaluate(
        self,
        *,
        substrate: GradingSubstrate,
        task_config: RunnerGradingConfig,
        kind_config: Mapping[str, Any] | None,
        trial_id: str,
        agent_tools: Mapping[str, Any],
        logger: StructuredLogger,
    ) -> Grade | None: ...
