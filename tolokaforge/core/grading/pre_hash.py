"""Steps 1–3 of the pre-hash order, for a task that declares a comparison view.

ADR-0053 § The order puts both sides of a hash comparison through five steps::

    full state (unstable fields present)
      → 1. comparison view, rules in list order
      → 2. unstable_fields
      → 3. compare_columns pipeline
      → 4. clock / nullable masks
      → 5. compute_stable_hash | state_digest

Both grading substrates call :func:`view_the_pair` for steps 1–3 and hash what it
returns with their own algebra and masks (steps 4–5): the runner with
:func:`~tolokaforge.core.hash.compute_stable_hash`, core with ``state_digest``. The two
digests keep their different algebras; what this module fixes is that they hash the
same pair of states, so the two substrates reach the same verdict.

Step 2 resolves every ``unstable_fields`` table name the way the db-service resolves
it (:func:`~tolokaforge.core.hash.resolve_unstable_field_paths`), so both substrates
mask the same columns. It resolves against the tables of both *full* states, before
the view drops any, so an ``exclude_tables`` rule cannot make a declared name resolve
to another table. It then leaves out every id field the view re-keyed
(:attr:`~tolokaforge.core.grading.comparison_view.ComparisonViewRecord.rekeyed_fields`):
a re-keyed id is a function of its record's content and must reach the hash even
where ``unstable_fields`` names it. The clock mask of step 4 is not adjusted here: a
re-keyed id field it would drop is refused when the task loads
(:mod:`tolokaforge.core.grading.comparison_view_checks`).

The golden side is viewed first. A view that cannot be computed for it raises
:class:`~tolokaforge.core.grading.comparison_view.ComparisonViewError`, which each
substrate reports as a grading error: the declaration does not fit the state the task's
own golden path builds. Once the golden's view succeeded, the declaration is shown
sound, so any :class:`~tolokaforge.core.grading.comparison_view.ComparisonViewError` on
the trial side — a re-keying that is not bijective, a record without its key field, a
value no rule can read — is the trial's own state that cannot be viewed:
:func:`view_the_pair` returns it as a :class:`TrialViewError`, which fails the trial.

A task without a ``comparison_view`` never reaches this module: each substrate keeps
its own path, so no existing digest moves.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tolokaforge.core.grading.comparison_view import (
    ComparisonViewConfig,
    ComparisonViewError,
    ComparisonViewRecord,
    RekeyedField,
    apply_comparison_view,
)
from tolokaforge.core.grading.trial_golden_diff import compute_view_diff
from tolokaforge.core.hash import (
    ColumnCompareRule,
    apply_compare_columns_pipeline,
    filter_unstable_fields,
    resolve_unstable_field_paths,
)
from tolokaforge.runner.models import (
    ComparisonViewGradeRecord,
    ComparisonViewTrialError,
)

__all__ = [
    "PreHashDeclaration",
    "TrialViewError",
    "ViewedPair",
    "comparison_view_grade_record",
    "comparison_view_reason",
    "resolve_unstable_fields",
    "view_the_pair",
]


@dataclass(frozen=True)
class PreHashDeclaration:
    """What a task declares for steps 1–3, in the shape both substrates hand over.

    ``unstable_fields`` are the dotted ``table.field`` paths the task declares, with
    the table names as written: resolution against the states is this module's job.
    """

    view: ComparisonViewConfig
    id_fields: Mapping[str, str | list[str]] = field(default_factory=dict)
    unstable_fields: tuple[str, ...] = ()
    compare_columns: Mapping[str, Mapping[str, ColumnCompareRule]] = field(default_factory=dict)
    numeric_string_fields: tuple[str, ...] = ()
    auto_normalize_nullables: bool = False


@dataclass(frozen=True)
class ViewedPair:
    """Both sides through steps 1–3, ready for a substrate's own steps 4–5.

    ``trial`` and ``golden`` are what the substrate hashes. ``trial_view`` and
    ``golden_view`` are the two states after steps 1–2, before any fold: what a view
    diff compares, because a fold token is not a value an author can read.
    """

    trial: dict[str, Any]
    golden: dict[str, Any]
    trial_view: dict[str, Any]
    golden_view: dict[str, Any]
    trial_record: ComparisonViewRecord
    golden_record: ComparisonViewRecord


@dataclass(frozen=True)
class TrialViewError:
    """The trial's state cannot be viewed, after the golden's view succeeded: it fails."""

    golden_record: ComparisonViewRecord
    error: ComparisonViewError

    @property
    def ids(self) -> tuple[Any, ...]:
        """The ids the error involves; a collision names them, other errors none."""
        return tuple(getattr(self.error, "ids", ()))


def resolve_unstable_fields(
    unstable_fields: Iterable[str], *states: Mapping[str, Any]
) -> tuple[str, ...]:
    """The declared ``unstable_fields`` with each table resolved against ``states``' tables.

    The db-service's resolution (:func:`~tolokaforge.core.hash.resolve_unstable_field_paths`)
    over the union of the given states' table names, so one comparison masks the same
    columns on both of its sides.
    """
    return tuple(
        resolve_unstable_field_paths(
            unstable_fields, {table for state in states for table in state}
        )
    )


def _masked_after_the_view(
    unstable_fields: Sequence[str],
    rekeyed: Iterable[RekeyedField],
    *states: Mapping[str, Any],
) -> tuple[str, ...]:
    """Step 2's paths: the resolved unstable fields minus every re-keyed id field."""
    kept_in = {rekeyed_field.dotted for rekeyed_field in rekeyed}
    return tuple(
        path for path in resolve_unstable_fields(unstable_fields, *states) if path not in kept_in
    )


def view_the_pair(
    trial: Mapping[str, Any],
    golden: Mapping[str, Any],
    *,
    initial: Mapping[str, Any] | None,
    declaration: PreHashDeclaration,
) -> ViewedPair | TrialViewError:
    """Steps 1–3 for both sides of one comparison, golden first.

    ``trial`` and ``golden`` are full states, unstable fields present; ``initial`` is
    the state both started from. None of the three is mutated.

    Raises:
        ComparisonViewError: the golden's view cannot be computed — a grading error.
            Any error viewing the trial's state is returned as a :class:`TrialViewError`.
    """
    golden_result = apply_comparison_view(
        golden, initial=initial, view=declaration.view, id_fields=declaration.id_fields
    )
    try:
        trial_result = apply_comparison_view(
            trial, initial=initial, view=declaration.view, id_fields=declaration.id_fields
        )
    except ComparisonViewError as error:
        return TrialViewError(golden_record=golden_result.record, error=error)

    masked = _masked_after_the_view(
        declaration.unstable_fields,
        (*golden_result.rekeyed_fields, *trial_result.rekeyed_fields),
        trial,
        golden,
    )
    trial_view = filter_unstable_fields(trial_result.state, list(masked))
    golden_view = filter_unstable_fields(golden_result.state, list(masked))
    trial_folded, golden_folded = apply_compare_columns_pipeline(
        trial_view,
        golden_view,
        {table: dict(rules) for table, rules in declaration.compare_columns.items()},
        numeric_string_fields=(
            frozenset(declaration.numeric_string_fields)
            if declaration.numeric_string_fields
            else None
        ),
        auto_normalize_nullables=declaration.auto_normalize_nullables,
    )
    return ViewedPair(
        trial=trial_folded,
        golden=golden_folded,
        trial_view=trial_view,
        golden_view=golden_view,
        trial_record=trial_result.record,
        golden_record=golden_result.record,
    )


def comparison_view_grade_record(
    outcome: ViewedPair | TrialViewError, *, matched: bool
) -> ComparisonViewGradeRecord:
    """What a grade records about the view: both records, and the view diff on a mismatch.

    The view diff is :func:`~tolokaforge.core.grading.trial_golden_diff.compute_view_diff`
    over the two views after step 2 — the one diff function both substrates' grades
    carry, which names a table only one side holds as well as every row that differs.
    Identical views hash equal on either substrate, so a mismatched digest of the views
    comes with a non-identical view diff (#1444). A trial whose state could not be viewed records the golden's record and
    the error — its type, its message and the ids it names — and no trial record: the
    trial has no view.
    """
    if isinstance(outcome, TrialViewError):
        return ComparisonViewGradeRecord(
            golden=outcome.golden_record,
            trial_error=ComparisonViewTrialError(
                error=type(outcome.error).__name__,
                message=str(outcome.error),
                ids=list(outcome.ids),
            ),
        )
    return ComparisonViewGradeRecord(
        golden=outcome.golden_record,
        trial=outcome.trial_record,
        view_diff=None if matched else compute_view_diff(outcome.trial_view, outcome.golden_view),
    )


def comparison_view_reason(record: ComparisonViewGradeRecord) -> str | None:
    """The sentence a grade carries beside a hash verdict reached through a view.

    ``None`` on a match: the hash sentence already says so. Both substrates render it
    from the record, so the two grades say the same thing.
    """
    if record.trial_error is not None:
        error = record.trial_error
        ids = f" (ids: {error.ids})" if error.ids else ""
        return (
            f"Comparison view: the trial's state cannot be viewed — {error.error}: "
            f"{error.message}{ids}"
        )
    if record.view_diff is not None:
        return f"Comparison view: {record.view_diff.summary}"
    return None
