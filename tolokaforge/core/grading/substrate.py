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
:class:`LiveRunnerCallbackGradingSubstrate` is the independent-grader path —
each read dials the runner's read-only :class:`SubstrateService` gRPC surface
via :class:`~tolokaforge.core.grading.substrate_client.GrpcSubstrateClient`;
any transport failure raises :class:`SubstrateUnreachableError` and the seam
translates that into ``GradingFailedError`` at the composite dispatch. The
remaining three implementations are declared but not implemented — each raises
``NotImplementedError`` with a pointer to ADR-0039 so a downstream contributor
who reaches for them sees the recipe rather than a mystery stub.

Substrate implementations are themselves a plug-in group
(``tolokaforge.grading_substrates``): when the trajectory-storage service
ships, it registers a one-line entry point — no framework PR needed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import grpc

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.kb_search import KnowledgeSearch, SearchHit
    from tolokaforge.core.grading.substrate_client import GrpcSubstrateClient

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
# Shipped implementation 2 — LiveRunnerCallbackGradingSubstrate
# ---------------------------------------------------------------------------


_MISSING: Any = object()


class _GrpcDBReader:
    """Sync :class:`DBReader` view over the runner's ``ReadFinalDBState`` RPC.

    ``get_state`` returns the runner's RAW final DB tables in one call.
    ``query`` fetches all tables once and runs jsonpath locally against them —
    the substrate service exposes no server-side jsonpath endpoint, so the
    caller assembles the same ``{results: [...]}`` shape ``db_client.query``
    ships today on the client side.
    """

    def __init__(self, client: GrpcSubstrateClient) -> None:
        self._client = client
        self._all_tables_cache: dict[str, Any] | None = None

    def get_state(self, tables: list[str] | None = None) -> dict[str, Any]:
        return self._client.read_final_db_state(tables=tables)

    def query(self, jsonpath: str) -> dict[str, Any]:
        # Local import so ``substrate`` module import does not pay the jsonpath
        # library's import cost for callers that never reach for query().
        from jsonpath_ng import parse

        if self._all_tables_cache is None:
            self._all_tables_cache = self._client.read_final_db_state()
        expr = parse(jsonpath)
        return {"results": [match.value for match in expr.find(self._all_tables_cache)]}


class _GrpcKnowledgeSearch:
    """Sync :class:`KnowledgeSearch` view over the runner's ``KBSearch`` RPC."""

    def __init__(self, client: GrpcSubstrateClient) -> None:
        self._client = client

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        return self._client.kb_search(query, top_k, alpha).hits


class LiveRunnerCallbackGradingSubstrate:
    """The independent grader container path: dials the runner's read-only
    :class:`SubstrateService` on demand.

    Reads are lazy and cached: each accessor fires at most one RPC per grade
    call; a second call returns the cached value. ``filesystem_root`` eagerly
    materialises the agent-visible tree to a :class:`tempfile.TemporaryDirectory`
    on first use (Phase 3 revisits the eager materialisation cost for
    coding-harness workspaces — see ADR-0039).

    A grader losing the runner mid-grade raises
    :class:`SubstrateUnreachableError`; the seam translates that into
    ``GradingFailedError`` so the trial is booked as ungradeable.
    """

    def __init__(
        self,
        runner_substrate_address: str,
        trial_id: str,
        *,
        channel: grpc.Channel | None = None,
    ) -> None:
        # Local import breaks the module-load cycle
        # (substrate_client imports SubstrateUnreachableError from this module).
        from tolokaforge.core.grading.substrate_client import GrpcSubstrateClient

        self.runner_substrate_address = runner_substrate_address
        self.trial_id = trial_id
        if channel is None:
            self._channel: grpc.Channel = grpc.insecure_channel(runner_substrate_address)
            self._owns_channel = True
        else:
            self._channel = channel
            self._owns_channel = False
        self._client = GrpcSubstrateClient(self._channel, trial_id)
        self._initial_state_cache: dict[str, Any] | Any = _MISSING
        self._final_state_cache: dict[str, Any] | Any = _MISSING
        self._final_state_stable_cache: dict[str, Any] | Any = _MISSING
        self._filesystem_state_cache: dict[str, str] | None | Any = _MISSING
        self._filesystem_root_cache: Path | None | Any = _MISSING
        self._filesystem_tmpdir: tempfile.TemporaryDirectory | None = None
        self._kb_available: bool | None = None
        self._db_reader_cache: _GrpcDBReader | None = None
        self._kb_search_cache: _GrpcKnowledgeSearch | None = None
        self._closed = False

    def db_reader(self) -> DBReader:
        if self._db_reader_cache is None:
            self._db_reader_cache = _GrpcDBReader(self._client)
        return self._db_reader_cache

    def knowledge_search(self) -> KnowledgeSearch | None:
        if self._kb_available is None:
            probe = self._client.kb_search(query="", top_k=0, alpha=0.0)
            self._kb_available = probe.kb_available
        if not self._kb_available:
            return None
        if self._kb_search_cache is None:
            self._kb_search_cache = _GrpcKnowledgeSearch(self._client)
        return self._kb_search_cache

    def initial_state(self) -> dict[str, Any]:
        if self._initial_state_cache is _MISSING:
            self._initial_state_cache = self._client.read_initial_state()
        return self._initial_state_cache

    def final_state(self) -> dict[str, Any]:
        if self._final_state_cache is _MISSING:
            self._final_state_cache = self._client.read_final_db_state()
        return self._final_state_cache

    def final_state_stable(self) -> dict[str, Any]:
        if self._final_state_stable_cache is _MISSING:
            self._final_state_stable_cache = self._client.read_final_db_state_stable()
        return self._final_state_stable_cache

    def filesystem_state(self) -> dict[str, str] | None:
        if self._filesystem_state_cache is _MISSING:
            self._filesystem_state_cache = self._read_filesystem_state()
        return self._filesystem_state_cache

    def filesystem_root(self) -> Path | None:
        if self._filesystem_root_cache is _MISSING:
            self._filesystem_root_cache = self._materialise_filesystem_root()
        return self._filesystem_root_cache

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._filesystem_tmpdir is not None:
            self._filesystem_tmpdir.cleanup()
            self._filesystem_tmpdir = None
        if self._owns_channel:
            self._channel.close()

    def _read_filesystem_state(self) -> dict[str, str] | None:
        rel_paths = self._client.list_filesystem_dir()
        if not rel_paths and not self._workspace_root_exists():
            return None
        fs: dict[str, str] = {}
        for rel in rel_paths:
            entry = self._client.read_filesystem_path(rel)
            if entry.is_file:
                fs[f"/env/fs/agent-visible/{rel}"] = entry.content_utf8
        return fs

    def _materialise_filesystem_root(self) -> Path | None:
        rel_paths = self._client.list_filesystem_dir()
        if not rel_paths and not self._workspace_root_exists():
            return None
        self._filesystem_tmpdir = tempfile.TemporaryDirectory(prefix="grader-workspace-")
        root = Path(self._filesystem_tmpdir.name)
        for rel in rel_paths:
            entry = self._client.read_filesystem_path(rel)
            if not entry.is_file:
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if entry.content_bytes:
                dest.write_bytes(entry.content_bytes)
            else:
                dest.write_text(entry.content_utf8, encoding="utf-8")
        return root

    def _workspace_root_exists(self) -> bool:
        return self._client.read_filesystem_path("").exists


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
