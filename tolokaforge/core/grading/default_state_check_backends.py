"""Reference impls of :class:`StateCheckBackend` — ``jsonpath`` + ``db_probes``.

Registered under those two names in the ``tolokaforge.state_check_backends``
entry-point group. Each wraps the corresponding evaluator in
:mod:`tolokaforge.core.grading.jsonpath_evaluators` /
:mod:`tolokaforge.core.grading.db_probes` and reshapes / bridges around it
so :func:`~tolokaforge.core.grading.composite.grade_state_checks_reads` can
dispatch through the seam without importing either evaluator.

Hash grading is deliberately NOT a registered backend. Its snapshot-and-replay
semantics need write access to the trial's DB — the read-only substrate
cannot serve them — so hash grading stays runner-integrated on
:meth:`~tolokaforge.runner.service.RunnerServiceImpl._execute_hash_grading`,
called from :meth:`_grade_trial_async` above the composite dispatch.

This module holds both concrete impls of :class:`StateCheckBackend` in the
shipping distribution, so the ``.importlinter`` contract can forbid
composite from importing it without also forbidding the Protocol module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.db_probes import evaluate_db_probes
from tolokaforge.core.grading.jsonpath_addressing import addresses_the_database
from tolokaforge.core.grading.jsonpath_evaluators import evaluate_jsonpath_checks
from tolokaforge.core.grading.state_check_backend import StateCheckBackend
from tolokaforge.runner.db_client import TrialNotFoundError as DBTrialNotFoundError

if TYPE_CHECKING:
    from tolokaforge.core.grading.substrate import GradingSubstrate

__all__ = [
    "DbProbesStateCheckBackend",
    "JsonpathStateCheckBackend",
]

# The absent-trial degradation logs the shipped wording downstream tooling
# (ops greps, canonical unit locks) reads. Kept on a module-level logger
# rather than a per-call handle so the seam signature stays sync + evidence-only.
_logger = logging.getLogger(__name__)


class JsonpathStateCheckBackend:
    """Score ``jsonpath_checks`` against the substrate's STABLE DB view + filesystem.

    Reshapes :meth:`substrate.final_state_stable` (the STABLE view jsonpath
    grading resolves against, so a run-scoped ``session_token`` never drags
    an author's ``$.db.users[0].session_token == 'S-1'``) and
    :meth:`substrate.filesystem_state` into the ``{db, tables, filesystem}``
    shape :func:`~tolokaforge.core.grading.jsonpath_evaluators.evaluate_jsonpath_checks`
    addresses. Gates each read on the expression: a path-glob-only pack fetches
    nothing; a DB-addressing pack fetches only STABLE; a filesystem-only-``path:``
    pack fetches only the workspace walk.

    A :class:`DBTrialNotFoundError` from the STABLE read is graceful
    degradation — filesystem-only tasks never call ``db_client.init_trial()``,
    so an absent DB is the expected shape for them, and DB-declared tasks
    whose ``$.db.*`` assertions cannot match still get the per-assertion
    "Path not found" diagnosis from
    :func:`~tolokaforge.core.grading.jsonpath_evaluators.evaluate_jsonpath_checks`
    rather than a blanket refusal.
    """

    def query(
        self,
        *,
        expression: list[dict[str, Any]],
        substrate: GradingSubstrate,
        trial_id: str | None = None,
    ) -> tuple[float | None, str | None]:
        if not expression:
            return None, None
        path_checks = [check for check in expression if check.get("path") is not None]
        state_dict_needed = bool(path_checks)
        db_state_needed = any(addresses_the_database(check) for check in path_checks)
        fs_state_needed = any(not addresses_the_database(check) for check in path_checks)
        jsonpath_state: dict[str, Any] | None = None
        if state_dict_needed:
            db_state: dict[str, Any] = {}
            if db_state_needed:
                try:
                    db_state = substrate.final_state_stable()
                except DBTrialNotFoundError:
                    # Filesystem-only tasks never call db_client.init_trial(),
                    # so an absent DB is the expected shape. For tasks that
                    # DID declare a DB this same branch fires and downstream
                    # ``$.db.*`` assertions surface as "Path not found" — log
                    # at warn so ops see the real cause rather than debugging
                    # per-assertion failures.
                    _logger.warning(
                        f"GradeTrial: {trial_id} - DB trial not found; grading with empty DB state"
                    )
            fs_state = substrate.filesystem_state() if fs_state_needed else None
            jsonpath_state = {
                "db": db_state,
                "tables": db_state,
                "filesystem": fs_state or {},
            }
        return evaluate_jsonpath_checks(expression, state=jsonpath_state)


class DbProbesStateCheckBackend:
    """Score ``db_probes`` by opening task-declared postgres connections.

    Each probe carries its own ``dsn`` and hits its task's postgres directly
    via :func:`~tolokaforge.core.grading.db_probes.evaluate_db_probes`; the
    substrate does not intermediate — a probe DSN resolves only inside the
    task's docker network, which is the connection the probe itself opens.
    :meth:`query` is sync so the seam matches the sync composite dispatch;
    the async evaluator runs under an ephemeral :func:`asyncio.run` on the
    executor thread the runner already parks the composite on via
    ``loop.run_in_executor(None, ...)``.
    """

    def query(
        self,
        *,
        expression: list[dict[str, Any]],
        substrate: GradingSubstrate,
        trial_id: str | None = None,
    ) -> tuple[float | None, str | None]:
        if not expression:
            return None, None
        return asyncio.run(evaluate_db_probes(expression))


def _jsonpath_state_check_backend_factory() -> StateCheckBackend:
    """Entry-point factory. Arg-less; returns a fresh backend instance."""
    return JsonpathStateCheckBackend()


def _db_probes_state_check_backend_factory() -> StateCheckBackend:
    """Entry-point factory. Arg-less; returns a fresh backend instance."""
    return DbProbesStateCheckBackend()
