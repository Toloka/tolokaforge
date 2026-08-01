"""Grade wire type plus the rubric judge's per-trial audit records.

Holds the host-side view of a graded trial: the combined score, the
per-tier :class:`GradeComponents`, and the rubric judge's outputs
(:class:`CustomCheckDetail`, :class:`JudgeStatus`, :class:`JudgeUsage`,
:class:`JudgeKbGating`, :class:`JudgeInputs`). Per-criterion results
come in via :class:`CriterionResult`, whose canonical home is
``tolokaforge.runner.models``.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from tolokaforge.core.models.grade_components import GradeComponents
from tolokaforge.runner.models import CriterionResult, TraceConstraintResult

__all__ = [
    "CustomCheckDetail",
    "Grade",
    "JudgeInputs",
    "JudgeKbGating",
    "JudgeStatus",
    "JudgeUsage",
]


class JudgeStatus(str, Enum):
    """Outcome of the rubric judge for a grade (mirrors proto ``JudgeStatus``).

    ``ERRORED`` is the fail-loud marker: the judge malfunctioned (retry / budget
    exhaustion or a crash) and there is NO trustworthy numeric score — the
    ``llm_judge`` component is incomplete, NOT 0.0. ``UNSPECIFIED`` means no
    judge was configured / run. The integer values match the proto enum so the
    gRPC wire value maps directly via :meth:`from_proto`.
    """

    UNSPECIFIED = "unspecified"
    COMPLETED = "completed"
    ERRORED = "errored"

    @classmethod
    def from_proto(cls, value: int) -> "JudgeStatus":
        """Map the proto ``JudgeStatus`` integer to this enum (fail loud on unknown)."""
        mapping = {0: cls.UNSPECIFIED, 1: cls.COMPLETED, 2: cls.ERRORED}
        if value not in mapping:
            raise ValueError(f"Unknown proto JudgeStatus value: {value}")
        return mapping[value]


class JudgeUsage(BaseModel):
    """The rubric judge's OWN token usage / cost (host-side boundary model).

    The judge runs its own LLM inside the Runner, so its spend is separate from
    the agent's (which lives in ``Metrics.usage``). This is intentionally a
    distinct, small type rather than the per-call
    :class:`~tolokaforge.core.llm.usage.Usage`: the judge reports a single
    aggregate (no per-call cache history), and its ``tool_calls`` / ``calls``
    counters are judge-specific accounting. Field set mirrors the runner's
    :class:`tolokaforge.core.grading.judge.JudgeUsage` dataclass 1:1 and the
    proto ``JudgeReport`` usage fields.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    consistency_rejections: int = 0

    model_config = {"extra": "forbid"}


class JudgeKbGating(BaseModel):
    """The rubric judge's knowledge-search gating for this trial (host-side model).

    Kept distinct from :class:`JudgeUsage` (which stays strictly token/cost
    accounting): this is the audit + replay record of *which* KB tools the judge
    was offered and which config withheld. ``knowledge_search_disabled`` is the
    authoritative replay signal — ``True`` means
    ``grading.llm_judge.customization.disable_knowledge_search`` withheld KB,
    regardless of whether the agent had a KB tool this trial. ``offered`` /
    ``withheld`` are supporting audit detail; an empty ``withheld`` on a disabled
    judge means the agent had no KB tool to gate.
    """

    knowledge_search_disabled: bool
    offered: list[str]
    withheld: list[str]

    model_config = {"extra": "forbid"}


class JudgeInputs(BaseModel):
    """The judge's non-derivable ``LLMJudge.run()`` inputs, recorded for offline replay.

    The transcript, agent policy, rubric, and judge model are already structured in
    sibling artifacts (``trajectory.yaml``, ``prompts.yaml``, ``task.yaml``); this
    records only what a replay cannot otherwise reconstruct: the exact
    ``state_diff`` string the judge saw (``None`` when no diff was built) and its
    non-KB read-tool surface (``get_db_state`` / ``query_db`` / ``read_file`` — the
    KB surface lives in :class:`JudgeKbGating`). Written to a sidecar
    ``judge_inputs.yaml``, never inlined in ``grade.yaml`` (the diff can be large).
    See docs/OUTPUT_FORMAT.md.
    """

    state_diff_text: str | None = None
    read_tools_offered: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class CustomCheckDetail(BaseModel):
    """Detail for individual custom check result"""

    check_name: str
    status: str  # "passed", "failed", "skipped", "error"
    score: float = 0.0
    message: str = ""
    details: dict[str, Any] | None = None


class Grade(BaseModel):
    """Grading result"""

    binary_pass: bool
    score: float = Field(ge=0.0, le=1.0)
    components: GradeComponents = Field(default_factory=GradeComponents)
    reasons: str | dict[str, list[str]] = ""
    state_diff: dict[str, Any] | None = None
    custom_checks_details: list[CustomCheckDetail] | None = None
    # Per-constraint trace-check verdicts, serialized inline in ``grade.yaml``:
    # small and scannable, so a reviewer reads which constraint failed and which
    # timeline positions it selected without opening a sidecar. Empty when the
    # pack declared no trace checks, or when the timeline carried no events for
    # them to read. See docs/OUTPUT_FORMAT.md.
    trace_check_results: list[TraceConstraintResult] = Field(default_factory=list)
    # Per-criterion rubric-judge breakdown. ``None`` when no LLM judge ran;
    # an empty list is distinct (judge ran, rubric had no scorable criteria).
    criterion_results: list[CriterionResult] | None = None
    # Rubric-judge outcome. ``UNSPECIFIED`` when no judge was configured;
    # ``ERRORED`` means the judge malfunctioned and the ``llm_judge`` component
    # carries NO score (it must NOT be read as 0.0). See JudgeStatus.
    judge_status: JudgeStatus = JudgeStatus.UNSPECIFIED
    # The judge's own token usage / cost. ``None`` when no judge ran; populated
    # for both COMPLETED and ERRORED runs (an errored judge still spent tokens).
    judge_usage: JudgeUsage | None = None
    # The judge's full message transcript (role / content / tool_calls dicts) —
    # the audit channel for WHY a criterion was scored as it was. Written to a
    # sidecar ``judge_trajectory.yaml`` artifact, not inlined in ``grade.yaml``,
    # so the grade stays scannable (mirrors the trajectory/prompts split). See
    # docs/OUTPUT_FORMAT.md. ``None`` when no judge ran or none was captured.
    judge_transcript: list[dict[str, Any]] | None = None
    # The judge's knowledge-search gating for this trial (offered / withheld / whether
    # config disabled KB). ``None`` when no judge ran. Serialized inline in
    # ``grade.yaml`` (a scalar/lists block, unlike the transcript sidecar). Separate
    # from ``judge_usage``, which stays token/cost only. See docs/OUTPUT_FORMAT.md.
    judge_kb_gating: JudgeKbGating | None = None
    # The judge's non-derivable ``run()`` inputs (state-diff string + non-KB
    # read-tool surface). ``None`` when no judge ran. Excluded from the
    # ``grade.yaml`` payload and written to a sidecar ``judge_inputs.yaml`` (the
    # state-diff can be large), mirroring the ``judge_transcript`` split. See
    # docs/OUTPUT_FORMAT.md.
    judge_inputs: JudgeInputs | None = None
    # Whether the judge ran with a custom system-prompt body
    # (grading.llm_judge.customization.system_prompt). Tri-state: ``None`` when no
    # judge ran, ``False``/``True`` when a judge ran with the default / a custom
    # prompt. Serialized inline in ``grade.yaml``. The full custom text is recorded
    # in ``task.yaml.grading_config.llm_judge.customization``, not here. See
    # docs/OUTPUT_FORMAT.md.
    judge_custom_prompt: bool | None = None
    # Whether the harness embedded the agent's policy / system prompt in the judge's
    # opening-message evidence
    # (grading.llm_judge.customization.include_agent_system_prompt). Tri-state:
    # ``None`` when no judge ran, ``True``/``False`` when a judge ran with the agent
    # policy included / gated out. Serialized inline in ``grade.yaml``. See
    # docs/OUTPUT_FORMAT.md.
    judge_agent_prompt_included: bool | None = None
