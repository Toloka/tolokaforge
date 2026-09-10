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
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tolokaforge.runner.models import CriterionResult


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
