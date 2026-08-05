"""Per-tier score container returned inside :class:`Grade`.

``GradeComponents`` carries the five grading-tier scores (state checks,
transcript rules, trace checks, LLM judge, custom checks); the combine
algebra lives on ``GradingCombineConfig``. ``None`` on any tier means the
tier did not run for this trial (distinct from ``0.0``, which is a scored
zero).
"""

from pydantic import BaseModel

__all__ = ["GradeComponents"]


class GradeComponents(BaseModel):
    """Individual grading component scores"""

    state_checks: float | None = None
    transcript_rules: float | None = None
    trace_checks: float | None = None
    llm_judge: float | None = None
    custom_checks: float | None = None
