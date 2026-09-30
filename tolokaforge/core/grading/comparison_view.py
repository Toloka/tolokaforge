"""The comparison view: a one-sided transform of a state before the state hash.

:func:`apply_comparison_view` computes the view of one state from that state, its
initial state and the ``state_checks.comparison_view`` block; the other side of
the comparison is never an input. ADR-0053 has the design, the order of the
pre-hash steps and the versioning policy of the record. The semantics:

``exclude_records``
    Drops the rows of ``table`` that match ``where`` or, with ``path``, the items
    of the nested list there. ``where`` is a non-empty conjunction, and a missing
    field reads as null. ``field: value`` is exact equality with a scalar, before
    any ``numeric_string_fields`` folding (``"130.00"`` is not ``"130"``, a bool is
    never a number); ``{in: [...]}`` is equality with one of several;
    ``{is_null: bool}``; ``{starts_with: prefix}`` matches strings only; and
    ``all_zero: [fields]`` needs every field to hold a number, or a plain decimal
    string, equal to zero (null and missing are not zero).
    ``unless_referenced_by: [{table, field}]`` keeps a matching row whose id
    another row holds in a listed field. The id field is the table's
    ``state_checks.id_fields`` entry (``"id"`` when absent; one field, never
    null). Ids match as JSON values, so ``1`` is ``1.0`` but not ``"1"``, and
    references are read before the rule removes anything.
``exclude_tables``
    Drops the named tables, key included; refused for a table another rule names.

A path (``path``, ``unless_referenced_by.field``) is field names joined by ``.``.
A list met on the way is walked item by item, a missing or null field ends the
path, and a value that does not fit raises :class:`ComparisonViewError`, as does
every rule that cannot apply. The input is never mutated, and the same inputs
give the same view and the same :class:`ComparisonViewRecord`.

The module depends on the standard library and pydantic only: the runner will
apply the view too, and the runner-subset wheel excludes ``state_checks`` and
``combine``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from abc import abstractmethod
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Protocol, cast, runtime_checkable

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    FieldSerializationInfo,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

__all__ = [
    "COMPARISON_VIEW_FUNCTION_VERSION",
    "COMPARISON_VIEW_VERSIONS",
    "ComparisonViewConfig",
    "ComparisonViewError",
    "ComparisonViewRecord",
    "ComparisonViewResult",
    "ComparisonViewRule",
    "ComparisonViewRuleConfig",
    "RuleApplication",
    "RuleOutcome",
    "apply_comparison_view",
    "comparison_view_rules",
    "resolve_comparison_view_rule",
]

COMPARISON_VIEW_FUNCTION_VERSION: Final[int] = 1
"""Version of what the rules compute. Bumped on any change to the view a rule
produces from the same inputs, so a recorded view can be told apart from one a
later engine would compute."""

COMPARISON_VIEW_VERSIONS: Final[tuple[int, ...]] = (1,)
"""Schema versions of the ``comparison_view`` block this engine reads."""

ALL_ZERO: Final[str] = "all_zero"
"""The ``where`` key whose value lists fields that must all be numerically zero.
It is reserved, so a field named ``all_zero`` cannot take a condition."""

_DEFAULT_ID_FIELD: Final[str] = "id"
_ID_TYPES: Final[tuple[type, ...]] = (str, int, float, bool)
_PLAIN_DECIMAL: Final[re.Pattern[str]] = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", re.ASCII)


class ComparisonViewError(ValueError):
    """A rule cannot compute the view of this state.

    It is an evaluation error, never a pass or a fail: the grade reports it as a
    grading error.
    """


# ---------------------------------------------------------------------------
# Declared names
# ---------------------------------------------------------------------------


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _field_name(value: str) -> str:
    _non_blank(value)
    if "." in value:
        raise ValueError(
            f"{value!r} names a field of the record and cannot contain '.'; "
            f"a nested list is reached with 'path'"
        )
    return value


def _dotted_path(value: str) -> str:
    if any(not segment.strip() for segment in value.split(".")):
        raise ValueError(
            f"path {value!r} has an empty segment; join field names with '.', "
            f"e.g. 'expense_payments.purchase_allocations'"
        )
    return value


NonBlankStr = Annotated[StrictStr, AfterValidator(_non_blank)]
FieldName = Annotated[StrictStr, AfterValidator(_field_name)]
DottedPath = Annotated[StrictStr, AfterValidator(_dotted_path)]
Scalar = StrictStr | StrictInt | StrictFloat | StrictBool | None


def _scalar(value: Any, what: str) -> Any:
    """``value`` if it is a JSON scalar; the unions below then see only values that fit."""
    if value is not None and not isinstance(value, str | int | float | bool):
        raise ValueError(
            f"{what} must be a string, a number, a bool or null, not a {type(value).__name__}"
        )
    return value


def _segments(path: str) -> tuple[str, ...]:
    return tuple(path.split("."))


# ---------------------------------------------------------------------------
# where conditions
# ---------------------------------------------------------------------------


class InCondition(BaseModel):
    """``{in: [v1, v2, ...]}``: the field equals one of the listed scalars."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    any_of: tuple[Scalar, ...] = Field(alias="in", min_length=1)

    @field_validator("any_of", mode="before")
    @classmethod
    def _scalars(cls, value: Any) -> Any:
        if not isinstance(value, list | tuple):
            raise ValueError("must be a list of values")
        return tuple(_scalar(item, "each value") for item in value)


class IsNullCondition(BaseModel):
    """``{is_null: true}`` matches a null or missing field, ``false`` a present non-null one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    is_null: StrictBool


class StartsWithCondition(BaseModel):
    """``{starts_with: prefix}``: the field is a string that begins with ``prefix``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    starts_with: Annotated[StrictStr, StringConstraints(min_length=1)]


_OPERATORS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {"in": InCondition, "is_null": IsNullCondition, "starts_with": StartsWithCondition}
)

FieldCondition = Scalar | InCondition | IsNullCondition | StartsWithCondition
WhereCondition = FieldCondition | tuple[FieldName, ...]


def _parse_where(raw: Any) -> Any:
    """Turn a declared ``where`` mapping into conditions, with a message per mistake."""
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(
            "must be a non-empty mapping of conditions: a rule must not drop every row "
            "of a table because some of its rows are optional"
        )
    return {
        field: (
            _parse_all_zero(condition) if field == ALL_ZERO else _parse_condition(field, condition)
        )
        for field, condition in raw.items()
    }


def _parse_all_zero(fields: Any) -> tuple[str, ...]:
    if not isinstance(fields, list | tuple) or not fields:
        raise ValueError(f"{ALL_ZERO!r} takes a non-empty list of field names")
    for field in fields:
        if not isinstance(field, str):
            raise ValueError(f"{ALL_ZERO!r} lists field names, not a {type(field).__name__}")
        _field_name(field)
    if len(set(fields)) != len(fields):
        raise ValueError(f"{ALL_ZERO!r} lists a field more than once: {list(fields)}")
    return tuple(fields)


def _parse_condition(field: Any, condition: Any) -> Any:
    if isinstance(condition, list | tuple):
        raise ValueError(
            f"{field!r}: equality takes one scalar; for any of several values "
            f"write {{in: {list(condition)}}}"
        )
    if not isinstance(condition, Mapping):
        return _scalar(condition, f"{field!r}: the value")
    if len(condition) != 1 or next(iter(condition)) not in _OPERATORS:
        raise ValueError(
            f"{field!r}: {sorted(map(str, condition))} is not one operator; an operator "
            f"mapping holds exactly one of {sorted(_OPERATORS)}"
        )
    operator = next(iter(condition))
    try:
        return _OPERATORS[operator].model_validate(condition)
    except ValidationError as exc:
        raise ValueError(f"{field!r}: {_error_summary(exc)}") from None


def _error_summary(exc: ValidationError) -> str:
    """The errors of ``exc`` on one line, a raised message without pydantic's prefix."""
    parts = []
    for error in exc.errors():
        location = ".".join(map(str, error["loc"])) or "<entry>"
        raised = (error.get("ctx") or {}).get("error")
        parts.append(f"{location}: {raised if raised is not None else error['msg']}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Rule configs
# ---------------------------------------------------------------------------


class ComparisonViewRuleConfig(BaseModel):
    """One entry of ``comparison_view.rules``, validated by the rule its ``kind`` names.

    Every rule's config model derives from this class. ``kind`` is the entry's
    key in the rule table; everything else belongs to the rule.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str

    @abstractmethod
    def names(self) -> tuple[str, ...]:
        """The tables this entry names, for the ``exclude_tables`` guard."""


class RecordReference(BaseModel):
    """A field of another table's rows that may hold the ids of the rows a rule drops."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: NonBlankStr
    field: DottedPath


class ExcludeRecordsConfig(ComparisonViewRuleConfig):
    """``exclude_records``: see the module docstring for the semantics."""

    kind: Literal["exclude_records"] = "exclude_records"
    table: NonBlankStr
    path: DottedPath | None = None
    where: Mapping[FieldName, WhereCondition]
    unless_referenced_by: tuple[RecordReference, ...] = ()
    reason: NonBlankStr | None = None

    @field_validator("where", mode="before")
    @classmethod
    def _conditions(cls, value: Any) -> Any:
        return _parse_where(value)

    @field_validator("where")
    @classmethod
    def _read_only(cls, value: Mapping[str, WhereCondition]) -> Mapping[str, WhereCondition]:
        """A validated view is immutable all the way down, so its sha cannot change."""
        return MappingProxyType(dict(value))

    @field_serializer("where", mode="wrap")
    def _as_a_plain_mapping(
        self, where: Mapping[str, WhereCondition], handler: SerializerFunctionWrapHandler
    ) -> Any:
        return handler(dict(where))

    @model_validator(mode="after")
    def _references_keep_rows_only(self) -> ExcludeRecordsConfig:
        if self.path is not None and self.unless_referenced_by:
            raise ValueError(
                "unless_referenced_by keeps rows of the table that another table references "
                "by id; with path the rule drops nested items, which have no declared id, "
                "so the two do not combine"
            )
        return self

    def names(self) -> tuple[str, ...]:
        references = (reference.table for reference in self.unless_referenced_by)
        return tuple(dict.fromkeys((self.table, *references)))


class ExcludeTablesConfig(ComparisonViewRuleConfig):
    """``exclude_tables``: drop the named tables whole; ``reason`` says why they do not count."""

    kind: Literal["exclude_tables"] = "exclude_tables"
    tables: tuple[NonBlankStr, ...] = Field(min_length=1)
    reason: NonBlankStr

    @field_validator("tables")
    @classmethod
    def _each_table_once(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError(f"lists a table more than once: {list(value)}")
        return value

    def names(self) -> tuple[str, ...]:
        return self.tables


# ---------------------------------------------------------------------------
# What a view records
# ---------------------------------------------------------------------------


class RuleApplication(BaseModel):
    """What one rule did to one table, in rule order.

    ``rows_removed`` counts the rows of ``table`` a rule removed or, with
    ``path``, the items of the nested lists at that path. ``exclude_tables``
    contributes one application per listed table; a table held as a single value
    rather than a list of rows counts as one row, and an absent one as none.
    ``ids_rewritten`` counts rewritten record keys; no v1 rule rewrites one yet.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    table: str
    path: str | None = None
    rows_removed: int = Field(ge=0)
    ids_rewritten: int = Field(default=0, ge=0)


class ComparisonViewRecord(BaseModel):
    """Which transform produced a view, recorded with the grade it is compared for.

    ``version`` is the block's schema version, ``function_version`` the engine's
    :data:`COMPARISON_VIEW_FUNCTION_VERSION`, and ``config_sha256`` the digest of
    what the rules do (:meth:`ComparisonViewConfig.config_sha256`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    function_version: int
    config_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    applied: tuple[RuleApplication, ...]


@dataclass(frozen=True)
class RuleOutcome:
    """What a rule returns: the state after it, and what it did to each table."""

    state: dict[str, Any]
    applied: tuple[RuleApplication, ...]


@dataclass(frozen=True)
class ComparisonViewResult:
    """The view of one state, and the record of how it was computed."""

    state: dict[str, list[dict[str, Any]]]
    record: ComparisonViewRecord


# ---------------------------------------------------------------------------
# The rule table
# ---------------------------------------------------------------------------


@runtime_checkable
class ComparisonViewRule(Protocol):
    """A rule the ``kind`` of a ``comparison_view`` entry resolves to.

    ``config_model`` validates the entry. ``apply`` is a pure function of one
    state, its initial state and the rule's config: it must not mutate ``state``
    or ``initial``, returns the next state in its outcome (unchanged tables may
    be shared with ``state``), and raises :class:`ComparisonViewError` when the
    state does not fit the declaration.
    """

    kind: str
    config_model: type[ComparisonViewRuleConfig]

    def apply(
        self,
        state: dict[str, Any],
        *,
        initial: Mapping[str, Any] | None,
        id_fields: Mapping[str, str | list[str]],
        config: ComparisonViewRuleConfig,
    ) -> RuleOutcome: ...


class ExcludeRecords:
    """The ``exclude_records`` rule."""

    kind = "exclude_records"
    config_model: type[ComparisonViewRuleConfig] = ExcludeRecordsConfig

    def apply(
        self,
        state: dict[str, Any],
        *,
        initial: Mapping[str, Any] | None,
        id_fields: Mapping[str, str | list[str]],
        config: ComparisonViewRuleConfig,
    ) -> RuleOutcome:
        config = cast(ExcludeRecordsConfig, config)
        if config.table not in state:
            return RuleOutcome(state=state, applied=(self._application(config, removed=0),))
        rows = _records(state[config.table], f"exclude_records: table {config.table!r}")
        matches = _record_predicate(config.where)
        if config.path is None:
            kept = _rows_to_keep(config, rows, matches, state, id_fields)
            removed = len(rows) - len(kept)
        else:
            where = f"exclude_records: '{config.table}.{config.path}'"
            kept, removed = _without_matching_items(rows, _segments(config.path), matches, where)
        return RuleOutcome(
            state={**state, config.table: kept},
            applied=(self._application(config, removed=removed),),
        )

    def _application(self, config: ExcludeRecordsConfig, *, removed: int) -> RuleApplication:
        return RuleApplication(
            kind=self.kind, table=config.table, path=config.path, rows_removed=removed
        )


class ExcludeTables:
    """The ``exclude_tables`` rule."""

    kind = "exclude_tables"
    config_model: type[ComparisonViewRuleConfig] = ExcludeTablesConfig

    def apply(
        self,
        state: dict[str, Any],
        *,
        initial: Mapping[str, Any] | None,
        id_fields: Mapping[str, str | list[str]],
        config: ComparisonViewRuleConfig,
    ) -> RuleOutcome:
        config = cast(ExcludeTablesConfig, config)
        dropped = set(config.tables)
        applied = tuple(
            RuleApplication(kind=self.kind, table=table, rows_removed=_row_count(state, table))
            for table in config.tables
        )
        kept = {table: rows for table, rows in state.items() if table not in dropped}
        return RuleOutcome(state=kept, applied=applied)


def _row_count(state: Mapping[str, Any], table: str) -> int:
    if table not in state:
        return 0
    rows = state[table]
    return len(rows) if isinstance(rows, list) else 1


_BUILTIN_RULES: Final[Mapping[str, ComparisonViewRule]] = MappingProxyType(
    {rule.kind: rule for rule in (ExcludeRecords(), ExcludeTables())}
)


def comparison_view_rules() -> Mapping[str, ComparisonViewRule]:
    """The rule table a ``comparison_view`` entry's ``kind`` resolves through.

    Only the built-in rules. Turning third-party rules on later changes this
    function alone: it merges the ``tolokaforge.comparison_view_rules``
    entry-point group into the built-ins and refuses duplicates (ADR-0053
    § Extensibility).
    """
    return _BUILTIN_RULES


def resolve_comparison_view_rule(kind: str) -> ComparisonViewRule:
    """The rule ``kind`` names, or :class:`ComparisonViewError` naming the known kinds."""
    rules = comparison_view_rules()
    if not isinstance(kind, str) or kind not in rules:
        raise ComparisonViewError(
            f"unknown comparison_view rule kind {kind!r}; known kinds: {sorted(rules)}"
        )
    return rules[kind]


# ---------------------------------------------------------------------------
# exclude_records helpers
# ---------------------------------------------------------------------------

Record = Mapping[str, Any]


def _records(value: Any, where: str) -> list[Record]:
    """``value`` as a list of mapping records, or :class:`ComparisonViewError`."""
    if not isinstance(value, list):
        raise ComparisonViewError(f"{where} holds a {type(value).__name__}, not a list of records")
    for item in value:
        if not isinstance(item, Mapping):
            raise ComparisonViewError(
                f"{where} holds a {type(item).__name__} item; a rule reads fields of "
                f"mapping records only"
            )
    return value


def _record_predicate(where: Mapping[str, WhereCondition]) -> Callable[[Record], bool]:
    tests = [_field_test(field, condition) for field, condition in where.items()]
    return lambda record: all(test(record) for test in tests)


def _field_test(field: str, condition: WhereCondition) -> Callable[[Record], bool]:
    if field == ALL_ZERO and isinstance(condition, tuple):
        names = condition
        return lambda record: all(_is_numeric_zero(record.get(name)) for name in names)
    if isinstance(condition, InCondition):
        values = condition.any_of
        return lambda record: any(_same_value(record.get(field), value) for value in values)
    if isinstance(condition, IsNullCondition):
        wants_null = condition.is_null
        return lambda record: (record.get(field) is None) is wants_null
    if isinstance(condition, StartsWithCondition):
        prefix = condition.starts_with
        return lambda record: _starts_with(record.get(field), prefix)
    return lambda record: _same_value(record.get(field), condition)


def _same_value(value: Any, expected: Any) -> bool:
    """Equality as JSON reads it: a bool never equals a number, ``1`` equals ``1.0``."""
    return isinstance(value, bool) is isinstance(expected, bool) and value == expected


def _starts_with(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix)


def _is_numeric_zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value == 0
    if isinstance(value, str):
        text = value.strip()
        return _PLAIN_DECIMAL.fullmatch(text) is not None and Decimal(text) == 0
    return False


def _rows_to_keep(
    config: ExcludeRecordsConfig,
    rows: list[Record],
    matches: Callable[[Record], bool],
    state: Mapping[str, Any],
    id_fields: Mapping[str, str | list[str]],
) -> list[Record]:
    if not config.unless_referenced_by:
        return [row for row in rows if not matches(row)]
    id_field = _record_id_field(config.table, id_fields)
    _refuse_own_id_references(config, id_field)
    counts = _reference_counts(state, config.unless_referenced_by)
    return [
        row
        for row in rows
        if not matches(row) or _referenced_by_another_row(row, config, id_field, counts)
    ]


def _refuse_own_id_references(config: ExcludeRecordsConfig, id_field: str) -> None:
    for reference in config.unless_referenced_by:
        if reference.table == config.table and reference.field == id_field:
            raise ComparisonViewError(
                f"unless_referenced_by names {config.table}.{id_field}, the id field of the "
                f"rule's own table: every row would reference only itself"
            )


def _referenced_by_another_row(
    row: Record, config: ExcludeRecordsConfig, id_field: str, counts: Counter[tuple[bool, Any]]
) -> bool:
    """Whether a reference other than the row's own references to itself holds its id."""
    key = _reference_key(_record_id(row, config.table, id_field))
    own = sum(
        1
        for reference in config.unless_referenced_by
        if reference.table == config.table
        for value in _ids_at(row, _segments(reference.field), _reference_label(reference))
        if _reference_key(value) == key
    )
    return counts[key] > own


def _record_id_field(table: str, id_fields: Mapping[str, str | list[str]]) -> str:
    """The one field holding ``table``'s record ids: its ``id_fields`` entry, else ``"id"``.

    Resolves as ``tolokaforge.runner.id_resolution.table_key`` does (a blank or
    empty entry falls through to ``"id"``, a one-element list means its element),
    which this module does not import. A composite key has no single field a
    reference could hold, so it is refused.
    """
    declared = id_fields.get(table)
    if not declared:
        return _DEFAULT_ID_FIELD
    fields = [declared] if isinstance(declared, str) else declared
    if not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields):
        raise ComparisonViewError(
            f"state_checks.id_fields[{table!r}] is {declared!r}, which names no key field; "
            f"a key is a field name or a list of field names"
        )
    if len(fields) > 1:
        raise ComparisonViewError(
            f"state_checks.id_fields[{table!r}] is the composite key {declared!r}; "
            f"unless_referenced_by needs one id field for table {table!r}, and a composite "
            f"key has no single field a reference could hold"
        )
    return fields[0]


def _record_id(row: Record, table: str, id_field: str) -> Any:
    value = row.get(id_field)
    if value is None:
        raise ComparisonViewError(
            f"a record of table {table!r} matches exclude_records but its id field "
            f"{id_field!r} is missing or null, so unless_referenced_by cannot tell whether "
            f"it is referenced; declare state_checks.id_fields[{table!r}]"
        )
    if not isinstance(value, _ID_TYPES):
        raise ComparisonViewError(
            f"a record of table {table!r} holds a {type(value).__name__} in its id field "
            f"{id_field!r}; an id is a string, a number or a bool"
        )
    return value


def _reference_key(value: Any) -> tuple[bool, Any]:
    """The key an id and a reference to it share, compared as JSON values.

    A bool never matches a number, ``1`` matches ``1.0``, and ``1`` does not match
    ``"1"``: the JSON types differ.
    """
    return isinstance(value, bool), value


def _reference_counts(
    state: Mapping[str, Any], references: Sequence[RecordReference]
) -> Counter[tuple[bool, Any]]:
    """How many times each id is referenced by the listed fields, over every row."""
    counts: Counter[tuple[bool, Any]] = Counter()
    for reference in references:
        if reference.table not in state:
            continue
        rows = _records(state[reference.table], f"unless_referenced_by: table {reference.table!r}")
        found = _ids_at(rows, _segments(reference.field), _reference_label(reference))
        counts.update(map(_reference_key, found))
    return counts


def _reference_label(reference: RecordReference) -> str:
    return f"unless_referenced_by: '{reference.table}.{reference.field}'"


def _ids_at(value: Any, segments: Sequence[str], where: str) -> list[Any]:
    """The ids the reference field at ``segments`` holds below ``value``."""
    found: list[Any] = []

    def collect(leaf: Any) -> Any:
        found.extend(_ids_in(leaf, where))
        return leaf

    _rewrite_at(value, segments, collect, where)
    return found


def _ids_in(value: Any, where: str) -> Iterator[Any]:
    """The ids a reference field holds: a scalar, or the scalars of a (nested) list."""
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            yield from _ids_in(item, where)
        return
    if not isinstance(value, _ID_TYPES):
        raise ComparisonViewError(f"{where} holds a {type(value).__name__}, not an id")
    yield value


def _without_matching_items(
    rows: list[Record], segments: Sequence[str], matches: Callable[[Record], bool], where: str
) -> tuple[list[Record], int]:
    """``rows`` without the matching items of the lists at ``segments``, and how many went."""
    removed = 0

    def drop_matching(items: Any) -> list[Record]:
        nonlocal removed
        records = _records(items, where)
        kept = [item for item in records if not matches(item)]
        removed += len(records) - len(kept)
        return kept

    return _rewrite_at(rows, segments, drop_matching, where), removed


def _rewrite_at(value: Any, segments: Sequence[str], leaf: Callable[[Any], Any], where: str) -> Any:
    """``value`` with ``leaf`` applied to each value ``segments`` reach below it.

    The one path walker of the rules (see the module docstring): each segment reads a
    field of a mapping, and a list met before the path ends is walked item by
    item. A missing or null field ends the walk and leaves the value as it is; a
    value a field has to be read from that is neither a mapping nor a list raises
    :class:`ComparisonViewError`. The mappings on the way are rebuilt, never
    mutated.
    """
    if value is None:
        return value
    if not segments:
        return leaf(value)
    if isinstance(value, list):
        return [_rewrite_at(item, segments, leaf, where) for item in value]
    if not isinstance(value, Mapping):
        raise ComparisonViewError(
            f"{where}: field {segments[0]!r} is read from a {type(value).__name__}, "
            f"not a mapping or a list"
        )
    head = segments[0]
    if head not in value:
        return value
    return {**value, head: _rewrite_at(value[head], segments[1:], leaf, where)}


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


class ComparisonViewConfig(BaseModel):
    """The ``state_checks.comparison_view`` block: a schema version and rules in list order.

    ``kind`` resolves through :func:`comparison_view_rules`, not a static union, so
    a rule the table gains later validates the same way the built-ins do. Dump it
    with ``by_alias=True``: the ``in`` operator serialises under its alias only
    then, and only that dump validates back.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    rules: tuple[ComparisonViewRuleConfig, ...] = Field(min_length=1)

    @field_validator("version", mode="before")
    @classmethod
    def _known_version(cls, value: Any) -> Any:
        if type(value) is not int or value not in COMPARISON_VIEW_VERSIONS:
            raise ValueError(
                f"comparison_view.version {value!r} is not a version this engine reads; "
                f"known versions: {list(COMPARISON_VIEW_VERSIONS)}"
            )
        return value

    @field_validator("rules", mode="before")
    @classmethod
    def _resolve_rules(cls, value: Any) -> Any:
        if not isinstance(value, list | tuple):
            raise ValueError("comparison_view.rules must be a list of rule entries")
        if not value:
            raise ValueError(
                "comparison_view.rules is empty; a view without rules is no view, so "
                "drop the block instead"
            )
        return tuple(_resolve_entry(index, entry) for index, entry in enumerate(value))

    @field_serializer("rules")
    def _each_rule_as_its_own_model(
        self, rules: tuple[ComparisonViewRuleConfig, ...], info: FieldSerializationInfo
    ) -> list[dict[str, Any]]:
        """Dump every rule with its own config model, not the base the field declares."""
        return [
            rule.model_dump(
                mode=info.mode,
                by_alias=info.by_alias,
                exclude_unset=info.exclude_unset,
                exclude_defaults=info.exclude_defaults,
                exclude_none=info.exclude_none,
            )
            for rule in rules
        ]

    @model_validator(mode="after")
    def _excluded_tables_are_named_once(self) -> ComparisonViewConfig:
        for index, rule in enumerate(self.rules):
            if isinstance(rule, ExcludeTablesConfig):
                _refuse_shared_tables(index, rule, self.rules)
        return self

    def config_sha256(self) -> str:
        """sha256 of what the rules do: each rule's kind and its non-default settings.

        The policy (ADR-0053 § Versioning): a setting at its default is left out,
        so a new optional field whose default keeps a rule's behaviour keeps every
        existing sha; a change to what a rule does bumps
        :data:`COMPARISON_VIEW_FUNCTION_VERSION` instead; ``reason`` is prose, not
        behaviour, and is not hashed. The JSON is canonical as ``ModelsFingerprint``
        hashes model data (sorted keys, ASCII, no whitespace), so the key order of
        the declaration does not change it either.
        """
        payload = [_hashed_rule(rule) for rule in self.rules]
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hashed_rule(rule: ComparisonViewRuleConfig) -> dict[str, Any]:
    settings = rule.model_dump(
        mode="json", by_alias=True, exclude_defaults=True, exclude={"kind", "reason"}
    )
    return {"kind": rule.kind, **settings}


def _resolve_entry(index: int, entry: Any) -> ComparisonViewRuleConfig:
    """Validate one entry of ``rules`` through the rule its ``kind`` names."""
    if isinstance(entry, ComparisonViewRuleConfig):
        kind = entry.kind
    elif isinstance(entry, Mapping) and "kind" in entry:
        kind = entry["kind"]
    else:
        raise ValueError(
            f"rules[{index}] must be a mapping with a 'kind'; known kinds: "
            f"{sorted(comparison_view_rules())}"
        )
    try:
        rule = resolve_comparison_view_rule(kind)
    except ComparisonViewError as exc:
        raise ValueError(f"rules[{index}]: {exc}") from None
    if isinstance(entry, ComparisonViewRuleConfig):
        return entry
    try:
        return rule.config_model.model_validate(entry)
    except ValidationError as exc:
        raise ValueError(f"rules[{index}] ({rule.kind}): {_error_summary(exc)}") from None


def _refuse_shared_tables(
    index: int, rule: ExcludeTablesConfig, rules: Sequence[ComparisonViewRuleConfig]
) -> None:
    for other_index, other in enumerate(rules):
        if other_index == index:
            continue
        shared = sorted(set(rule.tables) & set(other.names()))
        if shared:
            raise ValueError(
                f"rules[{index}] (exclude_tables) drops table(s) {shared} that "
                f"rules[{other_index}] ({other.kind}) also names; a table is dropped whole "
                f"or shaped by other rules, not both"
            )


# ---------------------------------------------------------------------------
# The function
# ---------------------------------------------------------------------------


def apply_comparison_view(
    state: Mapping[str, list[dict[str, Any]]],
    *,
    initial: Mapping[str, list[dict[str, Any]]] | None,
    view: ComparisonViewConfig,
    id_fields: Mapping[str, str | list[str]],
) -> ComparisonViewResult:
    """The view of ``state`` under ``view``: its rules applied in order to a copy of it.

    ``initial`` is the initial state of the database ``state`` was read from, and
    ``id_fields`` is ``state_checks.id_fields`` (absent table → ``"id"``). Neither
    input is mutated: the rules get private copies of both, and the returned state
    shares nothing with ``state``. A rule
    that cannot apply raises :class:`ComparisonViewError`.
    """
    working: dict[str, Any] = copy.deepcopy(dict(state))
    initial_copy = None if initial is None else copy.deepcopy(dict(initial))
    applied: list[RuleApplication] = []
    for config in view.rules:
        rule = resolve_comparison_view_rule(config.kind)
        outcome = rule.apply(working, initial=initial_copy, id_fields=id_fields, config=config)
        working = outcome.state
        applied.extend(outcome.applied)
    record = ComparisonViewRecord(
        version=view.version,
        function_version=COMPARISON_VIEW_FUNCTION_VERSION,
        config_sha256=view.config_sha256(),
        applied=tuple(applied),
    )
    return ComparisonViewResult(state=working, record=record)
