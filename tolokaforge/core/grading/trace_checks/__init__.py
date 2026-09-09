"""Substrate-neutral evaluation of a trial's trace-check constraints.

Substrate-neutral and pure — no services, no I/O — over the timeline both
substrates build, so a constraint reaches the same verdict whichever substrate
grades the trial.

:func:`select_events` is the **only** function that resolves a matcher, and it
resolves one under an environment of bound values. A constraint reads events
through it and nothing else, so a constraint declaring a ``bind`` is the whole
existing evaluator run once per candidate assignment rather than a second
evaluator. :func:`evaluate_trace_checks` folds the constraint verdicts into the
component score.

Both layers are three-valued. An event either definitely matches, definitely does
not, or cannot be decided because the evidence a predicate reads was never
recorded — and the third case is a state the timeline reaches routinely, on every
bundle re-graded without its tool-call record. Collapsing it into "did not match"
would satisfy every negative constraint in the agent's favour, which
``docs/GRADING.md`` G4 names as the hazard to avoid. A constraint is therefore
decided only when every completion of the undecidable evidence agrees, and an
undecided constraint is a failing sub-check that names what could not be read.

The result carries the author keys this evaluation decomposed, per constraint
kind, for the runner's accounted-keys ledger. Recording them here rather than in
the runner phase is what makes the account honest: a kind the evaluation never
reaches — one nested inside a composite the walk stopped descending into, say —
is never recorded, where a runner-side walk of the *config* would report it as
evaluated whatever the evaluator did.

Package layout (concern-grouped, all imports resolve through this ``__init__``):

* :mod:`.truth` — the three-valued Kleene truth primitives every layer reads.
* :mod:`.matcher` — :class:`MatcherOutcome` + :func:`select_events` and the
  predicate-resolution helpers a matcher reads through.
* :mod:`.bindings` — candidate-set enumeration and side-reading reductions
  for the constraint's binder.
* :mod:`.resolver` — the per-constraint :class:`_Resolver` and its message-
  rendering surface.
* :mod:`.constraints` — the 10-member constraint-operator vocabulary
  (:mod:`.constraints.presence`, :mod:`.constraints.ordering`,
  :mod:`.constraints.windows`, :mod:`.constraints.logical`), grouped by the
  private helpers each cluster shares.
* :mod:`.dispatch` — the ``_HANDLERS`` registry every composite operator
  (``_all_of`` / ``_any_of`` / ``_negate``) recurses through.
* :mod:`.evaluator` — :func:`evaluate_trace_checks`, the top-level fold,
  ``_KindLedger`` accounting, and the multi-path / severity-gate machinery.

The authored vocabulary is documented in ``docs/GRADING.md`` § "Trace Checks".
"""

from tolokaforge.core.grading.trace_checks.bindings import _candidates, _extracted
from tolokaforge.core.grading.trace_checks.constraints.ordering import _VIEW_KINDS
from tolokaforge.core.grading.trace_checks.dispatch import _HANDLERS
from tolokaforge.core.grading.trace_checks.evaluator import evaluate_trace_checks
from tolokaforge.core.grading.trace_checks.matcher import (
    MatcherOutcome,
    _binding_operator_names,
    _operator_holds,
    select_events,
)
from tolokaforge.core.grading.trace_checks.resolver import _FAILURE_DETAIL
from tolokaforge.core.models import TraceChecksResult

__all__ = [
    "MatcherOutcome",
    "TraceChecksResult",
    "_FAILURE_DETAIL",
    "_HANDLERS",
    "_VIEW_KINDS",
    "_binding_operator_names",
    "_candidates",
    "_extracted",
    "_operator_holds",
    "evaluate_trace_checks",
    "select_events",
]
