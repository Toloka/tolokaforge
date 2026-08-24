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

Two impls ship today. :class:`InProcessGradingSubstrate` is the aggregate-image
/ in-runner path — a thin view over live objects the caller owns.
:class:`~tolokaforge.core.grading.substrate_live.LiveRunnerCallbackGradingSubstrate`
is the independent-grader path — each read dials the runner's read-only
:class:`SubstrateService` gRPC surface via
:class:`~tolokaforge.core.grading.substrate_client.GrpcSubstrateClient`; any
transport failure raises :class:`SubstrateUnreachableError` and the seam
translates that into ``GradingFailedError`` at the composite dispatch. It lives
in the sibling :mod:`~tolokaforge.core.grading.substrate_live` module because
the runner subset does not ship the gRPC client. The remaining three
implementations are declared but not implemented — each raises
``NotImplementedError`` with a pointer to ADR-0039 so a downstream contributor
who reaches for them sees the recipe rather than a mystery stub.

Two views of the trial's final DB state ride the Protocol side by side:
:meth:`GradingSubstrate.final_state` returns the **RAW** rows the judge's
state-diff and ``custom_checks`` need to see every stored field, and
:meth:`GradingSubstrate.final_state_stable` returns the **STABLE** rows
jsonpath grading reads (unstable fields filtered server-side, so a
run-scoped ``session_token`` never decides an author's assertion). Three
call sites, two DB reads: the Protocol carries both accessors so the
composite can pick the right one per component without a substrate having
to guess.

Substrate implementations are themselves a plug-in group
(``tolokaforge.grading_substrates``): when the trajectory-storage service
ships, it registers a one-line entry point — no framework PR needed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.kb_search import KnowledgeSearch

__all__ = [
    "GradingSubstrate",
    "InProcessGradingSubstrate",
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
        """The trial's post-execution state, **RAW**: ``{table_name: [row, ...]}``.

        The whole final DB state read at trial end without server-side filtering
        — the shape the judge's ``state_diff`` construction and ``custom_checks``
        consume, both of which need every stored field (including timestamps and
        opaque tokens the DB service marks unstable) to compute a faithful diff.

        Jsonpath grading reads a different view: unstable fields are filtered
        server-side by the DB service so a run-scoped ``session_token`` cannot
        drag every trial's assertion off. That view is returned by
        :meth:`final_state_stable`; both accessors return the same
        ``{table: [rows]}`` shape and only the row *contents* differ.
        """
        ...

    def final_state_stable(self) -> dict[str, Any]:
        """The trial's post-execution state, **STABLE**: ``{table_name: [row, ...]}``.

        The same shape as :meth:`final_state` but with unstable fields filtered
        out server-side by the DB service. The view jsonpath grading assertions
        resolve against, so a per-run ``session_token`` does not decide an
        author's ``$.db.users[0].session_token == 'S-1'``.

        Implementations that source state lazily are free to memoise this
        accessor separately from :meth:`final_state`; they are two DB reads.
        """
        ...

    def filesystem_state(self) -> dict[str, str] | None:
        """The agent-visible filesystem as ``{'/env/fs/agent-visible/<rel>': text}``,
        or ``None`` when the trial has no workspace tree.

        The shape jsonpath grading resolves ``$.filesystem['/env/fs/agent-
        visible/<rel>']`` against — every non-symlink UTF-8-decodable file
        below the agent-visible root, keyed by its logical path. Binary
        files and symlinks are skipped, matching the runner's shipped
        ``_read_agent_visible_filesystem`` filter.

        ``None`` — first-class "the trial declared no filesystem surface" —
        is distinct from ``{}`` (a workspace root that exists but holds no
        readable files).
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


_MISSING: Any = object()


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

    Two shapes for :meth:`final_state`: pass ``final_state=`` to return a
    pre-fetched value verbatim, or ``final_state_factory=`` for a
    memoised-on-first-read lambda the composite can gate on. Both together
    is a construction-time ``ValueError`` — the two argument shapes are
    mutually exclusive. :meth:`final_state_stable` and
    :meth:`filesystem_state` are factory-only; the composite lazily invokes
    them at most once per grade call, and only when its own gates say
    the read is needed.
    """

    def __init__(
        self,
        *,
        db_reader: DBReader,
        knowledge_search: KnowledgeSearch | None,
        filesystem_root: Path | None,
        initial_state: dict[str, Any] | None = None,
        final_state: dict[str, Any] | None = None,
        final_state_factory: Callable[[], dict[str, Any]] | None = None,
        final_state_stable_factory: Callable[[], dict[str, Any]] | None = None,
        filesystem_state_factory: Callable[[], dict[str, str] | None] | None = None,
    ) -> None:
        if final_state is not None and final_state_factory is not None:
            raise ValueError(
                "InProcessGradingSubstrate: 'final_state' and 'final_state_factory' "
                "are mutually exclusive — pass one or the other, not both."
            )
        self._db_reader = db_reader
        self._knowledge_search = knowledge_search
        self._filesystem_root = filesystem_root
        self._initial_state = initial_state if initial_state is not None else {}
        self._final_state = final_state
        self._final_state_factory = final_state_factory
        self._final_state_stable_factory = final_state_stable_factory
        self._filesystem_state_factory = filesystem_state_factory
        self._final_state_stable_cache: dict[str, Any] | Any = _MISSING
        self._filesystem_state_cache: dict[str, str] | None | Any = _MISSING
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
        if self._final_state is None and self._final_state_factory is not None:
            self._final_state = self._final_state_factory()
        if self._final_state is None:
            raise RuntimeError(
                "InProcessGradingSubstrate.final_state() called but neither "
                "'final_state' nor 'final_state_factory' was supplied at "
                "construction."
            )
        return self._final_state

    def final_state_stable(self) -> dict[str, Any]:
        if self._final_state_stable_cache is _MISSING:
            if self._final_state_stable_factory is None:
                raise RuntimeError(
                    "InProcessGradingSubstrate.final_state_stable() called but "
                    "'final_state_stable_factory' was not supplied at "
                    "construction — STABLE reads require explicit wiring so a "
                    "caller reaching for the filtered view never silently gets "
                    "RAW rows."
                )
            self._final_state_stable_cache = self._final_state_stable_factory()
        return self._final_state_stable_cache

    def filesystem_state(self) -> dict[str, str] | None:
        if self._filesystem_state_cache is _MISSING:
            if self._filesystem_state_factory is None:
                self._filesystem_state_cache = None
            else:
                self._filesystem_state_cache = self._filesystem_state_factory()
        return self._filesystem_state_cache

    def close(self) -> None:
        # Nothing to release — the caller owns the DB reader, KB search,
        # and workspace paths; the substrate is a thin view.
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
