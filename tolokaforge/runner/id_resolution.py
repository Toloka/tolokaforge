"""Primary-key resolution and diff-op computation for record dicts.

Central home for the "given a table, which ordered fields form the primary
key, and what diff mutations does before→after produce?" question. Callers
with a Pydantic model class (:class:`DBServiceProxy`) resolve keys via
``_resolve_table_key``; callers with raw record dicts (MCP subprocess sync in
:mod:`tolokaforge.runner.service`, Tau in-memory sync in
:mod:`tolokaforge.runner.tool_factory`) use :func:`compute_diff_ops`.

A table's key is a :class:`TableKey` — a non-empty, ordered tuple of field
names resolved by :func:`table_key`: the configured ``state_checks.id_fields``
entry wins (a string names one field, a list names the ordered components),
otherwise ``("id",)``. Value lookup and diff computation are fail-loud — a
record missing any key component, holding an unhashable component value, or
sharing its full key value with another record raises
:class:`IdFieldResolutionError` rather than silently collapsing records.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_KEY_FIELD = "id"


class IdFieldResolutionError(ValueError):
    """A table's primary-key field or value cannot be determined."""


@dataclass(frozen=True)
class TableKey:
    """Ordered primary-key field names for one table."""

    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.fields:
            raise IdFieldResolutionError("TableKey requires at least one field name")


def table_key(table: str, id_fields: Mapping[str, str | list[str]]) -> TableKey:
    """Return the ordered primary-key fields for ``table``.

    A configured ``state_checks.id_fields`` entry wins: a string names one
    field, a list names the ordered components. Match
    :meth:`DBServiceProxy._resolve_table_key`'s truthiness semantics (``or``,
    not ``.get(..., default)``) so an absent table, a blank string, and an
    empty list all fall through to the ``("id",)`` default rather than
    diverging.
    """
    declared = id_fields.get(table)
    if not declared:
        return TableKey((_DEFAULT_KEY_FIELD,))
    if isinstance(declared, str):
        fields: tuple[str, ...] = (declared,)
    elif isinstance(declared, list):
        fields = tuple(declared)
    else:
        raise IdFieldResolutionError(
            f"state_checks.id_fields.{table} must be a field name or a list of "
            f"field names, got {declared!r}"
        )
    invalid = [f for f in fields if not isinstance(f, str) or not f]
    if invalid:
        raise IdFieldResolutionError(
            f"state_checks.id_fields.{table} declares invalid key component(s) "
            f"{invalid!r} in {list(fields)!r}; every component must be a "
            f"non-empty field name."
        )
    return TableKey(fields)


def resolve_record_id(
    record: dict[str, Any],
    table: str,
    id_fields: Mapping[str, str | list[str]],
) -> tuple[Any, ...]:
    """Return ``record``'s primary-key value, one component per declared field.

    Always a tuple, including the single-field case. Fail loud when any key
    component is absent from the record or holds an unhashable value.
    """
    key = table_key(table, id_fields)
    values = []
    for field in key.fields:
        if field not in record:
            raise IdFieldResolutionError(
                f"Record in table {table!r} is missing key field {field!r} "
                f"(declared key {list(key.fields)!r}). "
                f"Resolved from state_checks.id_fields (default 'id'). "
                f"Fix either: (a) declare the correct key in grading.yaml "
                f"state_checks.id_fields.{table}, "
                f"or (b) ensure records include the {field!r} field. "
                f"Record keys: {sorted(record.keys())}"
            )
        value = record[field]
        if not _is_hashable(value):
            raise IdFieldResolutionError(
                f"Record in table {table!r} has an unhashable key component "
                f"{field!r}={value!r} (declared key {list(key.fields)!r}); "
                f"key values must be hashable scalars."
            )
        values.append(value)
    return tuple(values)


def _is_hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


def compute_diff_ops(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    table: str,
    id_fields: Mapping[str, str | list[str]],
) -> list[dict[str, Any]]:
    """Compute the mutation ops that transform ``before`` into ``after``.

    Emits ops in the order ``[inserts, upserts, deletes]`` so downstream
    consumers see the same batch shape as the callers expect. Records
    are indexed by the table's key tuple (see :func:`table_key`); a record
    missing any key component or sharing its full key value with another
    record in the same list raises :class:`IdFieldResolutionError`.

    A single-field table emits ``key`` as a plain string and a one-field
    delete filter; a composite table emits ``key`` as the ordered component
    list and a delete filter carrying every component.
    """
    key = table_key(table, id_fields)
    before_by_id = _index_by_key(before, table, id_fields, side="before")
    after_by_id = _index_by_key(after, table, id_fields, side="after")

    wire_key: str | list[str] = key.fields[0] if len(key.fields) == 1 else list(key.fields)
    ops: list[dict[str, Any]] = []
    for rid, rec in after_by_id.items():
        if rid not in before_by_id:
            ops.append({"op": "insert", "record": rec})
    for rid, rec in after_by_id.items():
        if rid in before_by_id and before_by_id[rid] != rec:
            ops.append({"op": "upsert", "record": rec, "key": wire_key})
    for rid in before_by_id:
        if rid not in after_by_id:
            ops.append({"op": "delete", "filter": dict(zip(key.fields, rid, strict=True))})
    return ops


def _index_by_key(
    records: list[dict[str, Any]],
    table: str,
    id_fields: Mapping[str, str | list[str]],
    *,
    side: str,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """Index ``records`` by their key tuple; raise on duplicates.

    A duplicate resolved key would silently drop every record but the last —
    the same class of silent-collapse the id_fields resolver was introduced
    to close.
    """
    key = table_key(table, id_fields)
    indexed: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        rid = resolve_record_id(record, table, id_fields)
        if rid in indexed:
            components = dict(zip(key.fields, rid, strict=True))
            raise IdFieldResolutionError(
                f"Duplicate key {components!r} in {side} state for table {table!r}; "
                f"two records share the same resolved primary key. "
                f"Fix the source data or the state_checks.id_fields entry."
            )
        indexed[rid] = record
    return indexed


def _first_duplicate_key_value(
    records: list[dict[str, Any]],
    key: TableKey,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Return the first key value two of ``records`` share plus those records.

    A record lacking a component projects it as ``None`` here, so the caller
    receives the colliding records and can tell a genuine duplicate from rows
    that simply do not carry a component. Equality scan rather than a hash
    index, so an unhashable seeded component value cannot crash the gate
    before it reports.
    """
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for record in records:
        value = tuple(record.get(field) for field in key.fields)
        for prior_value, prior_record in seen:
            if prior_value == value:
                return dict(zip(key.fields, value, strict=True)), [prior_record, record]
        seen.append((value, record))
    return None


def id_fields_findings(
    id_fields: Mapping[str, str | list[str]],
    tables: Mapping[str, list[dict[str, Any]]],
) -> list[str]:
    """Return one sentence per defect in ``id_fields`` against ``tables``.

    Three findings, each carrying its own remediation: a declared table absent
    from ``tables``, a declared key component absent from the union of the
    table's seeded records' keys, and a declared key — single or composite —
    that does not uniquely identify the table's seeded records. The
    record-level checks are skipped for a table seeded empty — its rows may
    arrive via ``initialization_actions``, which this gate cannot see — and the
    uniqueness check is skipped when a component is absent from every record,
    because the component finding already owns that defect. A collision
    between records that merely lack a component (an absent component projects
    as ``None`` in the scan) is likewise reported as the component defect, not
    as a ``None``-valued duplicate.

    Empty on a clean declaration and on an empty ``id_fields``.
    """
    if not id_fields:
        return []
    findings: list[str] = []
    unknown = sorted(set(id_fields) - set(tables))
    if unknown:
        findings.append(
            f"state_checks.id_fields references table(s) not present "
            f"in initial_state: unknown={unknown}, known={sorted(tables)}. "
            f"Fix a typo, add the table to initial_state, or set "
            f"state_checks.relaxed_validation: true."
        )
    for table in sorted(set(id_fields) & set(tables)):
        records = tables[table]
        if not records:
            continue
        key = table_key(table, id_fields)
        seeded_fields = set().union(*(record.keys() for record in records))
        absent = [f for f in key.fields if f not in seeded_fields]
        if absent:
            findings.append(
                f"state_checks.id_fields[{table!r}] declares key component(s) "
                f"{absent} absent from every seeded record of table {table!r} "
                f"(seeded record fields: {sorted(seeded_fields)}). "
                f"Fix a typo, seed a record carrying the field(s), or set "
                f"state_checks.relaxed_validation: true."
            )
            continue
        duplicate = _first_duplicate_key_value(records, key)
        if duplicate is not None:
            colliding, involved = duplicate
            lacking = sorted({f for f in key.fields for r in involved if f not in r})
            if lacking:
                findings.append(
                    f"state_checks.id_fields[{table!r}] declares key "
                    f"{list(key.fields)!r}, but key component(s) {lacking} are "
                    f"absent from seeded records of table {table!r} that "
                    f"therefore share key value {colliding!r}. Seed the "
                    f"component(s) on those records, fix the declaration, or "
                    f"set state_checks.relaxed_validation: true."
                )
            else:
                findings.append(
                    f"state_checks.id_fields[{table!r}] declares key "
                    f"{list(key.fields)!r}, which does not uniquely identify "
                    f"table {table!r}'s seeded records: key value {colliding!r} "
                    f"appears on more than one record. Declare a key that "
                    f"distinguishes the rows (a composite list widens it), fix "
                    f"the seeded data, or set "
                    f"state_checks.relaxed_validation: true."
                )
    return findings


def check_id_fields_against_seeded_tables(
    id_fields: Mapping[str, str | list[str]],
    tables: Mapping[str, list[dict[str, Any]]],
    *,
    context: str,
    relaxed: bool,
) -> str | None:
    """Report :func:`id_fields_findings` as one gate message.

    Returns ``None`` when the checks pass or the findings are downgraded via
    ``relaxed=True`` (one warning is logged in that case). Returns the findings
    space-joined behind a ``context`` prefix (e.g. task id or
    ``"RegisterTrial: <trial>"``) on a strict failure, so callers can raise or
    return it.
    """
    findings = id_fields_findings(id_fields, tables)
    if not findings:
        return None
    msg = f"[{context}] " + " ".join(findings)
    if relaxed:
        logger.warning(msg)
        return None
    return msg
