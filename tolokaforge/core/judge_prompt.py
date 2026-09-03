"""Judge system-prompt composition — the data shape recorded and replayed.

The composed prompt (body + marker contract) is what the LLM judge runs under
and what a trial bundle records in ``prompts.yaml`` under the ``judge_prompt``
key. Three consumers share this composition — the judge itself
(``tolokaforge.core.grading.judge.LLMJudge``), the trial bundle writer (via
``InProcessConductor._write_artifacts``), and offline replay (Stage 3 in
``docs/OUTPUT_FORMAT.md`` § replays) — so the constants and composition helpers
live in a seam-neutral module rather than inside the judge implementation. That
way the writer path can derive the recorded prompt without pulling
``LLMClient`` or judge tooling onto its import graph, and the orchestration
surface (``conductor``) can compute the recorded string without reaching into
``core.grading.*``.

The public entry point ``effective_judge_system_prompt(llm_judge_config)`` is
re-exported from :mod:`tolokaforge.core.grading.judge` so external readers keep
one canonical import path.
"""

from __future__ import annotations

from tolokaforge.runner.models import LLMJudgeConfig

#: Author-facing default judge grading-stance body. Replaced verbatim when
#: ``grading.llm_judge.customization.system_prompt`` is set; the harness always
#: appends :data:`_JUDGE_MARKER_CONTRACT` regardless, so ``submit_report``
#: validation is unbreakable.
_JUDGE_SYSTEM_PROMPT_BODY = (
    "You are a strict, evidence-based grading judge. You evaluate an AI agent's "
    "work against a rubric of independent criteria. Each criterion is "
    "self-contained: its text states everything that must be checked. Grade each "
    "criterion exactly as written — nothing more, nothing less. Do not import "
    "outside expectations, and do not excuse a failure for reasons the criterion "
    "does not name. Read each rubric carefully, check the real conversation and "
    "tool calls and don't overthink your decision. Apply criteria - make "
    "decision. Your main evidence is the agent's transcript and, when provided, a "
    "database state diff shown below, plus any read-only tools you are given. If "
    "the transcript and diff settle every criterion, call submit_report without "
    "using tools. Use the read-only tools only for evidence the provided context "
    "does not settle — for example absence or invariant checks, or full final "
    "values not shown in the transcript. Never guess and it's better to call the "
    "tool if you are not sure. A criterion passes only on positive evidence that "
    "the described behavior occurred as stated. If the behavior a criterion "
    "describes never occurred in the trajectory, the criterion FAILS — unless the "
    "criterion's own text explicitly states that it passes when the situation "
    "never arises."
)

#: The enforced output contract — the marker form ``parse_submit_report`` validates.
#: Appended to every judge system prompt (default or custom); the sole source of
#: the marker sentence.
_JUDGE_MARKER_CONTRACT = (
    "For every criterion, write the evidence-based justification "
    "first and commit the verdict after it; end each justification with a final "
    "line 'VERDICT: MET' or 'VERDICT: NOT MET' (binary) / 'SCORE: <value in "
    "[0,1]>' (graded), and make that criterion's verdict field match it. When you "
    "have judged every criterion, call submit_report exactly once."
)

_JUDGE_SYSTEM_PROMPT = f"{_JUDGE_SYSTEM_PROMPT_BODY} {_JUDGE_MARKER_CONTRACT}"


def _compose_judge_system_prompt(custom_system_prompt: str | None) -> str:
    """Compose the judge system prompt, always ending with the marker contract.

    ``None`` yields the byte-for-byte default prompt; a custom body replaces the
    default grading stance while the marker contract stays appended, so
    ``submit_report`` validation can never be silently broken.
    """
    if custom_system_prompt is None:
        return _JUDGE_SYSTEM_PROMPT
    return f"{custom_system_prompt.strip()}\n\n{_JUDGE_MARKER_CONTRACT}"


def effective_judge_system_prompt(llm_judge_config: LLMJudgeConfig | None) -> str | None:
    """The composed judge system prompt for a trial's effective grading config.

    Returns the body + marker exactly as the judge would run under, so the trial
    bundle can record it verbatim without invoking the judge — an auto-fail trial
    that never called the judge still stamps the contract it would have graded
    under. ``None`` when the task has no LLM judge configured. The ``rubric_brief``
    the judge appends at run time is NOT included: the rubric already lives in
    ``task.yaml``, and duplicating it would introduce a second source of truth.
    """
    if llm_judge_config is None:
        return None
    customization = llm_judge_config.customization
    return _compose_judge_system_prompt(customization.system_prompt if customization else None)


__all__ = ["effective_judge_system_prompt"]
