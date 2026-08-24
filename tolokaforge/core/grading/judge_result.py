"""Judge outcome types — ``JudgeStatus``, ``JudgeUsage``, ``JudgeResult``.

Types-only module: no ``LLMJudge`` import, no behaviour. Every consumer of
the three outcome types imports them from here, keeping the composite
dispatch's judge-return surface reachable without pulling in the reference
impl in :mod:`tolokaforge.core.grading.judge`.

See :mod:`tolokaforge.core.grading.judge` for the ``LLMJudge`` /
``InMemoryJudge`` implementations that construct these outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tolokaforge.runner.models import CriterionResult

__all__ = [
    "JudgeResult",
    "JudgeStatus",
    "JudgeUsage",
]


class JudgeStatus(str, Enum):
    """Mirror of the proto ``JudgeStatus`` (kept as a host-side value object).

    ``ERRORED`` is the fail-loud marker: the judge malfunctioned and there is no
    trustworthy numeric score. ``COMPLETED`` means per-criterion results exist.
    ``UNSPECIFIED`` is never produced by a judge implementation (the caller uses
    it for the "no judge configured" case).
    """

    UNSPECIFIED = "unspecified"
    COMPLETED = "completed"
    ERRORED = "errored"


@dataclass(frozen=True)
class JudgeUsage:
    """The judge's own accounting — recorded to the output bundle (plan: judge cost).

    ``consistency_rejections`` counts ``submit_report`` attempts rejected for a
    verdict/justification marker mismatch (a ``VerdictConsistencyError``) on
    this trial — distinct from generic schema rejections, which are not counted.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    consistency_rejections: int = 0


@dataclass(frozen=True)
class JudgeResult:
    """Outcome of a rubric-judge run.

    ``status == ERRORED`` carries NO score (``score is None``) and NO criterion
    results — the fail-loud contract. ``status == COMPLETED`` carries the
    weighted ``score`` in ``[0, 1]``, the per-criterion ``criterion_results``,
    and ``gate_failed`` (a failed required criterion). ``reasons`` is always a
    human-readable diagnostic.
    """

    status: JudgeStatus
    usage: JudgeUsage
    reasons: str
    score: float | None = None
    binary_pass: bool | None = None
    gate_failed: bool = False
    criterion_results: tuple[CriterionResult, ...] = ()
    failed_required_ids: tuple[str, ...] = ()
    # Which KB backend(s) the judge was offered this trial — the visible signal
    # that the judge graded WITH (or WITHOUT) the knowledge base the agent used
    # (issue #95). ``("search_kb",)`` for rag-service, ``("search_policy",)`` for
    # the TypeSense passthrough, ``()`` for none offered. Surfaced verbatim into
    # ``reasons`` as a "Judge KB: …" note. Empty is NOT an error — we cannot
    # statically know a rubric needs KB — just an observability fact.
    kb_tools_offered: tuple[str, ...] = ()
    # KB-tagged tools the agent had this trial that the judge was constructed to
    # withhold (``disable_knowledge_search``). Empty when nothing was gated — either
    # the flag was off or the agent had no KB. ``knowledge_search_disabled`` records
    # the construction flag itself, so a disabled judge over a KB-less trial reads
    # ``knowledge_search_disabled=True`` with an empty ``kb_tools_withheld``.
    kb_tools_withheld: tuple[str, ...] = ()
    knowledge_search_disabled: bool = False
    # Whether the judge ran with a custom system-prompt body (the default marker
    # contract is always appended regardless). The full custom text is recorded in
    # the bundle's ``task.yaml`` grading config, not here — this is the honest bool.
    custom_system_prompt: bool = False
    # Whether the harness was configured to embed the agent's policy / system
    # prompt in the judge's opening-message evidence. Records the construction
    # setting, not whether a block physically appeared — a trial with an empty
    # agent prompt still reads ``True`` when the setting is default/on. Default
    # ``True``: the harness includes the agent policy unless gated off.
    include_agent_system_prompt: bool = True
    # Non-KB read-only tools the judge was offered this trial: ``get_db_state`` /
    # ``query_db`` (a DB reader was supplied), ``read_file`` (a workspace existed).
    # The KB surface is ``kb_tools_offered`` / ``kb_tools_withheld``. Recorded so an
    # offline replay knows which live backends to shim.
    read_tools_offered: tuple[str, ...] = ()
    # The ``initial → final`` state-delta string handed to the judge as its primary
    # outcome view (``None`` when no diff was built). Echoed from the ``run()`` input
    # so an offline replay can rebuild the judge's opening message from this exact
    # string rather than re-reading a live DB.
    state_diff: str | None = None
    # The judge's own message transcript (role / content / tool_calls dicts),
    # captured for audit/reproducibility (plan open question #2). Populated for
    # both COMPLETED and ERRORED runs — an errored judge's partial transcript is
    # the most useful artifact for debugging WHY it failed.
    transcript: tuple[dict[str, Any], ...] = ()
