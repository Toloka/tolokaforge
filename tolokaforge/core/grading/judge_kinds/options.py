"""``JudgeTrialOptions`` — the per-trial options every :class:`JudgeKind` receives.

The judge's per-trial customization travels to :meth:`JudgeKind.evaluate` as one
frozen object, the judge analogue of
:class:`~tolokaforge.core.loop.AgentLoopContext` and
:class:`~tolokaforge.core.actors.user_simulator.UserSimulatorContext`: the
Protocol's signature carries ``options`` and does not change when a knob is added.
A new knob is a field here whose default is the behaviour without the knob, so a
kind that does not read the field grades under that default.

:func:`resolve_judge_trial_options` is the one place a task's
:class:`~tolokaforge.runner.models.JudgeCustomization` becomes options: it collapses
the tri-state fields to the values a judge runs with and lays a run-level override
over them. Offline replay builds its options from the bundle it reads instead,
recording where each value came from (:mod:`tolokaforge.core.grading.replay`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tolokaforge.core.grading.judge_transcript import TranscriptEvidence
from tolokaforge.core.grading.kb_search import DEFAULT_JUDGE_SNIPPET_CHARS

if TYPE_CHECKING:
    from tolokaforge.core.models.run_config import JudgeGraderConfig
    from tolokaforge.runner.models import JudgeCustomization, ToolSchema

__all__ = [
    "JudgeTrialOptions",
    "resolve_judge_trial_options",
    "resolve_transcript_evidence",
]


@dataclass(frozen=True)
class JudgeTrialOptions:
    """How a judge kind grades one trial, beyond the evidence it reads.

    * ``disable_knowledge_search`` — withhold every knowledge-search tool from the
      judge (``customization.disable_knowledge_search``).
    * ``custom_system_prompt`` — a body fragment that replaces the default judge
      system prompt; the marker contract is appended to it
      (``customization.system_prompt``). ``None`` keeps the default prompt.
    * ``include_agent_system_prompt`` — embed the agent's system prompt in the
      judge's evidence (``customization.include_agent_system_prompt``).
    * ``judge_snippet_chars`` — how many characters of each hit the judge's
      ``search_kb`` shows; ``None`` shows whole documents
      (``customization.judge_snippet_chars``).
    * ``transcript_evidence`` — how a transcript-only judge reads the trial
      (``customization.evidence: transcript``); ``None`` is the judge over the final
      state. A transcript judge is handed no DB reader, workspace or state diff.

    The defaults are what a task with no ``customization`` block is graded with.
    A kind honours every field as :class:`~tolokaforge.core.grading.judge.LLMJudge`
    does, and a kind wrapping another passes the object on unchanged.
    """

    disable_knowledge_search: bool = False
    custom_system_prompt: str | None = None
    include_agent_system_prompt: bool = True
    judge_snippet_chars: int | None = DEFAULT_JUDGE_SNIPPET_CHARS
    transcript_evidence: TranscriptEvidence | None = None


def resolve_judge_trial_options(
    customization: JudgeCustomization | None,
    *,
    override: JudgeGraderConfig | None = None,
    agent_tools: Iterable[ToolSchema] = (),
) -> JudgeTrialOptions:
    """The options a trial is judged with, from its task's customization.

    An unset tri-state field takes its default. ``override`` is the run-level
    ``grader.judge`` block: each of its fields that is not ``None`` wins over the
    task's value, and ``None`` inherits it. It carries no ``judge_snippet_chars``:
    its ``None`` means "inherit", so it could not express whole documents.
    ``agent_tools`` are the trial's agent tool schemas; a transcript judge reads the
    ones declaring ``mutates_state: false`` as tools whose calls cannot change state.
    """
    disable_knowledge_search = customization.disable_knowledge_search if customization else None
    custom_system_prompt = customization.system_prompt if customization else None
    include_agent_system_prompt = (
        customization.include_agent_system_prompt if customization else None
    )
    if override is not None:
        if override.disable_knowledge_search is not None:
            disable_knowledge_search = override.disable_knowledge_search
        if override.custom_system_prompt is not None:
            custom_system_prompt = override.custom_system_prompt
        if override.include_agent_system_prompt is not None:
            include_agent_system_prompt = override.include_agent_system_prompt
    return JudgeTrialOptions(
        disable_knowledge_search=bool(disable_knowledge_search),
        custom_system_prompt=custom_system_prompt,
        include_agent_system_prompt=(
            True if include_agent_system_prompt is None else include_agent_system_prompt
        ),
        judge_snippet_chars=(
            customization.judge_snippet_chars
            if customization is not None
            else DEFAULT_JUDGE_SNIPPET_CHARS
        ),
        transcript_evidence=resolve_transcript_evidence(customization, agent_tools),
    )


def resolve_transcript_evidence(
    customization: JudgeCustomization | None, agent_tools: Iterable[ToolSchema]
) -> TranscriptEvidence | None:
    """The transcript judge's reading of a trial, ``None`` for a judge over the state."""
    if customization is None or customization.evidence != "transcript":
        return None
    return TranscriptEvidence(
        tool_result_chars=customization.transcript_tool_chars,
        read_only_tools=frozenset(tool.name for tool in agent_tools if tool.mutates_state is False),
        read_only_calls=tuple(customization.read_only_calls),
    )
