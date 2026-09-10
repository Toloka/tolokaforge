"""``critique(verdict_draft)`` — evidence-pointer tool for the agentic judge kind.

Lets an agentic judge episode (:class:`~tolokaforge.core.grading.judge_kinds.
agentic.AgenticRubricJudgeKind`) re-examine its own ``draft_report`` verdict
against evidence it already collected — the transcript, the state diff, and
any prior ``search_kb`` results from the same episode — before calling the
terminal ``submit_report``. Read-only and replay-only by construction: nothing
here issues a new knowledge-base search or any other I/O; it only re-scans
data the episode already holds.

:data:`CritiqueContext` never fabricates evidence — a criterion with no
matching line across any source simply contributes zero
:class:`EvidencePointer` entries, not a placeholder ("no evidence found")
string.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.judge import format_transcript
from tolokaforge.core.grading.judge_tools import SEARCH_KB_TOOL_NAME, read_only_policy
from tolokaforge.core.grading.rubric import (
    SubmitReportValidationError,
    VerdictConsistencyError,
    build_critique_tool_schema,
    parse_submit_report,
)
from tolokaforge.core.models import Message, MessageRole
from tolokaforge.tools.registry import Tool, ToolExecutionStatus, ToolResult

if TYPE_CHECKING:
    from tolokaforge.runner.models import Criterion, CriterionResult, Rubric

#: Regex for a ``search_kb`` hit header line rendered by
#: :class:`~tolokaforge.core.grading.judge_tools.SearchKbTool`, e.g.
#: ``"[1] Document: refund-policy"``.
_KB_HIT_HEADER = re.compile(r"^\[\d+\]\s+Document:\s*(\S+)\s*$")

#: Minimum token length for :func:`_criterion_tokens`. The matcher is a
#: deliberately naive substring scan (no semantic ranking — see module
#: docstring); filtering out short tokens (e.g. "is", "to") keeps it from
#: matching on nearly every line and staying deterministic for tests.
_MIN_TOKEN_LEN = 4


class EvidenceSource(str, Enum):
    """Where one :class:`EvidencePointer`'s text was found."""

    TRANSCRIPT = "transcript"
    STATE_DIFF = "state_diff"
    KB = "kb"


@dataclass(frozen=True)
class EvidencePointer:
    """One matching line of evidence for a single criterion.

    ``kb_doc_id`` is set only when ``source is EvidenceSource.KB`` — it names
    which prior ``search_kb`` hit the line came from.
    """

    criterion_id: str
    source: EvidenceSource
    text: str
    kb_doc_id: str | None = None


@dataclass(frozen=True)
class JudgeDraft:
    """The judge's own draft verdict, validated via ``parse_submit_report``."""

    verdicts: tuple[CriterionResult, ...]


@dataclass(frozen=True)
class CritiqueContext:
    """Evidence pointers gathered for a :class:`JudgeDraft`, across all criteria."""

    pointers: tuple[EvidencePointer, ...]


def serialize_critique_context(ctx: CritiqueContext) -> str:
    """Render ``ctx`` as the JSON string returned in the tool's ``ToolResult.output``.

    Grouped by ``criterion_id``; a criterion with zero pointers is simply
    absent from the result, never represented by a placeholder entry.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for pointer in ctx.pointers:
        grouped.setdefault(pointer.criterion_id, []).append(
            {
                "source": pointer.source.value,
                "text": pointer.text,
                "kb_doc_id": pointer.kb_doc_id,
            }
        )
    return json.dumps(grouped)


def _criterion_tokens(criterion: Criterion) -> set[str]:
    """Lowercased word tokens (len >= :data:`_MIN_TOKEN_LEN`) from a criterion's text."""
    text = (
        criterion.description
        if criterion.expected is None
        else f"{criterion.description} {criterion.expected}"
    )
    return {tok for tok in re.findall(r"[A-Za-z0-9]+", text.lower()) if len(tok) >= _MIN_TOKEN_LEN}


def _line_matches(tokens: set[str], line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in tokens)


def _iter_kb_hit_blocks(tool_output: str) -> list[tuple[str, str]]:
    """Parse ``SearchKbTool.execute``'s rendered hits into ``(doc_id, block_text)`` pairs.

    Splits on the ``"[N] Document: <doc_id>"`` header
    :class:`~tolokaforge.core.grading.judge_tools.SearchKbTool` renders per hit;
    a "no results" or error string yields no headers and so no blocks.
    """
    lines = tool_output.splitlines()
    headers = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _KB_HIT_HEADER.match(line))]
    blocks = []
    for idx, (start, doc_id) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        blocks.append((doc_id, "\n".join(lines[start:end])))
    return blocks


def _collect_kb_hits(judge_messages: list[Message]) -> list[tuple[str, str]]:
    """Replay every ``search_kb`` result already in ``judge_messages`` into hit blocks.

    Issues no new search: it only pairs each ``search_kb`` :class:`ToolCall`
    with the ``role=TOOL`` message carrying its result and re-parses that
    already-produced output.
    """
    result_by_call_id = {
        msg.tool_call_id: msg.content for msg in judge_messages if msg.role == MessageRole.TOOL
    }
    hits: list[tuple[str, str]] = []
    for msg in judge_messages:
        for call in msg.tool_calls or []:
            if call.name != SEARCH_KB_TOOL_NAME:
                continue
            output = result_by_call_id.get(call.id)
            if output is not None:
                hits.extend(_iter_kb_hit_blocks(output))
    return hits


def resolve_evidence(
    draft: JudgeDraft,
    *,
    rubric: Rubric,
    transcript: list[dict[str, Any]],
    state_diff: str | None,
    judge_messages: list[Message],
) -> CritiqueContext:
    """Scan the episode's own transcript, state diff, and replayed KB hits for evidence.

    For each criterion in ``draft``, a source line contributes an
    :class:`EvidencePointer` when it contains any token from that criterion's
    description/expected text (see :func:`_criterion_tokens`). A criterion with
    no matching line in any source contributes zero pointers.
    """
    criteria_by_id = {c.id: c for c in rubric.criteria}
    transcript_lines = format_transcript(transcript).splitlines()
    state_diff_lines = state_diff.splitlines() if state_diff else []
    kb_hits = _collect_kb_hits(judge_messages)

    pointers: list[EvidencePointer] = []
    for result in draft.verdicts:
        criterion = criteria_by_id[result.id]
        tokens = _criterion_tokens(criterion)
        pointers.extend(
            EvidencePointer(criterion_id=criterion.id, source=EvidenceSource.TRANSCRIPT, text=line)
            for line in transcript_lines
            if _line_matches(tokens, line)
        )
        pointers.extend(
            EvidencePointer(criterion_id=criterion.id, source=EvidenceSource.STATE_DIFF, text=line)
            for line in state_diff_lines
            if _line_matches(tokens, line)
        )
        pointers.extend(
            EvidencePointer(
                criterion_id=criterion.id, source=EvidenceSource.KB, text=text, kb_doc_id=doc_id
            )
            for doc_id, text in kb_hits
            if _line_matches(tokens, text)
        )
    return CritiqueContext(pointers=tuple(pointers))


class CritiqueTool(Tool):
    """The ``critique(verdict_draft)`` tool: validates a draft verdict, then returns evidence for it."""

    def __init__(
        self,
        *,
        rubric: Rubric,
        transcript: list[dict[str, Any]],
        state_diff: str | None,
        messages: list[Message],
    ):
        schema = build_critique_tool_schema(rubric)["function"]
        super().__init__(
            name=schema["name"],
            description=schema["description"],
            policy=read_only_policy(),
        )
        self._rubric = rubric
        self._transcript = transcript
        self._state_diff = state_diff
        self._messages = messages

    def get_schema(self) -> dict[str, Any]:
        return build_critique_tool_schema(self._rubric)

    def execute(self, **kwargs: Any) -> ToolResult:
        verdict_draft = kwargs.get("verdict_draft")
        if not isinstance(verdict_draft, dict):
            return ToolResult(
                success=False,
                output="",
                error=(
                    "critique requires a 'verdict_draft' object; got a flat payload — "
                    "did you mean submit_report?"
                ),
                status=ToolExecutionStatus.INVALID_ARGUMENTS,
            )
        try:
            results = parse_submit_report(verdict_draft, self._rubric)
        except (SubmitReportValidationError, VerdictConsistencyError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                status=ToolExecutionStatus.INVALID_ARGUMENTS,
            )

        draft = JudgeDraft(verdicts=tuple(results))
        ctx = resolve_evidence(
            draft,
            rubric=self._rubric,
            transcript=self._transcript,
            state_diff=self._state_diff,
            judge_messages=self._messages,
        )
        return ToolResult(
            success=True, output=serialize_critique_context(ctx), status=ToolExecutionStatus.SUCCESS
        )
