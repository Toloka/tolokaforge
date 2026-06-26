"""Pure rubric-grading helpers — schema generation, validation, aggregation.

Stage 3 of the rubric-grading plan (``docs/RUBRIC_GRADING_DESIGN.md``). These functions
are deliberately free of any LLM / loop / IO so they are unit-testable in
isolation; Stage 4 wires them into the runner-side judge that runs on the shared
loop.

Three pieces:

1. :func:`build_submit_report_tool` — derive the terminal ``submit_report`` tool's
   JSON-schema arguments from a :class:`~tolokaforge.runner.models.Rubric`.
2. :func:`parse_submit_report` — validate the judge's tool-call arguments against
   the rubric and produce :class:`~tolokaforge.runner.models.CriterionResult` per
   criterion, **failing loud** (never coercing) on any mismatch.
3. :func:`aggregate_rubric` — apply the required-gate and weighted-average scoring
   to a list of ``CriterionResult`` and return a :class:`RubricAggregate`.

Fail-loud contract (AGENTS.md rule 1): :func:`parse_submit_report` raises
:class:`SubmitReportValidationError` on ANY bad judge output. It never returns a
default / placeholder score. Stage 4 catches this exception on bounded-retry
exhaustion and emits an *errored* judge status — never ``0.0`` / ``0.5``.
"""

from dataclasses import dataclass

from tolokaforge.runner.models import Criterion, CriterionResult, Rubric

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Name of the terminal tool the judge calls to submit its rubric verdict.
SUBMIT_REPORT_TOOL_NAME = "submit_report"

#: Threshold a graded criterion's ``score`` must clear for ``met`` to be True.
#: ``met`` is only consulted by the required-gate, so this is the bar at which a
#: graded criterion counts as "passed" for gating purposes.
GRADED_MET_THRESHOLD = 0.5

#: Key suffix for the per-criterion justification field in the tool schema.
_JUSTIFICATION_SUFFIX = "_justification"

#: Key for the overall free-text reasons field.
_REASONS_KEY = "reasons"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SubmitReportValidationError(ValueError):
    """Judge ``submit_report`` arguments did not match the rubric.

    Raised on ANY mismatch — missing criterion, unknown/extra criterion id,
    wrong field type, ``score`` out of ``[0, 1]``, or a missing required field.
    Carries an actionable message naming the offending criterion / field so the
    bounded-retry re-prompt (Stage 4) can surface it to the judge, and so the
    eventual errored-grade status (on retry exhaustion) is diagnosable.
    """


# ---------------------------------------------------------------------------
# Aggregate result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RubricAggregate:
    """Aggregated rubric verdict for the ``llm_judge`` grading component.

    ``score`` is the weighted average over the NON-required criteria only
    (``Σ(w·score)/Σ(w)``), in ``[0, 1]``. Required criteria are pure gates: they
    decide pass/fail via ``gate_failed`` and do NOT contribute to the weighted
    average. When every criterion is required (no non-required criteria to
    average), ``score`` collapses to the gate verdict — ``1.0`` if the gate
    passes, ``0.0`` if a required criterion failed.

    ``binary_pass`` is INDICATIVE only — the judge component's own coarse pass
    signal (``not gate_failed`` and ``score >= GRADED_MET_THRESHOLD``, the 0.5
    bar). It is NOT the authoritative pass verdict: the combine layer (Stage 4)
    decides pass by applying the real ``pass_threshold`` to ``score`` AND
    requiring ``not gate_failed``. Do not mistake the 0.5 bar here for the
    configured pass bar.

    ``gate_failed`` is the explicit required-gate signal Stage 4 feeds into the
    top-level combine layer: when ``True`` the rubric failed outright regardless
    of the weighted ``score`` and the ``pass_threshold``. ``failed_required_ids``
    lists which required criteria tripped the gate, for reasons / diagnostics.
    """

    score: float
    binary_pass: bool
    gate_failed: bool
    failed_required_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 1. Schema generation — submit_report tool derived from the rubric
# ---------------------------------------------------------------------------


def _criterion_field_description(criterion: Criterion) -> str:
    """Inline the pass-condition (and expected reference) for the judge."""
    parts = [criterion.description]
    if criterion.expected is not None:
        parts.append(f"Expected: {criterion.expected}")
    return " ".join(parts)


def _criterion_verdict_property(criterion: Criterion) -> dict:
    """The per-criterion verdict field: ``met`` (binary) or ``score`` (graded)."""
    inline = _criterion_field_description(criterion)
    if criterion.kind == "binary":
        return {
            "type": "boolean",
            "description": f"Whether this criterion is met: {inline}",
        }
    return {
        "type": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "description": f"Score in [0,1] for this criterion: {inline}",
    }


def build_submit_report_tool(rubric: Rubric) -> dict:
    """Generate the terminal ``submit_report`` tool whose args derive from the rubric.

    The tool follows the project's OpenAI function-calling shape
    (``{"type": "function", "function": {...}}``) — the same shape every other
    judge read-tool (``judge_tools.py``) uses, so the LLM layer accepts it directly.

    Argument schema (all fields required):

    - one verdict field keyed by ``criterion.id`` — ``met`` (boolean) for a
      ``binary`` criterion, ``score`` (number in ``[0,1]``) for a ``graded`` one;
    - one ``<criterion.id>_justification`` string per criterion;
    - one overall ``reasons`` string.

    Each verdict field embeds the criterion's ``description`` (and ``expected``,
    when set) inline so the judge has the pass-condition without extra context.

    Relies on the construction-time guarantees from ``Rubric`` (see
    ``Rubric._validate_criterion_ids``): criterion ids are unique, identifier-safe,
    and never collide with the reserved ``reasons`` key or any derived
    ``<id>_justification`` key — so this builder need not re-check collisions.
    The reserved-key contract (``_REASONS_KEY`` / ``_JUSTIFICATION_SUFFIX``) is
    duplicated in that validator; keep the two in sync.
    """
    properties: dict[str, dict] = {}
    required: list[str] = []

    for criterion in rubric.criteria:
        properties[criterion.id] = _criterion_verdict_property(criterion)
        justification_key = f"{criterion.id}{_JUSTIFICATION_SUFFIX}"
        properties[justification_key] = {
            "type": "string",
            "description": f"Evidence-based justification for criterion '{criterion.id}'.",
        }
        required.append(criterion.id)
        required.append(justification_key)

    properties[_REASONS_KEY] = {
        "type": "string",
        "description": "Overall summary of the evaluation across all criteria.",
    }
    required.append(_REASONS_KEY)

    return {
        "type": "function",
        "function": {
            "name": SUBMIT_REPORT_TOOL_NAME,
            "description": (
                "Submit your final per-criterion rubric verdict. Provide a verdict "
                "and a justification for every criterion, plus overall reasons. "
                "Call this exactly once when you have finished evaluating."
            ),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ---------------------------------------------------------------------------
# 2. Validation — judge tool-call args → list[CriterionResult] (fail loud)
# ---------------------------------------------------------------------------


def _require_present(tool_args: dict, key: str, criterion_id: str, label: str) -> object:
    if key not in tool_args:
        raise SubmitReportValidationError(
            f"submit_report is missing the {label} field '{key}' for criterion '{criterion_id}'."
        )
    return tool_args[key]


def _coerce_binary_met(value: object, criterion_id: str) -> bool:
    if not isinstance(value, bool):
        raise SubmitReportValidationError(
            f"Criterion '{criterion_id}' is binary and expects 'met' to be a "
            f"boolean, got {type(value).__name__}: {value!r}."
        )
    return value


def _coerce_graded_score(value: object, criterion_id: str) -> float:
    # bool is a subclass of int — reject it explicitly so a True/False can't pass
    # as a 1.0/0.0 score for a graded criterion.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SubmitReportValidationError(
            f"Criterion '{criterion_id}' is graded and expects 'score' to be a "
            f"number, got {type(value).__name__}: {value!r}."
        )
    score = float(value)
    if not (0.0 <= score <= 1.0):
        raise SubmitReportValidationError(
            f"Criterion '{criterion_id}' score {score} is out of range; it must be "
            f"within [0.0, 1.0]."
        )
    return score


def _criterion_result(criterion: Criterion, tool_args: dict) -> CriterionResult:
    """Build one CriterionResult, failing loud on type / range mismatch."""
    raw_verdict = _require_present(tool_args, criterion.id, criterion.id, "verdict")
    justification_key = f"{criterion.id}{_JUSTIFICATION_SUFFIX}"
    raw_justification = _require_present(
        tool_args, justification_key, criterion.id, "justification"
    )
    if not isinstance(raw_justification, str):
        raise SubmitReportValidationError(
            f"Criterion '{criterion.id}' justification must be a string, got "
            f"{type(raw_justification).__name__}."
        )

    if criterion.kind == "binary":
        met = _coerce_binary_met(raw_verdict, criterion.id)
        score = 1.0 if met else 0.0
    else:
        score = _coerce_graded_score(raw_verdict, criterion.id)
        met = score >= GRADED_MET_THRESHOLD

    return CriterionResult(id=criterion.id, met=met, score=score, justification=raw_justification)


def parse_submit_report(tool_args: dict, rubric: Rubric) -> list[CriterionResult]:
    """Validate the judge's ``submit_report`` args against the rubric.

    Returns one :class:`CriterionResult` per rubric criterion, in rubric order.
    Derivation rules:

    - **binary** → ``score = 1.0 if met else 0.0``; ``met`` taken as given.
    - **graded** → ``score`` taken as given (validated within ``[0, 1]``);
      ``met = score >= GRADED_MET_THRESHOLD`` (0.5), used only for the
      required-gate in :func:`aggregate_rubric`.

    Raises :class:`SubmitReportValidationError` (fail loud, never coerce) on:
    missing criterion verdict / justification, unknown / extra criterion id,
    wrong field type, or ``score`` out of ``[0, 1]``.
    """
    known_ids = {c.id for c in rubric.criteria}

    results = [_criterion_result(criterion, tool_args) for criterion in rubric.criteria]

    expected_keys = known_ids | {f"{cid}{_JUSTIFICATION_SUFFIX}" for cid in known_ids}
    expected_keys.add(_REASONS_KEY)
    extra = set(tool_args) - expected_keys
    if extra:
        raise SubmitReportValidationError(
            f"submit_report contains unknown field(s) not in the rubric: "
            f"{sorted(extra)}. Allowed criterion ids: {sorted(known_ids)}."
        )

    return results


# ---------------------------------------------------------------------------
# 3. Aggregation — required-gate + weighted average
# ---------------------------------------------------------------------------


def aggregate_rubric(rubric: Rubric, results: list[CriterionResult]) -> RubricAggregate:
    """Aggregate per-criterion results into the ``llm_judge`` component verdict.

    Semantics (from ``docs/RUBRIC_GRADING_DESIGN.md``):

    - **Required gate:** if ANY criterion with ``required=True`` has ``met=False``,
      the rubric fails outright (``gate_failed=True`` / ``binary_pass=False``),
      regardless of the weighted average.
    - **Score:** weighted average ``Σ(weight·score) / Σ(weight)`` over the
      NON-required criteria only. Required criteria are PURE GATES — they are not
      weighted contributors. When all criteria are required (no non-required
      criteria), ``score`` is ``1.0`` if the gate passes, else ``0.0``.

    Raises :class:`SubmitReportValidationError` if ``results`` does not line up
    one-to-one with the rubric's criteria, or if there are non-required criteria
    but their total weight is not positive (defensive — Stage 4 always feeds the
    output of :func:`parse_submit_report`).
    """
    by_id = {r.id: r for r in results}
    if by_id.keys() != {c.id for c in rubric.criteria} or len(by_id) != len(results):
        raise SubmitReportValidationError(
            "Results do not match the rubric's criteria one-to-one; cannot "
            "aggregate. Pass the output of parse_submit_report unchanged."
        )

    failed_required_ids = tuple(c.id for c in rubric.criteria if c.required and not by_id[c.id].met)
    gate_failed = bool(failed_required_ids)

    # Required criteria are PURE GATES — they decide pass/fail via the gate, and
    # are EXCLUDED from the weighted average. The score is the weighted mean over
    # the non-required criteria only.
    non_required = [c for c in rubric.criteria if not c.required]
    if non_required:
        total_weight = sum(c.weight for c in non_required)
        if total_weight <= 0.0:
            raise SubmitReportValidationError(
                "Rubric non-required criterion weight must be positive to "
                f"aggregate; got {total_weight}."
            )
        weighted = sum(c.weight * by_id[c.id].score for c in non_required)
        score = weighted / total_weight
    else:
        # All criteria are required (pure gates) — there is nothing to average, so
        # the score collapses to the gate verdict.
        score = 0.0 if gate_failed else 1.0

    binary_pass = (not gate_failed) and score >= GRADED_MET_THRESHOLD

    return RubricAggregate(
        score=score,
        binary_pass=binary_pass,
        gate_failed=gate_failed,
        failed_required_ids=failed_required_ids,
    )
