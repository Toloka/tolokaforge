"""A declared comparison view, checked against the task it is declared on, at load.

``ComparisonViewConfig`` validates the block on its own; what it cannot see is the
task around it. This module reads the two together, wherever a task loads — the native
adapter's ``to_task_description`` and ``get_grading_config``, the runner's
``RegisterTrial`` and the authoring gate of ``tolokaforge validate`` — through one
function, :func:`comparison_view_findings`, so a view is refused by the same rule
everywhere:

- **Every table a rule names is seeded** by the initial state. Under
  ``relaxed_validation`` a missing table is a warning, as for ``id_fields``. A rule's
  fields are checked only against a declared schema, when the task has one: agents
  write fields no seeded record carries, so seeded records are no schema.
- **The id-field checks** ``apply_comparison_view`` makes when it runs — the id field
  in a key, a reference to the rule's own id, a composite key — read here off the
  declaration (``ComparisonViewRuleConfig.id_field_errors``), through the same
  functions.
- **What ``normalize_ids`` builds a key from is not masked.** A ``key``,
  ``ordinal_by`` or ``rank_by`` field that the unstable filter drops (after the table
  names resolve as the db-service resolves them), that the clock mask drops while
  ``auto_mask_clock_columns`` is on, or that ``numeric_string_fields`` folds would key
  a record by a value the hash is told to ignore or to fold.
- **A ``references`` field is not masked** either: after its rewrite it links a key
  to its record, and a dropped one links nothing.
- **A re-keyed id field reaches the hash.** The unstable filter after the view leaves
  it in (:mod:`tolokaforge.core.grading.pre_hash`); the clock mask would not, so a
  re-keyed id field the clock mask drops is refused.
- **A view with ``hash`` disabled** is read by nothing: a warning, not a refusal.

Each finding names the rule (``state_checks.comparison_view.rules[<i>] (<kind>)``)
and the table and field it is about; the caller's context names the task.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.comparison_view import (
    ComparisonViewConfig,
    ComparisonViewError,
    ComparisonViewRuleConfig,
    ExcludeRecordsConfig,
    NormalizeIdsConfig,
    RekeyedField,
)
from tolokaforge.core.hash import AUTO_MASKED_CLOCK_COLUMNS, resolve_unstable_field_paths

if TYPE_CHECKING:
    from tolokaforge.core.models.task_config import StateChecksConfig
    from tolokaforge.runner.models import RunnerInitialStateConfig, RunnerStateChecksConfig

logger = logging.getLogger(__name__)

__all__ = [
    "ComparisonViewFindings",
    "check_authored_comparison_view",
    "check_comparison_view",
    "check_wire_comparison_view",
    "comparison_view_findings",
]

_BLOCK = "state_checks.comparison_view"


@dataclass(frozen=True)
class ComparisonViewFindings:
    """What checking one view against its task found: refusals, and warnings to report."""

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class _Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def comparison_view_findings(
    view: ComparisonViewConfig,
    *,
    tables: Mapping[str, Any],
    id_fields: Mapping[str, str | list[str]],
    unstable_fields: Iterable[str] = (),
    numeric_string_fields: Iterable[str] = (),
    auto_mask_clock_columns: bool = False,
    schemas: Mapping[str, Collection[str]] | None = None,
    relaxed_validation: bool = False,
    hash_enabled: bool = True,
) -> ComparisonViewFindings:
    """Check ``view`` against the task it is declared on.

    Args:
        view: The validated ``state_checks.comparison_view`` block.
        tables: The tables the task's initial state seeds.
        id_fields: ``state_checks.id_fields``.
        unstable_fields: The task's unstable fields as dotted ``table.field`` paths,
            table names as declared.
        numeric_string_fields: ``state_checks.numeric_string_fields``.
        auto_mask_clock_columns: ``state_checks.auto_mask_clock_columns``.
        schemas: Declared field names per table, for the tasks that declare a schema;
            a table without one has its fields unchecked.
        relaxed_validation: ``state_checks.relaxed_validation``: a missing table is
            then a warning.
        hash_enabled: ``state_checks.hash.enabled``.
    """
    found = _Findings()
    masked = _masked_columns(view, tables, id_fields, unstable_fields)
    folded = frozenset(numeric_string_fields)
    for index, rule in enumerate(view.rules):
        where = f"{_BLOCK}.rules[{index}] ({rule.kind})"
        _check_tables(found, where, rule, tables, relaxed_validation)
        _check_schema_fields(found, where, rule, schemas or {})
        found.errors.extend(f"{where}: {error}" for error in rule.id_field_errors(id_fields))
        if isinstance(rule, NormalizeIdsConfig):
            _check_key_fields(found, where, rule, masked, folded, auto_mask_clock_columns)
            _check_references(found, where, rule, masked, auto_mask_clock_columns)
    for rekeyed in _rekeyed_fields(view, id_fields):
        if auto_mask_clock_columns and rekeyed.field in AUTO_MASKED_CLOCK_COLUMNS:
            found.errors.append(
                f"{_BLOCK}: normalize_ids re-keys {rekeyed.dotted}, a column "
                f"auto_mask_clock_columns drops; a re-keyed id must reach the hash, so name "
                f"the table's id field differently in state_checks.id_fields or turn the "
                f"clock mask off"
            )
    if not hash_enabled:
        found.warnings.append(
            f"{_BLOCK} is declared but state_checks.hash is not enabled, so no hash reads the view"
        )
    return ComparisonViewFindings(errors=tuple(found.errors), warnings=tuple(found.warnings))


def check_comparison_view(view: ComparisonViewConfig, *, context: str, **task: Any) -> str | None:
    """:func:`comparison_view_findings` as one gate message, like the ``id_fields`` gate.

    Logs every warning behind ``[context]`` and returns ``None`` when nothing is
    refused, or every refusal joined behind ``[context]`` (the task id, or
    ``"RegisterTrial: <trial>"``) for the caller to raise or return.
    """
    findings = comparison_view_findings(view, **task)
    for warning in findings.warnings:
        logger.warning("[%s] %s", context, warning)
    if not findings.errors:
        return None
    return f"[{context}] " + " ".join(findings.errors)


def check_wire_comparison_view(
    state_checks: RunnerStateChecksConfig,
    initial_state: RunnerInitialStateConfig,
    *,
    context: str,
) -> str | None:
    """:func:`check_comparison_view` over a ``TaskDescription``'s own blocks.

    Every fact the check reads is on the wire — the seeded tables, their declared
    schemas, the unstable fields and the state-check flags — so the native adapter and
    ``RegisterTrial`` check one description by one call. ``None`` when the block
    declares no view.
    """
    if state_checks.comparison_view is None:
        return None
    return check_comparison_view(
        state_checks.comparison_view,
        context=context,
        tables=initial_state.tables,
        schemas={schema.table_name: tuple(schema.fields) for schema in initial_state.schemas},
        unstable_fields=[
            f"{spec.table_name}.{spec.field_name}" for spec in initial_state.unstable_fields
        ],
        id_fields=state_checks.id_fields,
        numeric_string_fields=state_checks.numeric_string_fields,
        auto_mask_clock_columns=state_checks.auto_mask_clock_columns,
        relaxed_validation=state_checks.relaxed_validation,
        hash_enabled=state_checks.hash_enabled,
    )


def check_authored_comparison_view(
    state_checks: StateChecksConfig,
    *,
    tables: Mapping[str, Any],
    unstable_fields: Iterable[str],
    context: str,
) -> str | None:
    """:func:`check_comparison_view` over the authored block core grades by.

    ``tables`` and ``unstable_fields`` are the task's seeded tables and its unstable
    fields as dotted paths; an authored task declares no schema. ``None`` when the
    block declares no view.
    """
    if state_checks.comparison_view is None:
        return None
    return check_comparison_view(
        state_checks.comparison_view,
        context=context,
        tables=tables,
        unstable_fields=unstable_fields,
        id_fields=state_checks.id_fields,
        numeric_string_fields=state_checks.numeric_string_fields,
        auto_mask_clock_columns=state_checks.auto_mask_clock_columns,
        relaxed_validation=state_checks.relaxed_validation,
        hash_enabled=state_checks.hash is not None and state_checks.hash.enabled,
    )


def _rekeyed_fields(
    view: ComparisonViewConfig, id_fields: Mapping[str, str | list[str]]
) -> tuple[RekeyedField, ...]:
    """The re-keyed id fields of the rules whose id field resolves; the rest are refused
    by their own ``id_field_errors``."""
    resolved: list[RekeyedField] = []
    for rule in view.rules:
        try:
            resolved.extend(rule.rekeyed_fields(id_fields))
        except ComparisonViewError:
            continue
    return tuple(resolved)


def _masked_columns(
    view: ComparisonViewConfig,
    tables: Mapping[str, Any],
    id_fields: Mapping[str, str | list[str]],
    unstable_fields: Iterable[str],
) -> frozenset[str]:
    """The ``table.field`` columns step 2 drops: the resolved unstable fields minus every
    re-keyed id field, which the filter after the view leaves in."""
    kept_in = {rekeyed.dotted for rekeyed in _rekeyed_fields(view, id_fields)}
    return frozenset(resolve_unstable_field_paths(unstable_fields, tables)) - kept_in


def _check_tables(
    found: _Findings,
    where: str,
    rule: ComparisonViewRuleConfig,
    tables: Mapping[str, Any],
    relaxed: bool,
) -> None:
    for table in rule.names():
        if table in tables:
            continue
        message = (
            f"{where} names table {table!r}, which the initial state does not seed; it seeds "
            f"{sorted(tables)}"
        )
        if relaxed:
            found.warnings.append(f"{message} (state_checks.relaxed_validation downgrades it)")
        else:
            found.errors.append(
                f"{message}. Fix the table name, seed the table, or set "
                f"state_checks.relaxed_validation: true"
            )


def _rule_fields(rule: ComparisonViewRuleConfig) -> dict[str, set[str]]:
    """The top-level fields of each table a rule reads, for a schema to be checked against.

    A nested path is checked at its first segment; the fields ``where`` reads under a
    ``path`` belong to nested items no table schema describes.
    """
    fields: dict[str, set[str]] = {}
    if isinstance(rule, ExcludeRecordsConfig):
        if rule.path is None:
            fields.setdefault(rule.table, set()).update(_where_fields(rule))
        else:
            fields.setdefault(rule.table, set()).add(rule.path.split(".", 1)[0])
        for reference in rule.unless_referenced_by:
            fields.setdefault(reference.table, set()).add(reference.field.split(".", 1)[0])
    elif isinstance(rule, NormalizeIdsConfig):
        fields.setdefault(rule.table, set()).update(rule.key_fields())
        for reference in rule.references:
            fields.setdefault(reference.table, set()).add(reference.field.split(".", 1)[0])
    return fields


def _where_fields(rule: ExcludeRecordsConfig) -> set[str]:
    named: set[str] = set()
    for name, condition in rule.where.items():
        if isinstance(condition, tuple):
            named.update(condition)
        else:
            named.add(name)
    return named


def _check_schema_fields(
    found: _Findings,
    where: str,
    rule: ComparisonViewRuleConfig,
    schemas: Mapping[str, Collection[str]],
) -> None:
    for table, fields in sorted(_rule_fields(rule).items()):
        if table not in schemas:
            continue
        undeclared = sorted(fields - set(schemas[table]))
        if undeclared:
            found.errors.append(
                f"{where} reads field(s) {undeclared} of table {table!r}, which its declared "
                f"schema does not carry; it declares {sorted(schemas[table])}"
            )


def _check_key_fields(
    found: _Findings,
    where: str,
    rule: NormalizeIdsConfig,
    masked: frozenset[str],
    folded: frozenset[str],
    clock_mask: bool,
) -> None:
    """A key is read as it is, before any fold, so it must read a value the hash keeps."""
    for name in sorted(rule.key_fields()):
        column = f"{rule.table}.{name}"
        if column in masked:
            found.errors.append(
                f"{where} builds its key from {column}, which unstable_fields masks; the key "
                f"would carry a value the hash is told to ignore"
            )
        if clock_mask and name in AUTO_MASKED_CLOCK_COLUMNS:
            found.errors.append(
                f"{where} builds its key from {column}, a column auto_mask_clock_columns "
                f"drops; the key would carry a value the hash is told to ignore"
            )
        if name in folded:
            found.errors.append(
                f"{where} builds its key from {column}, which numeric_string_fields folds; "
                f"the key reads the value before the fold, so two values the hash equates "
                f"would key two records apart"
            )


def _check_references(
    found: _Findings,
    where: str,
    rule: NormalizeIdsConfig,
    masked: frozenset[str],
    clock_mask: bool,
) -> None:
    """A reference the masks drop links nothing to its record after its rewrite."""
    for reference in rule.references:
        head = reference.field.split(".", 1)[0]
        column = f"{reference.table}.{head}"
        label = f"{reference.table}.{reference.field}"
        if column in masked:
            found.errors.append(
                f"{where} rewrites the reference {label}, and unstable_fields masks "
                f"{column}; a dropped reference links no key to its record"
            )
        if clock_mask and head in AUTO_MASKED_CLOCK_COLUMNS:
            found.errors.append(
                f"{where} rewrites the reference {label}, and auto_mask_clock_columns drops "
                f"{column}; a dropped reference links no key to its record"
            )
