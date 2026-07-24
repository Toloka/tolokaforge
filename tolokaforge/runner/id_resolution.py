"""Primary-key resolution and diff-op computation for record dicts.

Central home for the "given a table, what column is the primary key, and what
diff mutations does before→after produce?" question. Callers with a Pydantic
model class (:class:`DBServiceProxy`) resolve keys via ``_resolve_id_field``;
callers with raw record dicts (MCP subprocess sync in
:mod:`tolokaforge.runner.service`, Tau in-memory sync in
:mod:`tolokaforge.runner.tool_factory`) use :func:`compute_diff_ops`.

All paths agree on the key name via :func:`id_field_for_table`: configured
``state_checks.id_fields`` entry wins, otherwise ``"id"``. Value lookup and diff
computation are fail-loud — a record without its resolved key raises rather
than silently collapsing into a single ``None`` bucket.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class IdFieldResolutionError(ValueError):
    """A table's primary-key field or value cannot be determined."""


def id_field_for_table(table: str, id_fields: dict[str, str]) -> str:
    """Return the primary-key field name for ``table``.

    Configured ``state_checks.id_fields`` entry wins; otherwise ``"id"``. Match
    :meth:`DBServiceProxy._resolve_id_field`'s truthiness semantics (``or``, not
    ``.get(..., "id")``) so a blank configured value falls through to the
    default rather than diverging.
    """
    return id_fields.get(table) or "id"


def resolve_record_id(
    record: dict[str, Any],
    table: str,
    id_fields: dict[str, str],
) -> Any:
    """Return ``record``'s primary-key value using the table's configured key.

    Fail loud when the resolved key is absent from the record.
    """
    key = id_field_for_table(table, id_fields)
    return _record_value(record, table, key)


def _record_value(record: dict[str, Any], table: str, key: str) -> Any:
    """Return ``record[key]`` or raise :class:`IdFieldResolutionError`."""
    if key not in record:
        raise IdFieldResolutionError(
            f"Record in table {table!r} is missing key field {key!r}. "
            f"Resolved from state_checks.id_fields (default 'id'). "
            f"Fix either: (a) declare the correct key in grading.yaml "
            f"state_checks.id_fields.{table}, "
            f"or (b) ensure records include the {key!r} field. "
            f"Record keys: {sorted(record.keys())}"
        )
    return record[key]


def compute_diff_ops(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    table: str,
    id_fields: dict[str, str],
) -> list[dict[str, Any]]:
    """Compute the mutation ops that transform ``before`` into ``after``.

    Emits ``insert`` for records only in ``after``, ``upsert`` for records
    present in both but changed, and ``delete`` for records only in ``before``.
    Records are indexed by the table's configured key (see
    :func:`id_field_for_table`); a record missing that key raises.
    """
    key = id_field_for_table(table, id_fields)
    before_by_id = {_record_value(r, table, key): r for r in before}
    after_by_id = {_record_value(r, table, key): r for r in after}

    ops: list[dict[str, Any]] = []
    for rid, rec in after_by_id.items():
        if rid not in before_by_id:
            ops.append({"op": "insert", "record": rec})
        elif before_by_id[rid] != rec:
            ops.append({"op": "upsert", "record": rec, "key": key})
    for rid in before_by_id:
        if rid not in after_by_id:
            ops.append({"op": "delete", "filter": {key: rid}})
    return ops


def check_id_fields_reference_known_tables(
    id_fields: dict[str, str],
    known_tables: list[str] | set[str],
    *,
    context: str,
    relaxed: bool,
) -> str | None:
    """Validate that every ``id_fields`` key names a table that exists.

    Returns ``None`` when the check passes or the mismatch is downgraded via
    ``relaxed=True`` (a warning is logged in that case). Returns the error
    message string on a strict failure so callers can raise or return it.

    ``context`` prefixes the message (e.g. task id or ``"RegisterTrial: <trial>"``).
    """
    if not id_fields:
        return None
    unknown = sorted(set(id_fields) - set(known_tables))
    if not unknown:
        return None
    known = sorted(known_tables)
    msg = (
        f"[{context}] state_checks.id_fields references table(s) not present "
        f"in initial_state: unknown={unknown}, known={known}. "
        f"Fix a typo, add the table to initial_state, or set "
        f"state_checks.relaxed_validation: true."
    )
    if relaxed:
        logger.warning(msg)
        return None
    return msg
