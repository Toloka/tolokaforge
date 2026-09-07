"""State-checks composite dispatch.

Every deployment shape's ``jsonpaths`` + ``db_probes`` scoring goes through
:func:`grade_state_checks_reads`. The composite forwards ``substrate``
verbatim and each resolved
:class:`~tolokaforge.core.grading.state_check_backend.StateCheckBackend`
consumes only the accessors it needs — dispatching without knowing either
evaluator's internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tolokaforge.core.grading.key_manifest import EVALUATED
from tolokaforge.runner.grading_ledger import DB_PROBES_KEY, JSONPATHS_KEY

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tolokaforge.core.grading.state_check_backend import StateCheckBackend
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import KeyAccountingRecord
    from tolokaforge.runner.models import RunnerStateChecksConfig


@dataclass(frozen=True)
class StateChecksReadResult:
    """The scored slots :func:`grade_state_checks_reads` returns.

    ``jsonpath_score`` / ``db_probe_score`` are ``None`` when the composite
    did not reach an assertion for that half — an empty checks list, or a
    probe-less pack — and the runner leaves the corresponding
    :class:`RunnerGradeComponents` slot untouched (which then folds as
    'component not evaluated'). Every author key the composite reached is
    added to ``accounted_keys`` so the caller can merge it into the
    RPC-level ledger.
    """

    jsonpath_score: float | None
    jsonpath_reasons: str | None
    db_probe_score: float | None
    db_probe_reasons: str | None
    accounted_keys: dict[str, KeyAccountingRecord] = field(default_factory=dict)


def grade_state_checks_reads(
    *,
    trial_id: str,
    config: RunnerStateChecksConfig,
    substrate: GradingSubstrate,
    state_check_backends: Mapping[str, StateCheckBackend],
    logger: StructuredLogger,
) -> StateChecksReadResult:
    """Score the pack's ``jsonpaths`` and ``db_probes`` against ``substrate``.

    ``state_check_backends`` is a name → resolved
    :class:`~tolokaforge.core.grading.state_check_backend.StateCheckBackend`
    mapping the runner resolves at startup — the shipping runner supplies
    ``{"jsonpath": JsonpathStateCheckBackend(), "db_probes":
    DbProbesStateCheckBackend()}``. Each backend owns its source's read
    strategy (which substrate accessors it consumes, how it handles absent
    state, whether it opens its own connections) so the composite dispatches
    without knowing either evaluator's internals. A downstream package
    registering a third source under the ``tolokaforge.state_check_backends``
    entry-point group (say, ``s3_diff``) becomes reachable here without a
    framework change.

    Substrate-independent: the composite forwards ``substrate`` verbatim and
    the resolved backend consumes only the accessors it needs. Hash grading
    is NOT part of this seam — its snapshot-and-replay semantics need write
    access to the trial's DB and stay runner-integrated on
    :meth:`RunnerServiceImpl._execute_hash_grading` (see the seam module
    docstring and ``docs/GRADER_SERVICE.md`` § "Sub-component plug-in seams").

    Sync-in-async note: the composite is a **sync** function. The InProcess
    substrate's factories block on ``run_coroutine_threadsafe`` to bridge to
    the runner's dedicated event-loop thread, which deadlocks when called
    from that loop. The runner therefore dispatches this function via
    ``loop.run_in_executor(None, ...)`` — matching the shipped
    ``_grade_llm_judge`` bridge — so the substrate's blocking reads and the
    ``db_probes`` backend's :func:`asyncio.run` land off the loop thread.
    """
    accounted: dict[str, KeyAccountingRecord] = {}

    jsonpath_checks = config.jsonpath_checks or []
    jsonpath_score: float | None = None
    jsonpath_reasons: str | None = None
    if jsonpath_checks:
        logger.info(f"GradeTrial: {trial_id} - Evaluating {len(jsonpath_checks)} jsonpath checks")
        jsonpath_score, jsonpath_reasons = state_check_backends["jsonpath"].query(
            expression=jsonpath_checks,
            substrate=substrate,
            trial_id=trial_id,
        )
        accounted[JSONPATHS_KEY] = EVALUATED
        if jsonpath_score is not None:
            logger.info(f"GradeTrial: {trial_id} - Jsonpath checks: score={jsonpath_score:.2f}")

    db_probe_score: float | None = None
    db_probe_reasons: str | None = None
    if config.db_probes:
        logger.info(f"GradeTrial: {trial_id} - Evaluating {len(config.db_probes)} db probes")
        probes = [probe.model_dump() for probe in config.db_probes]
        db_probe_score, db_probe_reasons = state_check_backends["db_probes"].query(
            expression=probes,
            substrate=substrate,
            trial_id=trial_id,
        )
        accounted[DB_PROBES_KEY] = EVALUATED
        if db_probe_score is not None:
            logger.info(f"GradeTrial: {trial_id} - DB probes: score={db_probe_score:.2f}")

    return StateChecksReadResult(
        jsonpath_score=jsonpath_score,
        jsonpath_reasons=jsonpath_reasons,
        db_probe_score=db_probe_score,
        db_probe_reasons=db_probe_reasons,
        accounted_keys=accounted,
    )
