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
``normalize_ids``
    Re-keys the records of ``table`` in ``scope`` (``new_records``, the default:
    those whose id the initial state's table lacks; or ``all``) and rewrites every
    exact reference to them in the listed ``references`` fields. The new key is
    ``<table>:<canonical JSON of its fields>``, built from the record's ``key``
    fields (``fee_credit_journal:{"account_id":"A1","delta":-5,"fee_id":"F2"}``) or
    from its ``ordinal_by`` group plus ``#<ordinal>`` ranked by ``rank_by``
    (``fee_credit_journal:{"account_id":"A1"}#2``); an integral float renders as
    the int it equals. The re-keying is bijective or raises: a key two records
    share, a key a kept record already holds, a rank tie and a reference that
    already holds a new key are refused. A reference to no re-keyed record stays
    as it is. A re-keyed id is a function of its record's content, not a
    generated value, so it must reach the hash: the unstable filter and the
    clock mask after the view must not drop the fields the record lists in
    ``rekeyed_fields``, even when ``unstable_fields`` names them. Key fields are
    read as they are, before ``numeric_string_fields`` or
    ``auto_normalize_nullables`` fold anything (``""`` and null give different
    keys), and a missing key field raises.

A path (``path``, ``unless_referenced_by.field``) is field names joined by ``.``.
A list met on the way is walked item by item, a missing or null field ends the
path, and a value that does not fit raises :class:`ComparisonViewError`, as does
every rule that cannot apply. The input is never mutated, and the same inputs
give the same view and the same :class:`ComparisonViewRecord`.

A rule is a class registered in the ``tolokaforge.comparison_view_rules``
entry-point group (:class:`ComparisonViewRule`), and ``kind`` resolves through the
group: the three above register there like any rule a distribution ships. A rule
decides which states hash equal, so registering one is a grading decision; the
trust boundary is stated on :class:`ComparisonViewRule`.

The module depends on the standard library and pydantic, and reaches the registry
(:mod:`tolokaforge.core.plugin_registry`) only where a kind resolves: the runner
will apply the view too, and the runner-subset wheel excludes ``state_checks`` and
``combine``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from abc import abstractmethod
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from types import MappingProxyType, ModuleType
from typing import Annotated, Any, ClassVar, Final, Literal, Protocol, cast, runtime_checkable

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
    "ComparisonViewCollision",
    "ComparisonViewError",
    "ComparisonViewRecord",
    "ComparisonViewResult",
    "ComparisonViewRule",
    "ComparisonViewRuleConfig",
    "RekeyedField",
    "RuleApplication",
    "RuleOutcome",
    "apply_comparison_view",
    "resolve_comparison_view_rule",
]

COMPARISON_VIEW_FUNCTION_VERSION: Final[int] = 1
"""Version of how :func:`apply_comparison_view` composes its rules: their order,
what each is handed and what the record holds. Bumped on any change to the view it
composes from the same rules, so a recorded view can be told apart from one a later
engine would compute. A change to what one rule computes bumps that rule's
``VERSION`` instead (:class:`ComparisonViewRule`)."""

COMPARISON_VIEW_VERSIONS: Final[tuple[int, ...]] = (1,)
"""Schema versions of the ``comparison_view`` block this engine reads."""

ALL_ZERO: Final[str] = "all_zero"
"""The ``where`` key whose value lists fields that must all be numerically zero.
It is reserved, so a field named ``all_zero`` cannot take a condition."""

_DEFAULT_ID_FIELD: Final[str] = "id"
_UNLESS: Final[str] = "unless_referenced_by"
_NORMALIZE: Final[str] = "normalize_ids"
_ID_TYPES: Final[tuple[type, ...]] = (str, int, float, bool)
_PLAIN_DECIMAL: Final[re.Pattern[str]] = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", re.ASCII)


class ComparisonViewError(ValueError):
    """A rule cannot compute the view of this state.

    It is an evaluation error, never a pass or a fail: the grade reports it as a
    grading error.
    """


class ComparisonViewCollision(ComparisonViewError):
    """``normalize_ids`` cannot re-key this state bijectively.

    Two records would share a key, a new key is the id of a kept record, two
    records tie on ``rank_by``, or a reference already holds a new key. On the
    golden side it is an evaluation error like any :class:`ComparisonViewError`.
    On the trial side, once the golden's view succeeded, it is the trial's own
    state that cannot be told apart, and the caller fails the trial with it as
    the reason. ``ids`` are the ids involved.
    """

    def __init__(self, message: str, ids: tuple[Any, ...]) -> None:
        super().__init__(message, ids)
        self.ids = ids

    def __str__(self) -> str:
        return str(self.args[0])


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
            f"e.g. 'orders.line_items'"
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

    Every rule's config model derives from this class and keeps its
    ``extra="forbid"``, so an entry key the rule does not declare is refused.
    ``kind`` is the name the rule is registered under; everything else belongs to
    the rule. A field named ``reason`` is prose: :meth:`ComparisonViewConfig.config_sha256`
    leaves it out. ``order_free_fields`` are the list fields whose order has no
    effect on what the rule does; the config sha sorts them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_free_fields: ClassVar[frozenset[str]] = frozenset()

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


class NormalizeIdsConfig(ComparisonViewRuleConfig):
    """``normalize_ids``: re-key the records in scope and every listed reference to them.

    The new key comes from ``key`` (the record's own content) or from ``rank_by``
    (its ordinal within its ``ordinal_by`` group, ranked by those fields), never
    both.
    """

    order_free_fields: ClassVar[frozenset[str]] = frozenset({"key", "ordinal_by", "references"})

    kind: Literal["normalize_ids"] = "normalize_ids"
    table: NonBlankStr
    key: tuple[FieldName, ...] = ()
    ordinal_by: tuple[FieldName, ...] = ()
    rank_by: tuple[FieldName, ...] = ()
    references: tuple[RecordReference, ...] = ()
    scope: Literal["new_records", "all"] = "new_records"
    reason: NonBlankStr | None = None

    @field_validator("key", "ordinal_by", "rank_by")
    @classmethod
    def _each_field_once(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError(f"lists a field more than once: {list(value)}")
        return value

    @field_validator("references")
    @classmethod
    def _each_reference_once(
        cls, value: tuple[RecordReference, ...]
    ) -> tuple[RecordReference, ...]:
        named = [f"{reference.table}.{reference.field}" for reference in value]
        repeated = sorted({name for name in named if named.count(name) > 1})
        if repeated:
            raise ValueError(f"lists the reference(s) {repeated} more than once")
        return value

    @model_validator(mode="after")
    def _one_key_form(self) -> NormalizeIdsConfig:
        if bool(self.key) == bool(self.rank_by):
            raise ValueError(
                "declare the new key either as key: [fields] or as rank_by: [fields] with "
                "an optional ordinal_by: [fields], exactly one of the two"
            )
        if self.key and self.ordinal_by:
            raise ValueError(
                "ordinal_by groups the records rank_by orders; it does not combine with key"
            )
        shared = sorted(set(self.ordinal_by) & set(self.rank_by))
        if shared:
            raise ValueError(
                f"ordinal_by and rank_by both list {shared}; a field that is constant within "
                f"a group cannot rank it"
            )
        rewritten = sorted(self.key_fields() & self.rewritten_fields(self.table))
        if rewritten:
            raise ValueError(
                f"the new key reads {rewritten}, which references rewrite in table "
                f"{self.table!r}; the key would be built from values the rule then changes"
            )
        return self

    def names(self) -> tuple[str, ...]:
        references = (reference.table for reference in self.references)
        return tuple(dict.fromkeys((self.table, *references)))

    def key_fields(self) -> frozenset[str]:
        """The fields of the table's records the new key is built from."""
        return frozenset((*self.key, *self.ordinal_by, *self.rank_by))

    def rewritten_fields(self, table: str) -> frozenset[str]:
        """The top-level fields of ``table`` its ``references`` rewrite."""
        return frozenset(
            reference.field
            for reference in self.references
            if reference.table == table and "." not in reference.field
        )


# ---------------------------------------------------------------------------
# What a view records
# ---------------------------------------------------------------------------


class RuleApplication(BaseModel):
    """What one rule did to one table, in rule order.

    ``rows_removed`` counts the rows of ``table`` a rule removed or, with
    ``path``, the items of the nested lists at that path. ``exclude_tables``
    contributes one application per listed table; a table held as a single value
    rather than a list of rows counts as one row, and an absent one as none.
    ``ids_rewritten`` counts the records of ``table`` whose key ``normalize_ids``
    changed, and ``references_rewritten`` the reference values it changed to
    follow them, over every listed reference field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    table: str
    path: str | None = None
    rows_removed: int = Field(default=0, ge=0)
    ids_rewritten: int = Field(default=0, ge=0)
    references_rewritten: int = Field(default=0, ge=0)


class RekeyedField(BaseModel):
    """An id field ``normalize_ids`` re-keyed: its values are now a function of content.

    A masked id is unstable because it is generated; a re-keyed one is not, so it
    must reach the hash. The masks applied after the view (the unstable filter and
    the clock mask) must not drop it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    field: str

    @property
    def dotted(self) -> str:
        """The ``table.field`` form ``unstable_fields`` names it in."""
        return f"{self.table}.{self.field}"


class ComparisonViewRecord(BaseModel):
    """Which transform produced a view, recorded with the grade it is compared for.

    ``version`` is the block's schema version, ``function_version`` the engine's
    :data:`COMPARISON_VIEW_FUNCTION_VERSION`, and ``config_sha256`` the digest of
    what the rules do (:meth:`ComparisonViewConfig.config_sha256`).
    ``rekeyed_fields`` are the id fields ``normalize_ids`` re-keyed; they follow
    from the declaration and ``id_fields``, not from which records were in scope,
    so both sides of a comparison name the same ones.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    function_version: int
    config_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    applied: tuple[RuleApplication, ...]
    rekeyed_fields: tuple[RekeyedField, ...] = ()


@dataclass(frozen=True)
class RuleOutcome:
    """What a rule returns: the state after it, what it did to each table, and the
    id fields it re-keyed."""

    state: dict[str, Any]
    applied: tuple[RuleApplication, ...]
    rekeyed: tuple[RekeyedField, ...] = ()


@dataclass(frozen=True)
class ComparisonViewResult:
    """The view of one state, and the record of how it was computed."""

    state: dict[str, list[dict[str, Any]]]
    record: ComparisonViewRecord

    @property
    def rekeyed_fields(self) -> tuple[RekeyedField, ...]:
        """The id fields the masks after the view must not drop (see :class:`RekeyedField`)."""
        return self.record.rekeyed_fields


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


@runtime_checkable
class ComparisonViewRule(Protocol):
    """A rule the ``kind`` of a ``comparison_view`` entry resolves to.

    A rule is a class a distribution registers under its ``NAME`` in the
    ``tolokaforge.comparison_view_rules`` entry-point group; the view instantiates
    it, without arguments, for each entry naming it::

        [project.entry-points."tolokaforge.comparison_view_rules"]
        drop_drafts = "acme_rules:DropDrafts"

    - ``NAME`` equals the entry-point name.
    - ``VERSION`` is a positive int, the version of what the rule computes: bump it
      on any change to the view the rule produces from the same inputs.
      :meth:`ComparisonViewConfig.config_sha256` hashes it with ``NAME`` and the
      entry's settings, so a recorded view names the implementation behind it.
    - ``config_model`` validates the entry. It derives from
      :class:`ComparisonViewRuleConfig`, keeps ``extra="forbid"``, and narrows
      ``kind`` to ``Literal[NAME]``.
    - ``apply`` is a pure function of one state, its initial state and the rule's
      config. It must not mutate ``state``, ``initial`` or ``id_fields``; it returns
      the next state in its outcome (unchanged tables may be shared with
      ``state``) with a :class:`RuleApplication` per table it touched, recorded
      under its ``NAME``; and it raises :class:`ComparisonViewError` when the state
      does not fit the declaration.

    :func:`resolve_comparison_view_rule` refuses a registration that breaks the
    declared parts of this contract, and :func:`apply_comparison_view` an outcome
    that does.

    **The trust boundary.** A rule decides which two states hash equal, so a
    registered rule can turn a failing trial into a passing one: a rule that drops
    every table passes anything. That is a stronger grant than a judge kind's or a
    search backend's. A rule rewrites the evidence the binary hash verdict is
    computed from, before every mask, and only its identity in the grade shows that
    it did. The engine holds a rule to four things:

    - it runs only for a task whose ``comparison_view`` names its kind, so
      installing a distribution changes the grade of no other task;
    - a name has one registration: a second distribution registering a name, a
      built-in's included, fails every lookup into the group
      (:class:`~tolokaforge.core.plugin_registry.DuplicateRegistrationError`);
    - it sees one side: the other state is never an input, and the view hands
      its rules a deep copy of the state, the initial state and ``id_fields``;
    - its identity is in the grade: ``NAME`` and ``VERSION`` are hashed into the
      record's ``config_sha256``, and what it did is recorded under its ``NAME``.

    Nothing in the engine tells a sound rule from an unsound one. A rule is part of
    the answer key: review and pin a distribution that registers one as a task's
    golden actions are reviewed.
    """

    NAME: ClassVar[str]
    VERSION: ClassVar[int]
    config_model: ClassVar[type[ComparisonViewRuleConfig]]

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

    NAME: ClassVar[str] = "exclude_records"
    VERSION: ClassVar[int] = 1
    config_model: ClassVar[type[ComparisonViewRuleConfig]] = ExcludeRecordsConfig

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
            kind=self.NAME, table=config.table, path=config.path, rows_removed=removed
        )


class ExcludeTables:
    """The ``exclude_tables`` rule."""

    NAME: ClassVar[str] = "exclude_tables"
    VERSION: ClassVar[int] = 1
    config_model: ClassVar[type[ComparisonViewRuleConfig]] = ExcludeTablesConfig

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
            RuleApplication(kind=self.NAME, table=table, rows_removed=_row_count(state, table))
            for table in config.tables
        )
        kept = {table: rows for table, rows in state.items() if table not in dropped}
        return RuleOutcome(state=kept, applied=applied)


def _row_count(state: Mapping[str, Any], table: str) -> int:
    if table not in state:
        return 0
    rows = state[table]
    return len(rows) if isinstance(rows, list) else 1


class NormalizeIds:
    """The ``normalize_ids`` rule."""

    NAME: ClassVar[str] = "normalize_ids"
    VERSION: ClassVar[int] = 1
    config_model: ClassVar[type[ComparisonViewRuleConfig]] = NormalizeIdsConfig

    def apply(
        self,
        state: dict[str, Any],
        *,
        initial: Mapping[str, Any] | None,
        id_fields: Mapping[str, str | list[str]],
        config: ComparisonViewRuleConfig,
    ) -> RuleOutcome:
        config = cast(NormalizeIdsConfig, config)
        id_field = _record_id_field(config.table, id_fields, needed_by=_NORMALIZE)
        _refuse_id_in_the_key(config, id_field)
        _refuse_own_id_references(config.table, config.references, id_field, option="references")
        kept_ids = _ids_that_keep_their_key(config, initial, id_field)
        rekeyed_fields = (RekeyedField(table=config.table, field=id_field),)
        if config.table not in state:
            application = RuleApplication(kind=self.NAME, table=config.table)
            return RuleOutcome(state=state, applied=(application,), rekeyed=rekeyed_fields)
        rows = _records(state[config.table], f"{_NORMALIZE}: table {config.table!r}")
        new_keys = _new_keys(config, rows, id_field, kept_ids)
        rekeyed = [_rekeyed(row, id_field, new_keys) for row in rows]
        next_state, references_rewritten = _follow_references(
            {**state, config.table: rekeyed}, config, new_keys
        )
        application = RuleApplication(
            kind=self.NAME,
            table=config.table,
            ids_rewritten=sum(new is not old for new, old in zip(rekeyed, rows)),
            references_rewritten=references_rewritten,
        )
        return RuleOutcome(state=next_state, applied=(application,), rekeyed=rekeyed_fields)


def _registry() -> ModuleType:
    """:mod:`tolokaforge.core.plugin_registry`, imported where a kind resolves.

    Not at module level: the registry imports every seam's module and the engine's
    config models, which importing this module must not load.
    """
    from tolokaforge.core import plugin_registry

    return plugin_registry


def resolve_comparison_view_rule(kind: str) -> type[ComparisonViewRule]:
    """The rule class registered as ``kind`` in ``tolokaforge.comparison_view_rules``.

    Raises:
        UnknownImplementationError: nothing registers ``kind``; the error lists what
            is registered.
        DuplicateRegistrationError: two distributions register one name.
        TypeError: the registration is not a rule — see :func:`_a_rule`.
    """
    return _a_rule(kind, _registry().load_comparison_view_rule(kind))


def _a_rule(kind: str, loaded: object) -> type[ComparisonViewRule]:
    """``loaded`` as the rule registered as ``kind``, or :class:`TypeError` naming what it lacks.

    Checks the declared parts of :class:`ComparisonViewRule`: a class whose ``NAME``
    is ``kind``, whose ``VERSION`` is a positive int, whose ``config_model`` derives
    from :class:`ComparisonViewRuleConfig` and forbids extra keys, and which has an
    ``apply``.
    """
    where = f"the comparison_view rule registered as {kind!r}"
    if not isinstance(loaded, type):
        raise TypeError(f"{where} is {loaded!r}, not a class")
    name = getattr(loaded, "NAME", None)
    if name != kind:
        raise TypeError(
            f"{where} is {loaded.__qualname__}, whose NAME is {name!r}; a rule is "
            f"registered under its NAME"
        )
    version = getattr(loaded, "VERSION", None)
    if type(version) is not int or version < 1:
        raise TypeError(
            f"{where} declares VERSION {version!r}; a rule's VERSION is a positive int, "
            f"hashed into the view's config_sha256"
        )
    config_model = getattr(loaded, "config_model", None)
    if not (isinstance(config_model, type) and issubclass(config_model, ComparisonViewRuleConfig)):
        raise TypeError(
            f"{where} declares config_model {config_model!r}, which does not derive from "
            f"ComparisonViewRuleConfig"
        )
    if config_model.model_config.get("extra") != "forbid":
        raise TypeError(
            f"{where} declares config_model {config_model.__qualname__}, which does not "
            f'forbid extra keys; a rule\'s entry refuses a key it does not declare (extra="forbid")'
        )
    if not callable(getattr(loaded, "apply", None)):
        raise TypeError(f"{where} has no apply method")
    return cast(type[ComparisonViewRule], loaded)


def _checked_outcome(rule: type[ComparisonViewRule], outcome: object) -> RuleOutcome:
    """``outcome`` as ``rule`` returned it, or :class:`TypeError` if it breaks the contract."""
    where = f"comparison_view rule {rule.NAME!r}"
    if not isinstance(outcome, RuleOutcome) or not isinstance(outcome.state, dict):
        raise TypeError(
            f"{where} returned {outcome!r}; apply returns a RuleOutcome holding the next "
            f"state as a dict"
        )
    if not all(isinstance(application, RuleApplication) for application in outcome.applied):
        raise TypeError(f"{where} reports {outcome.applied!r}, not RuleApplication entries")
    foreign = sorted({application.kind for application in outcome.applied} - {rule.NAME})
    if foreign:
        raise TypeError(
            f"{where} reports applications under {foreign}; a rule records what it did "
            f"under its own NAME"
        )
    return outcome


# ---------------------------------------------------------------------------
# exclude_records helpers
# ---------------------------------------------------------------------------

Record = Mapping[str, Any]
IdKey = tuple[bool, Any]
"""An id or a reference as :func:`_reference_key` compares it."""


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
    id_field = _record_id_field(config.table, id_fields, needed_by=_UNLESS)
    _refuse_own_id_references(config.table, config.unless_referenced_by, id_field, option=_UNLESS)
    counts = _reference_counts(state, config.unless_referenced_by)
    return [
        row
        for row in rows
        if not matches(row) or _referenced_by_another_row(row, config, id_field, counts)
    ]


def _refuse_own_id_references(
    table: str, references: Sequence[RecordReference], id_field: str, *, option: str
) -> None:
    for reference in references:
        if reference.table == table and reference.field == id_field:
            raise ComparisonViewError(
                f"{option} names {table}.{id_field}, the id field of the rule's own table: "
                f"a record's own id is not a reference to it"
            )


def _referenced_by_another_row(
    row: Record, config: ExcludeRecordsConfig, id_field: str, counts: Counter[IdKey]
) -> bool:
    """Whether a reference other than the row's own references to itself holds its id."""
    key = _reference_key(_record_id(row, config.table, id_field, needed_by=_UNLESS))
    own = sum(
        1
        for reference in config.unless_referenced_by
        if reference.table == config.table
        for value in _ids_at(row, _segments(reference.field), _reference_label(reference, _UNLESS))
        if _reference_key(value) == key
    )
    return counts[key] > own


def _record_id_field(
    table: str, id_fields: Mapping[str, str | list[str]], *, needed_by: str
) -> str:
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
            f"{needed_by} needs one id field for table {table!r}, and a composite key has "
            f"no single field a reference could hold"
        )
    return fields[0]


def _record_id(row: Record, table: str, id_field: str, *, needed_by: str) -> Any:
    """A record's id, which ``needed_by`` reads: a JSON scalar, never missing or null."""
    value = row.get(id_field)
    if value is None:
        raise ComparisonViewError(
            f"{needed_by} reads the id of a record of table {table!r}, but its id field "
            f"{id_field!r} is missing or null; declare state_checks.id_fields[{table!r}]"
        )
    if not isinstance(value, _ID_TYPES):
        raise ComparisonViewError(
            f"a record of table {table!r} holds a {type(value).__name__} in its id field "
            f"{id_field!r}; an id is a string, a number or a bool"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ComparisonViewError(
            f"a record of table {table!r} holds {value!r} in its id field {id_field!r}; a "
            f"non-finite number is not a JSON value and never equals a reference to it"
        )
    return value


def _reference_key(value: Any) -> IdKey:
    """The key an id and a reference to it share, compared as JSON values.

    A bool never matches a number, ``1`` matches ``1.0``, and ``1`` does not match
    ``"1"``: the JSON types differ.
    """
    return isinstance(value, bool), value


def _reference_counts(
    state: Mapping[str, Any], references: Sequence[RecordReference]
) -> Counter[IdKey]:
    """How many times each id is referenced by the listed fields, over every row."""
    counts: Counter[IdKey] = Counter()
    for reference in references:
        if reference.table not in state:
            continue
        rows = _records(state[reference.table], f"{_UNLESS}: table {reference.table!r}")
        found = _ids_at(rows, _segments(reference.field), _reference_label(reference, _UNLESS))
        counts.update(map(_reference_key, found))
    return counts


def _reference_label(reference: RecordReference, option: str) -> str:
    return f"{option}: '{reference.table}.{reference.field}'"


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
# normalize_ids helpers
# ---------------------------------------------------------------------------


def _refuse_id_in_the_key(config: NormalizeIdsConfig, id_field: str) -> None:
    for option, fields in (
        ("key", config.key),
        ("ordinal_by", config.ordinal_by),
        ("rank_by", config.rank_by),
    ):
        if id_field in fields:
            raise ComparisonViewError(
                f"{_NORMALIZE} {option} names {config.table}.{id_field}, the id field it "
                f"replaces; a key built from the generated id normalizes nothing"
            )


def _ids_that_keep_their_key(
    config: NormalizeIdsConfig, initial: Mapping[str, Any] | None, id_field: str
) -> frozenset[IdKey]:
    """The ids of the initial state's records of the table, under ``scope: new_records``."""
    if config.scope == "all":
        return frozenset()
    if initial is None:
        raise ComparisonViewError(
            f"{_NORMALIZE} of table {config.table!r} has scope new_records, which reads the "
            f"initial state, and none was given"
        )
    if config.table not in initial:
        return frozenset()
    rows = _records(initial[config.table], f"{_NORMALIZE}: initial table {config.table!r}")
    return frozenset(
        _reference_key(_record_id(row, config.table, id_field, needed_by=_NORMALIZE))
        for row in rows
    )


def _new_keys(
    config: NormalizeIdsConfig, rows: list[Record], id_field: str, kept_ids: frozenset[IdKey]
) -> dict[IdKey, str]:
    """old id → new key for the records in scope; raises unless the re-keying is bijective."""
    ids = [_record_id(row, config.table, id_field, needed_by=_NORMALIZE) for row in rows]
    _refuse_duplicate_ids(config.table, ids)
    in_scope = [(row, old) for row, old in zip(rows, ids) if _reference_key(old) not in kept_ids]
    if config.key:
        rendered = [
            (old, _rendered_key(config.table, _key_fields(config, row, old, config.key)))
            for row, old in in_scope
        ]
    else:
        rendered = _ordinal_keys(config, in_scope)
    _refuse_colliding_keys(
        config.table, rendered, [old for old in ids if _reference_key(old) in kept_ids]
    )
    return {_reference_key(old): new for old, new in rendered}


def _refuse_duplicate_ids(table: str, ids: list[Any]) -> None:
    seen: set[IdKey] = set()
    for value in ids:
        key = _reference_key(value)
        if key in seen:
            raise ComparisonViewError(
                f"{_NORMALIZE}: two records of table {table!r} share the id {value!r}, so a "
                f"reference to it cannot follow one of them"
            )
        seen.add(key)


def _key_fields(
    config: NormalizeIdsConfig, row: Record, old_id: Any, fields: Sequence[str]
) -> dict[str, Any]:
    missing = [field for field in fields if field not in row]
    if missing:
        raise ComparisonViewError(
            f"{_NORMALIZE}: record {old_id!r} of table {config.table!r} lacks the key "
            f"field(s) {missing}"
        )
    return {field: _key_value(row[field], config.table, old_id, field) for field in fields}


def _key_value(value: Any, table: str, old_id: Any, field: str) -> Any:
    """A key component as JSON renders it; an integral float renders as the int the hash folds
    it to, so ``5.0`` and ``5``, and ``1e23`` and ``10**23``, keep one key."""
    if value is not None and not isinstance(value, _ID_TYPES):
        raise ComparisonViewError(
            f"{_NORMALIZE}: record {old_id!r} of table {table!r} holds a "
            f"{type(value).__name__} in key field {field!r}; a key is built from JSON scalars"
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ComparisonViewError(
                f"{_NORMALIZE}: record {old_id!r} of table {table!r} holds {value!r} in key "
                f"field {field!r}, which JSON cannot render"
            )
        if value.is_integer():
            # Through the shortest decimal, as the hash folds it: int(1e23) is 99999999999999991611392.
            return int(Decimal(repr(value)))
    return value


def _rendered_key(table: str, fields: Mapping[str, Any], ordinal: int | None = None) -> str:
    """``<table>:<canonical JSON of the key fields>``, then ``#<ordinal>`` for the ordinal form."""
    body = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{table}:{body}" if ordinal is None else f"{table}:{body}#{ordinal}"


@dataclass(frozen=True)
class _Ranked:
    """A record in scope of the ordinal form: its rank, its old id and its group."""

    order: tuple[tuple[int, Any], ...]
    old_id: Any
    group: dict[str, Any]


def _ordinal_keys(
    config: NormalizeIdsConfig, in_scope: list[tuple[Record, Any]]
) -> list[tuple[Any, str]]:
    """Each record's ordinal, from 1, within its ``ordinal_by`` group ranked by ``rank_by``."""
    groups: dict[tuple[IdKey, ...], list[_Ranked]] = {}
    for row, old in in_scope:
        group = _key_fields(config, row, old, config.ordinal_by)
        rank = _key_fields(config, row, old, config.rank_by)
        member = _Ranked(tuple(map(_rank_order, rank.values())), old, group)
        groups.setdefault(tuple(map(_reference_key, group.values())), []).append(member)
    rendered: list[tuple[Any, str]] = []
    for members in groups.values():
        members.sort(key=lambda member: member.order)
        _refuse_rank_ties(config, members)
        rendered += [
            (member.old_id, _rendered_key(config.table, member.group, ordinal))
            for ordinal, member in enumerate(members, 1)
        ]
    return rendered


def _refuse_rank_ties(config: NormalizeIdsConfig, members: list[_Ranked]) -> None:
    for first, second in zip(members, members[1:]):
        if first.order == second.order:
            raise ComparisonViewCollision(
                f"{_NORMALIZE}: records {first.old_id!r} and {second.old_id!r} of table "
                f"{config.table!r} tie on rank_by {list(config.rank_by)}, so their ordinals "
                f"would depend on row order",
                (first.old_id, second.old_id),
            )


def _rank_order(value: Any) -> tuple[int, Any]:
    """A total order over JSON scalars: null, then bools, numbers, strings."""
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, int | float):
        return (2, value)
    return (3, value)


def _refuse_colliding_keys(table: str, rendered: list[tuple[Any, str]], kept: list[Any]) -> None:
    owners: dict[IdKey, Any] = {_reference_key(old): old for old in kept}
    for old, new in rendered:
        other = owners.get(_reference_key(new))
        if other is not None:
            raise ComparisonViewCollision(
                f"{_NORMALIZE}: record {old!r} of table {table!r} gets the key {new!r}, which "
                f"record {other!r} already holds; the key does not tell them apart",
                (other, old),
            )
        owners[_reference_key(new)] = old


def _rekeyed(row: Record, id_field: str, new_keys: Mapping[IdKey, str]) -> Record:
    new = new_keys.get(_reference_key(row[id_field]))
    if new is None or _reference_key(new) == _reference_key(row[id_field]):
        return row
    return {**row, id_field: new}


def _follow_references(
    state: dict[str, Any], config: NormalizeIdsConfig, new_keys: Mapping[IdKey, str]
) -> tuple[dict[str, Any], int]:
    """``state`` with every listed reference to a re-keyed record rewritten, and how many were."""
    owners = {_reference_key(new): old_key[1] for old_key, new in new_keys.items()}
    rewritten = 0

    def follow(value: Any, where: str) -> Any:
        nonlocal rewritten
        if isinstance(value, list):
            return [follow(item, where) for item in value]
        if value is None:
            return value
        if not isinstance(value, _ID_TYPES):
            raise ComparisonViewError(f"{where} holds a {type(value).__name__}, not an id")
        key = _reference_key(value)
        if key in new_keys:
            rewritten += _reference_key(new_keys[key]) != key
            return new_keys[key]
        if key in owners:
            raise ComparisonViewCollision(
                f"{where} holds {value!r}, the new key of record {owners[key]!r}, as a "
                f"reference to a record that is not re-keyed; it would follow the wrong record",
                (value, owners[key]),
            )
        return value

    for reference in config.references:
        if reference.table not in state:
            continue
        where = _reference_label(reference, "references")
        rows = _records(state[reference.table], f"references: table {reference.table!r}")
        leaf = partial(follow, where=where)
        state = {
            **state,
            reference.table: _rewrite_at(rows, _segments(reference.field), leaf, where),
        }
    return state, rewritten


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


class ComparisonViewConfig(BaseModel):
    """The ``state_checks.comparison_view`` block: a schema version and rules in list order.

    ``kind`` resolves through the ``tolokaforge.comparison_view_rules`` entry-point
    group (:func:`resolve_comparison_view_rule`), not a static union, so a rule a
    distribution registers validates the way the built-ins do. Dump it
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

    @model_validator(mode="after")
    def _keys_are_built_from_rewritten_values(self) -> ComparisonViewConfig:
        rules = list(enumerate(self.rules))
        for index, rule in rules:
            if isinstance(rule, NormalizeIdsConfig):
                _refuse_a_later_rewrite_of_the_key(index, rule, rules[index + 1 :])
        return self

    @model_validator(mode="after")
    def _a_table_is_normalized_once(self) -> ComparisonViewConfig:
        normalized: dict[str, int] = {}
        for index, rule in enumerate(self.rules):
            if not isinstance(rule, NormalizeIdsConfig):
                continue
            if rule.table in normalized:
                raise ValueError(
                    f"rules[{normalized[rule.table]}] and rules[{index}] both normalize the ids "
                    f"of table {rule.table!r}; a table is re-keyed once"
                )
            normalized[rule.table] = index
        return self

    def config_sha256(self) -> str:
        """sha256 of what the rules do: each rule's kind, its ``VERSION`` and its settings.

        The policy (ADR-0053 § Versioning): a setting at its default is left out,
        so a new optional field whose default keeps a rule's behaviour keeps every
        existing sha; a change to what a rule computes bumps the rule's ``VERSION``,
        which changes the sha of every view naming it; ``reason`` is prose, not
        behaviour, and is not hashed; a list whose order has no effect
        (``order_free_fields``) is hashed sorted. The JSON is canonical as
        ``ModelsFingerprint`` hashes model data (sorted keys, ASCII, no whitespace),
        so the key order of the declaration does not change it either.
        """
        payload = [_hashed_rule(rule) for rule in self.rules]
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _hashed_rule(rule: ComparisonViewRuleConfig) -> dict[str, Any]:
    settings = rule.model_dump(
        mode="json", by_alias=True, exclude_defaults=True, exclude={"kind", "reason"}
    )
    for name in rule.order_free_fields & settings.keys():
        settings[name] = sorted(settings[name], key=_canonical_json)
    version = resolve_comparison_view_rule(rule.kind).VERSION
    return {"kind": rule.kind, "version": version, "settings": settings}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _resolve_entry(index: int, entry: Any) -> ComparisonViewRuleConfig:
    """Validate one entry of ``rules`` through the rule its ``kind`` names."""
    registry = _registry()
    if isinstance(entry, ComparisonViewRuleConfig):
        kind: Any = entry.kind
    elif isinstance(entry, Mapping) and "kind" in entry:
        kind = entry["kind"]
    else:
        raise ValueError(
            f"rules[{index}] must be a mapping with a 'kind'; registered kinds: "
            f"{registry.available_comparison_view_rules()}"
        )
    registered = registry.available_comparison_view_rules()
    if not isinstance(kind, str) or kind not in registered:
        raise ValueError(
            f"rules[{index}]: unknown comparison_view rule kind {kind!r}; registered kinds: "
            f"{registered} (a rule registers in the "
            f"{registry.COMPARISON_VIEW_RULES_GROUP!r} entry-point group: install the "
            f"distribution that provides it, or check the name)"
        )
    rule = resolve_comparison_view_rule(kind)
    if isinstance(entry, ComparisonViewRuleConfig):
        if not isinstance(entry, rule.config_model):
            raise ValueError(
                f"rules[{index}] is a {type(entry).__name__}, not the {kind} rule's config "
                f"model {rule.config_model.__name__}"
            )
        return entry
    try:
        return rule.config_model.model_validate(entry)
    except ValidationError as exc:
        raise ValueError(f"rules[{index}] ({rule.NAME}): {_error_summary(exc)}") from None


def _refuse_a_later_rewrite_of_the_key(
    index: int,
    rule: NormalizeIdsConfig,
    later: Sequence[tuple[int, ComparisonViewRuleConfig]],
) -> None:
    for later_index, other in later:
        if not isinstance(other, NormalizeIdsConfig):
            continue
        rewritten = sorted(rule.key_fields() & other.rewritten_fields(rule.table))
        if rewritten:
            raise ValueError(
                f"rules[{index}] (normalize_ids) builds keys of table {rule.table!r} from "
                f"{rewritten}, which rules[{later_index}] rewrites as references afterwards; "
                f"put rules[{later_index}] first, so the key is built from rewritten values"
            )


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
    ``id_fields`` is ``state_checks.id_fields`` (absent table → ``"id"``). No input
    is mutated: the rules get private copies of all three, and the returned state
    shares nothing with ``state``. A rule that cannot apply raises
    :class:`ComparisonViewError`; a rule whose outcome breaks the
    :class:`ComparisonViewRule` contract, :class:`TypeError`.
    """
    working: dict[str, Any] = copy.deepcopy(dict(state))
    initial_copy = None if initial is None else copy.deepcopy(dict(initial))
    id_fields_copy = MappingProxyType(copy.deepcopy(dict(id_fields)))
    applied: list[RuleApplication] = []
    rekeyed: list[RekeyedField] = []
    for config in view.rules:
        rule = resolve_comparison_view_rule(config.kind)
        outcome = rule().apply(
            working, initial=initial_copy, id_fields=id_fields_copy, config=config
        )
        working = _checked_outcome(rule, outcome).state
        applied.extend(outcome.applied)
        rekeyed.extend(outcome.rekeyed)
    record = ComparisonViewRecord(
        version=view.version,
        function_version=COMPARISON_VIEW_FUNCTION_VERSION,
        config_sha256=view.config_sha256(),
        applied=tuple(applied),
        rekeyed_fields=tuple(rekeyed),
    )
    return ComparisonViewResult(state=working, record=record)
