"""Runner-wire helpers around GradeTrial's state-diff family.

Three responsibilities live here — all runner-wire adjacent, all consumed
by ``runner.service`` on the GradeTrial path:

- :func:`compute_state_diff` — human-readable diff between the trial's final
  stable state and the golden state, re-exported from
  :mod:`tolokaforge.core.grading.trial_golden_diff`, where core grading reaches
  it too.
- :func:`project_state_checks_to_runner_wire` — encode the composite
  fold's neutral ``None``-means-not-evaluated ``state_checks`` slot into
  the runner wire's ``-1.0`` sentinel.
- :func:`project_check_result_to_runner_wire` — encode a composite-produced
  :class:`~tolokaforge.core.grading.checks_interface.CheckResult` into the
  wire ``pb2.CustomCheckResult`` for ``Grade.custom_checks``.

See docs/GRPC_PROTOCOL.md for the grading algorithm specification. The
JSONPath assertion evaluators and the SQL-probe evaluator live in
:mod:`tolokaforge.core.grading.jsonpath_evaluators` and
:mod:`tolokaforge.core.grading.db_probes` — pure grading libraries the
runner-side GradeTrial and the standalone Grader v3 service both drive.
"""

import json
import logging

from tolokaforge.core.grading.checks_interface import CheckResult
from tolokaforge.core.grading.trial_golden_diff import compute_state_diff
from tolokaforge.core.models.grade import Grade
from tolokaforge.runner import runner_pb2 as pb2

__all__ = [
    "compute_state_diff",
    "grade_to_runner_wire",
    "project_check_result_to_runner_wire",
    "project_state_checks_to_runner_wire",
]

logger = logging.getLogger(__name__)


def project_state_checks_to_runner_wire(slot_component: float | None) -> float:
    """Encode the fold's ``state_checks`` slot into the runner wire's sentinel.

    The composite fold reports ``None`` where the ``state_checks`` block was
    not evaluated; the runner's ``pb2.GradeComponents`` field is a plain
    ``float`` and reserves ``-1.0`` as the not-evaluated sentinel. Kept
    beside the runner's other wire helpers so the pure fold module owes
    nothing to ``pb2``.
    """
    return -1.0 if slot_component is None else slot_component


def project_check_result_to_runner_wire(result: CheckResult) -> pb2.CustomCheckResult:
    """Encode a :class:`CheckResult` to the wire ``pb2.CustomCheckResult``.

    ``details`` is JSON-encoded into the proto's ``details_json`` string —
    empty when the check emitted no details. The status is projected via the
    enum's ``.value`` (or ``str(...)`` for a plain-string status), so the
    ``pb2.CustomCheckResult.status`` field always carries the lowercased
    literal (``passed`` / ``failed`` / ``skipped`` / ``error``) the wire
    contract pins. Kept beside :func:`project_state_checks_to_runner_wire` —
    every runner wire encoder scoped to a composite output lives here so
    the composite package owes nothing to ``pb2``.
    """
    status_str = result.status.value if hasattr(result.status, "value") else str(result.status)
    details_json = json.dumps(result.details) if result.details else ""
    return pb2.CustomCheckResult(
        check_name=result.check_name,
        status=status_str,
        score=result.score,
        message=result.message,
        details_json=details_json,
    )


def grade_to_runner_wire(grade: Grade) -> pb2.Grade:
    """Encode a Pydantic :class:`Grade` to the wire ``pb2.Grade``.

    Component scores are encoded with the runner-wire sentinel: ``None`` (tier
    did not run) → ``-1.0`` for the four scalar components; ``trace_checks``
    stays ``None`` when the tier did not run (proto3 optional presence), else
    the scored value or ``-1.0`` when explicitly set to "not evaluated".

    ``state_diff`` is JSON-encoded into ``state_diff_json`` — empty string
    when absent. ``custom_checks_details`` maps to the wire's ``custom_checks``
    repeated field; each entry's ``details`` dict is JSON-encoded into
    ``details_json`` — empty when the detail carried no dict.

    ``reasons`` is projected as a string: a dict-form (per-criterion reasons)
    is JSON-encoded so the wire's string field always carries a scalar.
    """
    state_checks_wire = project_state_checks_to_runner_wire(grade.components.state_checks)
    transcript_rules_wire = (
        -1.0 if grade.components.transcript_rules is None else grade.components.transcript_rules
    )
    llm_judge_wire = -1.0 if grade.components.llm_judge is None else grade.components.llm_judge
    custom_checks_wire = (
        -1.0 if grade.components.custom_checks is None else grade.components.custom_checks
    )
    components_kwargs: dict[str, float] = {
        "state_checks": state_checks_wire,
        "transcript_rules": transcript_rules_wire,
        "llm_judge": llm_judge_wire,
        "custom_checks": custom_checks_wire,
    }
    if grade.components.trace_checks is not None:
        components_kwargs["trace_checks"] = grade.components.trace_checks

    custom_check_wire: list[pb2.CustomCheckResult] = []
    for detail in grade.custom_checks_details or ():
        details_json = json.dumps(detail.details) if detail.details else ""
        custom_check_wire.append(
            pb2.CustomCheckResult(
                check_name=detail.check_name,
                status=detail.status,
                score=detail.score,
                message=detail.message,
                details_json=details_json,
            )
        )

    reasons_wire = grade.reasons if isinstance(grade.reasons, str) else json.dumps(grade.reasons)
    state_diff_json = json.dumps(grade.state_diff) if grade.state_diff else ""

    return pb2.Grade(
        binary_pass=grade.binary_pass,
        score=grade.score,
        components=pb2.GradeComponents(**components_kwargs),
        reasons=reasons_wire,
        state_diff_json=state_diff_json,
        custom_checks=custom_check_wire,
    )
