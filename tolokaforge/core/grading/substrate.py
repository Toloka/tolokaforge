"""``GradingSubstrate`` — the topology-abstraction seam for grading.

One Protocol, one question: **how does the grader see the trial's state?**
Every deployment topology is an implementation of the same Protocol. The
component evaluators above the substrate (rubric, judge, transcript rules,
trace-check operators, state-check backends, custom-check executors) never
change when the topology changes.

See ADR-0039 for the design rationale + the recipes carried for the three
reserved future substrates.

The runtime picture
-------------------

+-------------------------------------+----------------------------------------+
| Deployment shape                    | Substrate implementation               |
+=====================================+========================================+
| Aggregate image (this milestone)    | :class:`InProcessGradingSubstrate`     |
| Independent grader (this milestone) | :class:`LiveRunnerCallbackGradingSub-  |
|                                     | strate`                                |
| Trajectory-storage service (future) | :class:`TrajectoryStorageGradingSub-   |
|                                     | strate` (recipe in ADR-0039)           |
| Snapshot-on-wire (future)           | :class:`SnapshotGradingSubstrate`      |
|                                     | (recipe in ADR-0039)                   |
| Shared-mount (future)               | :class:`SharedMountGradingSubstrate`   |
|                                     | (recipe in ADR-0039)                   |
+-------------------------------------+----------------------------------------+

Two impls ship today (in-process, live-callback). The remaining three are
declared but not implemented — each raises ``NotImplementedError`` with a
pointer to ADR-0039 so a downstream contributor who reaches for them sees
the recipe rather than a mystery stub.

Substrate implementations are themselves a plug-in group
(``tolokaforge.grading_substrates``): when the trajectory-storage service
ships, it registers a one-line entry point — no framework PR needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.kb_search import KnowledgeSearch

__all__ = [
    "GradingSubstrate",
    "InProcessGradingSubstrate",
    "LiveRunnerCallbackGradingSubstrate",
    "SharedMountGradingSubstrate",
    "SnapshotGradingSubstrate",
    "SubstrateUnreachableError",
    "TrajectoryStorageGradingSubstrate",
]


class SubstrateUnreachableError(Exception):
    """Raised when a substrate cannot reach its state source.

    The grader translates this into ``GradingFailedError`` at the seam, so
    the trial is booked as ungradeable rather than as an agent failure.
    Distinct from a grading verdict of ``None`` (nothing to grade) — this
    is "the grader tried and could not read the inputs it needed".
    """


@runtime_checkable
class GradingSubstrate(Protocol):
    """The state source every component evaluator reads from.

    Implementations vary by deployment topology; the caller of every
    evaluator does not need to know which. Implementations are constructed
    per-trial and are freed by :meth:`close` when grading ends.

    Every method may raise :class:`SubstrateUnreachableError` — the source
    is unreachable, the trial's state can no longer be produced, and the
    grader must fail loud. Empty / absent results are represented as empty
    containers or ``None``, never as an exception.
    """

    def db_reader(self) -> DBReader:
        """A synchronous read seam over the trial's final DB state.

        Same Protocol as :class:`~tolokaforge.core.grading.judge.DBReader`.
        Implementations bridge to whatever transport the topology uses
        (direct in-process call, remote gRPC, snapshot dict, mount read).
        """
        ...

    def knowledge_search(self) -> KnowledgeSearch | None:
        """A per-trial :class:`~tolokaforge.core.grading.kb_search.KnowledgeSearch`
        pointed at the SAME index the agent's KB tool used, or ``None`` when
        the task declared no KB / the run did not provision one.

        ``None`` is a first-class answer — a trial without a KB is a common
        shape, not an error.
        """
        ...

    def filesystem_root(self) -> Path | None:
        """The root of the agent-visible filesystem, or ``None`` when the
        task carries no filesystem surface.

        Implementations that source the filesystem lazily (live-callback,
        future snapshot with a tarball unpacked to a tmpdir) return the
        root path; the caller walks it with normal filesystem APIs.
        """
        ...

    def initial_state(self) -> dict[str, Any]:
        """The trial's pre-execution state: ``{table_name: [row, ...]}``.

        The same shape the runner's ``TaskDescription.initial_state.tables``
        carries. Used by state-check hash grading and by custom checks that
        diff initial vs. final state.
        """
        ...

    def final_state(self) -> dict[str, Any]:
        """The trial's post-execution state: ``{table_name: [row, ...]}``.

        The whole final DB state read at trial end. Used by state-check
        jsonpath / hash grading, by custom checks, and by the judge's
        ``state_diff`` construction.
        """
        ...

    def close(self) -> None:
        """Release any transport / temp-directory resources this substrate
        owns. Called by the grader at the end of one grade call.

        Idempotent: double-close is a noop.
        """
        ...


# ---------------------------------------------------------------------------
# Shipped implementation 1 — InProcessGradingSubstrate
# ---------------------------------------------------------------------------


class InProcessGradingSubstrate:
    """The aggregate image / in-runner path: wraps live objects directly.

    Used by:

    - ``runner_rpc`` — the runner constructs this over its own in-memory
      DB / KB / workspace and hands it to the composite grading dispatch.
    - The aggregate Docker image — grader and runner share a Python
      process, so the same substrate serves both.

    No network hop, no serialisation. The reference impl for the
    ``in_process`` name in the ``tolokaforge.grading_substrates`` entry
    point group.
    """

    def __init__(
        self,
        db_reader: DBReader,
        knowledge_search: KnowledgeSearch | None,
        filesystem_root: Path | None,
        initial_state: dict[str, Any],
        final_state: dict[str, Any],
    ) -> None:
        self._db_reader = db_reader
        self._knowledge_search = knowledge_search
        self._filesystem_root = filesystem_root
        self._initial_state = initial_state
        self._final_state = final_state
        self._closed = False

    def db_reader(self) -> DBReader:
        return self._db_reader

    def knowledge_search(self) -> KnowledgeSearch | None:
        return self._knowledge_search

    def filesystem_root(self) -> Path | None:
        return self._filesystem_root

    def initial_state(self) -> dict[str, Any]:
        return self._initial_state

    def final_state(self) -> dict[str, Any]:
        return self._final_state

    def close(self) -> None:
        # Nothing to release — the caller owns the DB reader, KB search,
        # and workspace paths; the substrate is a thin view.
        self._closed = True


# ---------------------------------------------------------------------------
# Shipped implementation 2 — LiveRunnerCallbackGradingSubstrate (scaffold)
# ---------------------------------------------------------------------------
#
# NOTE: the class body below is scaffolded; the runner-side ``SubstrateService``
# gRPC service + the ``GrpcSubstrateClient`` that dials it land in the
# follow-up commits on this branch. Once those land, this class stops
# raising and becomes the reference impl for the ``live_callback`` name.
# Kept in this module today so ADR-0039's design is legible in one place.


class LiveRunnerCallbackGradingSubstrate:
    """The independent grader container path: dials the runner's read-only
    :class:`SubstrateService` on demand.

    Reads are lazy: each ``filesystem_root().read_file(path)`` becomes one
    gRPC call. Small wire per call; grader lifecycle tied to the runner
    being alive at grade time (the trade-off Inspect AI made too — see
    ADR-0039).

    A grader losing the runner mid-grade raises
    :class:`SubstrateUnreachableError`; the seam translates that into
    ``GradingFailedError`` so the trial is booked as ungradeable.
    """

    def __init__(self, runner_substrate_address: str, trial_id: str) -> None:
        self.runner_substrate_address = runner_substrate_address
        self.trial_id = trial_id
        self._closed = False

    def db_reader(self) -> DBReader:
        raise NotImplementedError(
            "LiveRunnerCallbackGradingSubstrate is not yet wired — the runner-side "
            "SubstrateService lands in a follow-up commit on this branch. Track "
            "milestone #36 / issue #1261. See ADR-0039."
        )

    def knowledge_search(self) -> KnowledgeSearch | None:
        raise NotImplementedError(
            "LiveRunnerCallbackGradingSubstrate is not yet wired — see #1261."
        )

    def filesystem_root(self) -> Path | None:
        raise NotImplementedError(
            "LiveRunnerCallbackGradingSubstrate is not yet wired — see #1261."
        )

    def initial_state(self) -> dict[str, Any]:
        raise NotImplementedError(
            "LiveRunnerCallbackGradingSubstrate is not yet wired — see #1261."
        )

    def final_state(self) -> dict[str, Any]:
        raise NotImplementedError(
            "LiveRunnerCallbackGradingSubstrate is not yet wired — see #1261."
        )

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Reserved future substrates — declared here so ADR-0039's design is
# discoverable in one module. All three raise NotImplementedError with a
# pointer to the ADR recipe.
# ---------------------------------------------------------------------------


class TrajectoryStorageGradingSubstrate:
    """Reserved: dials the Toloka trajectory-storage service (in development
    inside Toloka) that holds traces, environments, and per-trial state.

    Recipe in ADR-0039 § "Reserved future substrate — TrajectoryStorageGrading
    Substrate". Ships as a separate PR once the storage service is stable,
    coordinated with the trajectory-storage team. Registers under
    ``tolokaforge.grading_substrates`` as a one-line entry point — no
    framework change needed at that time.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        raise NotImplementedError(
            "TrajectoryStorageGradingSubstrate is a reserved future substrate. "
            "The recipe for wiring it is in docs/adr/0039-standalone-grader.md — "
            "the substrate ships as a separate PR once the trajectory-storage "
            "service is stable."
        )


class SnapshotGradingSubstrate:
    """Reserved: Harbor-pattern snapshot-on-wire. State travels inside
    ``GradeRequest``.

    Recipe in ADR-0039 § "Reserved future substrate — SnapshotGradingSubstrate
    (Harbor pattern)". Ship when offline replay / cross-region grading is a
    hard requirement (grader outlives the runner, or lives in a different
    network region). Filesystem cap policy + auto-fallback to live-callback
    is part of the recipe — the runner's ``_read_agent_visible_filesystem``
    already filters ``node_modules`` / ``.venv`` / ``.git``, so the wire
    payload is bounded for most tolokaforge tasks.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        raise NotImplementedError(
            "SnapshotGradingSubstrate is a reserved future substrate. The recipe "
            "for wiring it (GradeRequest v3, filesystem cap, auto-fallback to "
            "LiveRunnerCallback) is in docs/adr/0039-standalone-grader.md."
        )


class SharedMountGradingSubstrate:
    """Reserved: SWE-bench / METR-pattern shared filesystem/DB mount.

    Recipe in ADR-0039 § "Reserved future substrate — SharedMountGradingSubstrate
    (SWE-bench pattern)". Grader and runner run in sibling containers with
    a shared volume; grader reads what the runner wrote. Single-host
    constraint. Ship if a high-throughput single-host deployment wants a
    separate grader image without paying the wire hop.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        raise NotImplementedError(
            "SharedMountGradingSubstrate is a reserved future substrate. The "
            "recipe for wiring it (docker-compose mount, single-host constraint) "
            "is in docs/adr/0039-standalone-grader.md."
        )
