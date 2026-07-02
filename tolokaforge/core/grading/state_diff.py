"""Render a compact, human-readable ``initial → final`` state diff for the judge.

The rubric judge historically inspected the trial's final DB state by dumping it
whole (``get_db_state``) — a large, noisy JSON blob that is expensive on context
and hard for both the model and a human reviewer to read. This module produces
the *diff-first default*: a compact summary of exactly what the agent **changed**
between the pre-run initial state and the final state, injected into the judge's
opening message (and, because that message is part of the judge transcript,
persisted verbatim into ``judge_trajectory.yaml``).

Crucially this is the **initial → final** delta — the agent's own edits — NOT the
trial-vs-golden diff (``runner/grading.py:compute_state_diff``). The golden diff
reveals the expected answer and is deliberately withheld from the judge to avoid
path-matching bias (see ``docs/RUBRIC_GRADING_DESIGN.md`` Decisions #7/#8). An
initial→final delta leaks nothing about the oracle: it only says what the agent
did, which is the thing most rubrics actually grade.

The renderer is intentionally dependency-free — it takes plain dicts and returns
a string — so it is trivially unit-testable and carries no coupling to the runner
models. The caller (``runner/service.py``) extracts primary keys and unstable
fields from the trial's ``InitialStateConfig`` and passes them in as plain data.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

__all__ = ["render_state_diff"]

#: Overall cap on the rendered diff so a pathological change set can't blow the
#: judge's context window. Truncation is always flagged, never silent — matching
#: ``judge_tools._MAX_OUTPUT_CHARS``.
_MAX_OUTPUT_CHARS = 50_000

#: Per-table cap on how many added / removed rows are shown in full before the
#: rest collapse to a count. Modified rows are always shown (they carry the
#: field-level old→new that is the whole point of a diff) up to the same cap.
_MAX_ROWS_PER_CATEGORY = 20


def _fmt_value(value: Any) -> str:
    """Format a single field value unambiguously (``null``, quoted strings, …)."""
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)


def _fmt_record(record: dict[str, Any]) -> str:
    """Format a whole record as compact one-line JSON with stable key order."""
    return json.dumps(record, default=str, ensure_ascii=False, sort_keys=True)


def _pk_key(value: Any) -> str:
    """Canonical hashable key for PK matching.

    Normalizes numbers so an integer and its float twin key the same row
    (``1`` and ``1.0`` — JSON round-tripping through the DB readily flips one to
    the other, and for a primary key they denote the same entity). ``bool`` is
    kept distinct from ``int`` (Python treats ``True == 1``, which we do not want
    for a key). All other values fall back to the unambiguous JSON form.
    """
    if isinstance(value, bool):
        return f"bool:{value}"
    if isinstance(value, (int, float)):
        return f"num:{float(value)!r}"
    return _fmt_value(value)


def _strip_unstable(
    record: dict[str, Any], table: str, unstable: set[tuple[str, str]]
) -> dict[str, Any]:
    """Drop noise fields (auto-ids, timestamps, …) so the diff shows real edits."""
    if not unstable:
        return record
    return {k: v for k, v in record.items() if (table, k) not in unstable}


def _resolve_pk(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    declared_pk: str | None,
) -> str | None:
    """Pick a usable primary-key field for row matching, or ``None``.

    A field is usable when, on **each side independently**, every row carries it
    non-null and uniquely. Uniqueness is checked per side (not on the combined
    rows) — a key shared by a before-row and an after-row is exactly the match we
    want, not a duplicate. Prefers the declared schema PK, then ``id``, then the
    first ``*_id`` field. Returns ``None`` when no field reliably keys the rows —
    the caller then degrades to whole-record set matching (added/removed only).
    """

    def usable(field: str) -> bool:
        for side in (before, after):
            seen: set[str] = set()
            for r in side:
                if field not in r or r[field] is None:
                    return False
                key = _pk_key(r[field])
                if key in seen:
                    return False
                seen.add(key)
        return True

    if declared_pk and usable(declared_pk):
        return declared_pk
    sample = before or after
    if not sample:
        return declared_pk or None
    candidates = ["id"] + sorted(f for f in sample[0] if f.endswith("_id") and f != "id")
    for field in candidates:
        if usable(field):
            return field
    return None


def _field_diffs(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Field-level ``name: old → new`` fragments for changed fields only."""
    frags: list[str] = []
    for field in sorted(set(before) | set(after)):
        old, new = before.get(field), after.get(field)
        if old != new:
            frags.append(f"{field}: {_fmt_value(old)} → {_fmt_value(new)}")
    return frags


def _diff_table(
    initial_rows: list[dict[str, Any]],
    final_rows: list[dict[str, Any]],
    *,
    table: str,
    declared_pk: str | None,
    unstable: set[tuple[str, str]],
) -> tuple[list[str], list[str], list[tuple[str, list[str]]]]:
    """Return (added, removed, modified) for one table.

    ``added`` / ``removed`` are formatted record strings; ``modified`` is a list
    of ``(row_label, field_diff_fragments)``. Row matching is done on the **raw**
    records so a primary key that is itself flagged unstable (``id`` with
    ``reason="auto_id"`` is common) still correlates rows; unstable fields are
    stripped only for the displayed record and the field-level diff, so
    non-deterministic noise never shows up as a change. Output is sorted for
    determinism.
    """

    def strip(r: dict[str, Any]) -> dict[str, Any]:
        return _strip_unstable(r, table, unstable)

    # Resolve the PK on RAW rows (before stripping): the PK field must be present
    # to key rows, even when it is registered as an unstable (auto-id) field.
    pk = _resolve_pk(initial_rows, final_rows, declared_pk)

    if pk is None:
        # No reliable key: fall back to whole-record (post-strip) matching with
        # MULTISET semantics so removing one of two identical rows is not lost to
        # set collapse. Only added / removed can be reported, never modifications.
        before_counts = Counter(_fmt_record(strip(r)) for r in initial_rows)
        after_counts = Counter(_fmt_record(strip(r)) for r in final_rows)
        added = sorted((after_counts - before_counts).elements())
        removed = sorted((before_counts - after_counts).elements())
        return added, removed, []

    before_by_pk = {_pk_key(r[pk]): r for r in initial_rows}
    after_by_pk = {_pk_key(r[pk]): r for r in final_rows}

    added = sorted(
        _fmt_record(strip(row)) for key, row in after_by_pk.items() if key not in before_by_pk
    )
    removed = sorted(
        _fmt_record(strip(row)) for key, row in before_by_pk.items() if key not in after_by_pk
    )
    modified: list[tuple[str, list[str]]] = []
    for key in before_by_pk.keys() & after_by_pk.keys():
        before_row, after_row = before_by_pk[key], after_by_pk[key]
        frags = _field_diffs(strip(before_row), strip(after_row))
        if frags:
            # Label from the actual PK value (not the internal match key), and
            # from the final row so it reflects the surviving entity.
            modified.append((f"{pk}={_fmt_value(after_row[pk])}", frags))
    modified.sort(key=lambda m: m[0])

    return added, removed, modified


def render_state_diff(
    initial: dict[str, list[dict[str, Any]]],
    final: dict[str, list[dict[str, Any]]],
    *,
    primary_keys: dict[str, str] | None = None,
    unstable_fields: set[tuple[str, str]] | None = None,
    max_chars: int = _MAX_OUTPUT_CHARS,
) -> str:
    """Render the ``initial → final`` state delta as a compact diff block.

    Args:
        initial: pre-run state, ``table -> [records]``.
        final: final state after the agent's run, ``table -> [records]``.
        primary_keys: declared ``table -> pk field`` (from the task schemas);
            used to match rows across the two states. Falls back to an
            ``id`` / ``*_id`` heuristic, then to whole-record matching.
        unstable_fields: ``(table, field)`` pairs to exclude as noise
            (auto-ids, timestamps, LLM-generated content) so the diff shows
            only meaningful edits — the same set used for stable-hash grading.
        max_chars: overall cap; detail beyond it collapses with a flag.

    Returns:
        A ``===== STATE CHANGES … =====`` block. When nothing changed, the body
        is a single explicit "No changes" line so the judge can distinguish
        "agent correctly changed nothing" from "diff unavailable".
    """
    primary_keys = primary_keys or {}
    unstable_fields = unstable_fields or set()

    lines: list[str] = []
    changed_tables = 0
    unchanged: list[str] = []

    for table in sorted(set(initial) | set(final)):
        added, removed, modified = _diff_table(
            initial.get(table, []),
            final.get(table, []),
            table=table,
            declared_pk=primary_keys.get(table),
            unstable=unstable_fields,
        )
        if not (added or removed or modified):
            unchanged.append(table)
            continue

        changed_tables += 1
        counts = []
        if added:
            counts.append(f"{len(added)} added")
        if removed:
            counts.append(f"{len(removed)} removed")
        if modified:
            counts.append(f"{len(modified)} modified")
        lines.append(f"{table}: {', '.join(counts)}")

        for label, frags in modified[:_MAX_ROWS_PER_CATEGORY]:
            lines.append(f"  ~ {label}: {'; '.join(frags)}")
        if len(modified) > _MAX_ROWS_PER_CATEGORY:
            lines.append(f"  ~ … ({len(modified) - _MAX_ROWS_PER_CATEGORY} more modified)")

        for row in added[:_MAX_ROWS_PER_CATEGORY]:
            lines.append(f"  + {row}")
        if len(added) > _MAX_ROWS_PER_CATEGORY:
            lines.append(f"  + … ({len(added) - _MAX_ROWS_PER_CATEGORY} more added)")

        for row in removed[:_MAX_ROWS_PER_CATEGORY]:
            lines.append(f"  - {row}")
        if len(removed) > _MAX_ROWS_PER_CATEGORY:
            lines.append(f"  - … ({len(removed) - _MAX_ROWS_PER_CATEGORY} more removed)")

    header = "===== STATE CHANGES (what the agent changed: initial → final) ====="
    footer = "===== END STATE CHANGES ====="

    body: list[str]
    if changed_tables == 0:
        body = ["No changes detected in any table (agent left the state untouched)."]
    else:
        body = list(lines)
        if unchanged:
            body.append(f"Unchanged tables ({len(unchanged)}): {', '.join(unchanged)}")

    rendered = "\n".join([header, *body, footer])
    if len(rendered) > max_chars:
        # Trim the detail lines, keeping header/footer and a truncation flag. Use
        # query_db / get_db_state to inspect anything the diff had to drop.
        budget = max(0, max_chars - len(header) - len(footer) - 120)
        kept: list[str] = []
        running = 0
        for line in body:
            if running + len(line) + 1 > budget:
                break
            kept.append(line)
            running += len(line) + 1
        kept.append(
            "… (state diff truncated — use query_db / get_db_state to inspect "
            "the remaining changes)"
        )
        rendered = "\n".join([header, *kept, footer])

    return rendered
