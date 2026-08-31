"""``GradingMethod`` — the runner-side dispatch-selector seam.

One entry-point group, one marker Protocol: **which grading dispatch does
the runner run for this trial?** ``RunnerGradingConfig.grading_method``
carries a bare string; the runner resolves it against
``tolokaforge.grading_methods`` at ``RegisterTrial`` time — unknown names
fail loud there rather than running a trial that scores nothing. See
``load_grading_method`` on ``tolokaforge.core.plugin_registry`` and the
seam narrative in ``docs/GRADER_SERVICE.md`` § Extension points.

Two built-in markers ship: :class:`CompositeGradingMethod` (the default
state-checks / transcript-rules / trace-checks / llm-judge / custom-checks
fold) and :class:`TestExecutionGradingMethod` (the reference-suite
short-circuit at :meth:`RunnerServiceImpl._grade_via_test_execution`).
A downstream adapter registers its own dispatch name by one entry-point
line — no framework PR — under the same
``[project.entry-points."tolokaforge.grading_methods"]`` block.

The Protocol carries a single class attribute — ``NAME`` — so the marker
is the shape a discovery-time typecheck can enforce. Runtime dispatch
today still branches on the wire string at
``RunnerServiceImpl.GradeTrial``; the registry is a validation +
discovery surface only.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

__all__ = [
    "CompositeGradingMethod",
    "GradingMethod",
    "TestExecutionGradingMethod",
]


@runtime_checkable
class GradingMethod(Protocol):
    """Marker Protocol every ``tolokaforge.grading_methods`` entry resolves to.

    Implementations are class-shaped — the entry-point resolves to the
    class object itself, mirroring
    :class:`~tolokaforge.core.grading.substrate.GradingSubstrate` from
    ADR-0040. ``NAME`` MUST equal the entry-point name so a downstream
    typo in ``pyproject.toml`` surfaces at discovery, not at grade time.
    """

    NAME: ClassVar[str]


class CompositeGradingMethod:
    """Marker for the default composite dispatch — the state-checks /
    transcript-rules / trace-checks / llm-judge / custom-checks fold that
    runs when ``grading.grading_method`` is ``"composite"`` or omitted
    (``None``)."""

    NAME: ClassVar[str] = "composite"


class TestExecutionGradingMethod:
    """Marker for the reference-suite dispatch —
    :meth:`RunnerServiceImpl._grade_via_test_execution` short-circuits to
    an exec-capable lifecycle tool that runs the pack's own tests and
    reads the reward off disk."""

    NAME: ClassVar[str] = "test_execution"
