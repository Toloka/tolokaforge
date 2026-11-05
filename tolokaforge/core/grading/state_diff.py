"""Render a compact, human-readable ``initial → final`` state diff for the judge.

The rubric judge historically inspected the trial's final DB state by dumping it
whole (``get_db_state``) — a large, noisy JSON blob that is expensive on context
and hard for both the model and a human reviewer to read. This module produces
the *diff-first default*: a compact summary of exactly what the agent **changed**
between the pre-run initial state and the final state, injected into the judge's
opening message (and, because that message is part of the judge transcript,
persisted verbatim into ``judge_trajectory.yaml``).

Crucially this is the **initial → final** delta — the agent's own edits — NOT the
trial-vs-golden diff (``core/grading/trial_golden_diff.py:compute_state_diff``). The
golden diff reveals the expected answer and is deliberately withheld from the judge to
avoid path-matching bias (see ``docs/RUBRIC_GRADING_DESIGN.md`` Decisions #7/#8). An
initial→final delta leaks nothing about the oracle: it only says what the agent
did, which is the thing most rubrics actually grade.

The renderer is intentionally dependency-free — it takes plain dicts and returns
a string — so it is trivially unit-testable and carries no coupling to the runner
models. The caller (``runner/service.py``) passes primary keys — the trial's
declared ``state_checks.id_fields`` layered over the task schemas — and the
unstable fields from ``InitialStateConfig``, all as plain data.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from functools import partial
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

#: A changed field whose JSON runs past the diff's ``max_chars`` cannot be shown
#: whole: its line would be cut with every line after it. It is diffed path by
#: path instead, down to the values that changed, and no value is serialised
#: past the size it is shown at. A world kept as one row whose single field
#: nests every table is the case this serves: shown whole, it was dumped twice
#: — gigabytes of strings in the grading process — to render a truncation flag.
#: The longest a value under such a path is shown before it is cut.
_MAX_NESTED_VALUE_CHARS = 2_000

#: How many changed paths of one large field are listed before the rest
#: collapse to a count.
_MAX_PATHS_PER_FIELD = 50

#: ``_fmt_value``'s encoding, chunk by chunk: the same text, abandoned at a limit.
_ENCODER = json.JSONEncoder(default=str, ensure_ascii=False, sort_keys=True)


def _fmt_value(value: Any) -> str:
    """Format a single field value unambiguously (``null``, quoted strings, …)."""
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)


def _fmt_record(record: dict[str, Any]) -> str:
    """Format a whole record as compact one-line JSON with stable key order."""
    return json.dumps(record, default=str, ensure_ascii=False, sort_keys=True)


def _bounded_json(value: Any, limit: int) -> str | None:
    """``_fmt_value(value)`` when it is at most ``limit`` characters, else ``None``.

    A non-empty ``dict`` or ``list`` is encoded chunk by chunk and given up at
    the limit, so refusing a large value costs ``limit`` characters, not the
    value's whole JSON.
    """
    if isinstance(value, (dict, list)) and value:
        chunks: list[str] = []
        size = 0
        for chunk in _ENCODER.iterencode(value):
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
        return "".join(chunks)
    if isinstance(value, str) and len(value) > limit:
        return None
    text = _fmt_value(value)
    return text if len(text) <= limit else None


def _cut_json(value: Any, limit: int) -> str:
    """``value``'s JSON, or its first ``limit`` characters and a flag when longer."""
    text = _bounded_json(value, limit)
    if text is not None:
        return text
    if isinstance(value, str):
        head = _fmt_value(value[:limit])
    elif isinstance(value, (dict, list)):
        chunks: list[str] = []
        size = 0
        for chunk in _ENCODER.iterencode(value):
            chunks.append(chunk)
            size += len(chunk)
            if size >= limit:
                break
        head = "".join(chunks)
    else:
        head = _fmt_value(value)
    return f"{head[:limit]}… (cut)"


def _record_digest(record: dict[str, Any]) -> bytes:
    """A whole-record match key that holds 32 bytes, not the record's JSON."""
    # ``surrogatepass``: a JSON parse keeps a lone surrogate (``"\\ud800"``), which
    # strict UTF-8 refuses to encode.
    return hashlib.sha256(_fmt_record(record).encode("utf-8", "surrogatepass")).digest()


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


def _row_key(record: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, ...]:
    """Hashable match key for one row: ``_pk_key`` per component, independently."""
    return tuple(_pk_key(record[f]) for f in fields)


def _resolve_pk(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    declared_pk: str | Sequence[str] | None,
) -> tuple[str, ...] | None:
    """Pick a usable primary-key field tuple for row matching, or ``None``.

    A key is usable when, on **each side independently**, every row carries all
    of its components non-null and the component tuple is unique. Uniqueness is
    checked per side (not on the combined rows) — a key shared by a before-row
    and an after-row is exactly the match we want, not a duplicate. Prefers the
    declared key (a single field, or an ordered component list for a composite
    key), then ``id``, then the first ``*_id`` field; the heuristic only ever
    tries single fields, so an undeclared composite-keyed table falls through.
    Returns ``None`` when no key reliably identifies the rows — the caller then
    degrades to whole-record set matching (added/removed only).
    """

    def usable(fields: tuple[str, ...]) -> bool:
        for side in (before, after):
            seen: set[tuple[str, ...]] = set()
            for r in side:
                if any(f not in r or r[f] is None for f in fields):
                    return False
                key = _row_key(r, fields)
                if key in seen:
                    return False
                seen.add(key)
        return True

    declared = (declared_pk,) if isinstance(declared_pk, str) else tuple(declared_pk or ())
    if declared and usable(declared):
        return declared
    sample = before or after
    if not sample:
        return None
    candidates = ["id"] + sorted(f for f in sample[0] if f.endswith("_id") and f != "id")
    for field in candidates:
        if usable((field,)):
            return (field,)
    return None


class _PathDiffs:
    """The changed paths under one large field: the first few rendered, the rest counted."""

    def __init__(self) -> None:
        self.shown: list[str] = []
        self.more = 0

    def add(self, render: Callable[[], str]) -> None:
        if len(self.shown) < _MAX_PATHS_PER_FIELD:
            self.shown.append(render())
        else:
            self.more += 1


#: A key one side of a nested dict lacks: shown as ``(absent)``, never as ``null``,
#: so a key dropped from a large field is not mistaken for no change.
_ABSENT = object()

#: A key that reads unambiguously after a ``.`` in a path; any other is quoted.
_PLAIN_KEY = re.compile(r"[A-Za-z0-9_@:-]+")


def _too_large(value: Any) -> bool:
    return _bounded_json(value, _MAX_NESTED_VALUE_CHARS) is None


def _shown(value: Any) -> str:
    return "(absent)" if value is _ABSENT else _cut_json(value, _MAX_NESTED_VALUE_CHARS)


def _key_path(path: str, key: Any) -> str:
    if isinstance(key, str) and _PLAIN_KEY.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{_fmt_value(key)}]"


def _walk(path: str, old: Any, new: Any, out: _PathDiffs) -> None:
    """Record where ``old`` and ``new`` differ under ``path``.

    Two dicts are descended into, as a row's fields are, and so are two lists
    either side of which is too large to show; anything else that differs is
    one ``path: old → new`` line, each side cut to ``_MAX_NESTED_VALUE_CHARS``.
    """
    if old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            _walk(_key_path(path, key), old.get(key, _ABSENT), new.get(key, _ABSENT), out)
        return
    if isinstance(old, list) and isinstance(new, list) and (_too_large(old) or _too_large(new)):
        _walk_list(path, old, new, out)
        return
    out.add(lambda: f"{path}: {_shown(old)} → {_shown(new)}")


def _line(sign: str, path: str, value: Any) -> str:
    """One added (``+``) or removed (``-``) element of a nested list."""
    return f"{sign} {path}: {_shown(value)}"


def _walk_list(path: str, old: list[Any], new: list[Any], out: _PathDiffs) -> None:
    """Descend into two lists: rows matched by a key when they have one, else by position."""

    rows = all(isinstance(r, dict) for r in old) and all(isinstance(r, dict) for r in new)
    pk = _resolve_pk(old, new, None) if rows else None
    if pk is None:
        for i in range(min(len(old), len(new))):
            _walk(f"{path}[{i}]", old[i], new[i], out)
        for i in range(len(old), len(new)):
            out.add(partial(_line, "+", f"{path}[{i}]", new[i]))
        for i in range(len(new), len(old)):
            out.add(partial(_line, "-", f"{path}[{i}]", old[i]))
        return

    def at(row: dict[str, Any]) -> str:
        return f"{path}[{', '.join(f'{f}={_fmt_value(row[f])}' for f in pk)}]"

    before = {_row_key(r, pk): r for r in old}
    after = {_row_key(r, pk): r for r in new}
    if [k for k in before if k in after] != [k for k in after if k in before]:
        out.add(lambda: f"{path}: order changed")
    for key, row in after.items():
        if key in before:
            _walk(at(row), before[key], row, out)
        else:
            out.add(partial(_line, "+", at(row), row))
    for key, row in before.items():
        if key not in after:
            out.add(partial(_line, "-", at(row), row))


def _field_diffs(
    before: dict[str, Any], after: dict[str, Any], limit: int
) -> tuple[list[str], bool]:
    """Field-level ``name: old → new`` fragments for changed fields only.

    A field either side of which runs past ``limit`` characters is diffed path
    by path instead (:func:`_walk`); the flag says whether any was, since those
    fragments render one per line.
    """
    frags: list[str] = []
    nested = False
    for field in sorted(set(before) | set(after)):
        old, new = before.get(field), after.get(field)
        if old == new:
            continue
        old_text, new_text = _bounded_json(old, limit), _bounded_json(new, limit)
        if old_text is not None and new_text is not None:
            frags.append(f"{field}: {old_text} → {new_text}")
            continue
        nested = True
        paths = _PathDiffs()
        _walk(field, old, new, paths)
        frags.extend(paths.shown)
        if paths.more:
            frags.append(f"… ({paths.more} more changed paths)")
        if not paths.shown:
            # The walk compares by its own rules; a difference it cannot place still is one.
            frags.append(f"{field}: changed (not shown)")
    return frags, nested


def _diff_table(
    initial_rows: list[dict[str, Any]],
    final_rows: list[dict[str, Any]],
    *,
    table: str,
    declared_pk: str | Sequence[str] | None,
    unstable: set[tuple[str, str]],
    max_chars: int = _MAX_OUTPUT_CHARS,
) -> tuple[list[str], list[str], list[tuple[str, list[str], bool]]]:
    """Return (added, removed, modified) for one table.

    ``added`` / ``removed`` are formatted record strings; ``modified`` is a list
    of ``(row_label, field_diff_fragments, nested)``. Row matching is done on the **raw**
    records so a primary key that is itself flagged unstable (``id`` with
    ``reason="auto_id"`` is common) still correlates rows; unstable fields are
    stripped only for the displayed record and the field-level diff, so
    non-deterministic noise never shows up as a change. Output is sorted for
    determinism. A record longer than ``max_chars`` is shown cut to
    ``_MAX_NESTED_VALUE_CHARS``.
    """

    def strip(r: dict[str, Any]) -> dict[str, Any]:
        return _strip_unstable(r, table, unstable)

    def shown(r: dict[str, Any]) -> str:
        record = strip(r)
        text = _bounded_json(record, max_chars)
        return text if text is not None else _cut_json(record, _MAX_NESTED_VALUE_CHARS)

    # Resolve the PK on RAW rows (before stripping): the PK field must be present
    # to key rows, even when it is registered as an unstable (auto-id) field.
    pk = _resolve_pk(initial_rows, final_rows, declared_pk)

    if pk is None:
        # No reliable key: fall back to whole-record (post-strip) matching with
        # MULTISET semantics so removing one of two identical rows is not lost to
        # set collapse. Only added / removed can be reported, never modifications.
        # Records are counted by digest, so the table is never held as JSON.
        def counted(rows: list[dict[str, Any]]) -> tuple[Counter[bytes], dict[bytes, Any]]:
            counts: Counter[bytes] = Counter()
            first: dict[bytes, Any] = {}
            for r in rows:
                digest = _record_digest(strip(r))
                counts[digest] += 1
                first.setdefault(digest, r)
            return counts, first

        before_counts, before_rows = counted(initial_rows)
        after_counts, after_rows = counted(final_rows)
        added = sorted(shown(after_rows[d]) for d in (after_counts - before_counts).elements())
        removed = sorted(shown(before_rows[d]) for d in (before_counts - after_counts).elements())
        return added, removed, []

    before_by_pk = {_row_key(r, pk): r for r in initial_rows}
    after_by_pk = {_row_key(r, pk): r for r in final_rows}

    added = sorted(shown(row) for key, row in after_by_pk.items() if key not in before_by_pk)
    removed = sorted(shown(row) for key, row in before_by_pk.items() if key not in after_by_pk)
    modified: list[tuple[str, list[str], bool]] = []
    for key in before_by_pk.keys() & after_by_pk.keys():
        before_row, after_row = before_by_pk[key], after_by_pk[key]
        frags, nested = _field_diffs(strip(before_row), strip(after_row), max_chars)
        if frags:
            # Label from the actual PK values (not the internal match key), and
            # from the final row so it reflects the surviving entity.
            label = ", ".join(f"{f}={_fmt_value(after_row[f])}" for f in pk)
            modified.append((label, frags, nested))
    modified.sort(key=lambda m: m[0])

    return added, removed, modified


def render_state_diff(
    initial: dict[str, list[dict[str, Any]]],
    final: dict[str, list[dict[str, Any]]],
    *,
    primary_keys: Mapping[str, str | Sequence[str]] | None = None,
    unstable_fields: set[tuple[str, str]] | None = None,
    max_chars: int = _MAX_OUTPUT_CHARS,
) -> str:
    """Render the ``initial → final`` state delta as a compact diff block.

    Args:
        initial: pre-run state, ``table -> [records]``.
        final: final state after the agent's run, ``table -> [records]``.
        primary_keys: declared ``table -> key`` — a single field name or an
            ordered list of component fields for a composite-keyed table; used
            to match rows across the two states, comparing every component.
            Falls back to an ``id`` / ``*_id`` heuristic, then to whole-record
            matching.
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
            max_chars=max_chars,
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

        for label, frags, nested in modified[:_MAX_ROWS_PER_CATEGORY]:
            if nested:
                lines.append(f"  ~ {label}:")
                lines.extend(f"      {frag}" for frag in frags)
            else:
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
