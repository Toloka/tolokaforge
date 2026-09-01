"""State-check backend seam — Protocol + FactoryAlias.

A :class:`StateCheckBackend` scores one source of state-check evidence
against a :class:`~tolokaforge.core.grading.substrate.GradingSubstrate` and
returns ``(score, reasons)`` — the same shape
:func:`~tolokaforge.core.grading.composite.grade_state_checks_reads`
produces per source. Discovery goes through
:func:`~tolokaforge.core.plugin_registry.load_state_check_backend` over the
``tolokaforge.state_check_backends`` entry-point group.

Two reference impls ship: ``jsonpath`` reshapes the substrate's STABLE DB
view + agent-visible filesystem into the ``{db, tables, filesystem}`` state
:func:`~tolokaforge.core.grading.jsonpath_evaluators.evaluate_jsonpath_checks`
addresses; ``db_probes`` opens task-declared postgres connections and applies
each probe's ``expect`` block. A downstream package registers a third source
(e.g. an S3-bucket-diff backend) alongside without a framework PR.

Hash grading is deliberately NOT a registered backend. The
``state_checks.hash`` component has state-mutation semantics (snapshot →
reset → replay → snapshot → restore) that the read-only substrate cannot
serve; hash grading stays runner-integrated on
:meth:`~tolokaforge.runner.service.RunnerServiceImpl._execute_hash_grading`,
called by :meth:`_grade_trial_async` above the composite dispatch.

The reference impls live in
:mod:`tolokaforge.core.grading.default_state_check_backends` — this
Protocol module carries no behaviour so the composite dispatch can name
:class:`StateCheckBackend` without ever reaching a reference impl through
it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.grading.substrate import GradingSubstrate

__all__ = [
    "StateCheckBackend",
    "StateCheckBackendFactory",
]


@runtime_checkable
class StateCheckBackend(Protocol):
    """Score one source of state-check evidence against ``substrate``.

    ``expression`` is the source-specific config (a list of jsonpath check
    dicts for the ``jsonpath`` backend; a list of probe dicts for the
    ``db_probes`` backend). Return the ``(score, reasons)`` pair the
    composite folds into :class:`StateChecksReadResult`; ``(None, None)``
    is a first-class "no evidence to score" signal — an empty expression
    list, or a source-specific gate — that leaves the corresponding
    component slot untouched.

    ``trial_id`` is optional context a backend may weave into audit
    warnings so a multi-trial log stream stays attributable — the
    reference ``jsonpath`` backend prefixes its DB-absent warning with
    it. Backends that emit no per-trial warning ignore the field.
    """

    def query(
        self,
        *,
        expression: list[dict[str, Any]],
        substrate: GradingSubstrate,
        trial_id: str | None = None,
    ) -> tuple[float | None, str | None]: ...


StateCheckBackendFactory = Callable[[], StateCheckBackend]
