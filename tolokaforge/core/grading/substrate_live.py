"""``LiveRunnerCallbackGradingSubstrate`` — the independent-grader path.

Each read dials the runner's read-only :class:`SubstrateService` gRPC surface
via :class:`~tolokaforge.core.grading.substrate_client.GrpcSubstrateClient`;
any transport failure raises :class:`SubstrateUnreachableError` and the
composite dispatch translates that into ``GradingFailedError`` so the trial
is booked as ungradeable.

Layering — this module lives grader-side. The runner subset does not ship
it: the runner never instantiates a live-callback substrate (that is the
grader container's job), and the substrate's transitive dependency on
:mod:`~tolokaforge.core.grading.substrate_client` — which itself pulls in
the gRPC protobuf surface via ``runner_pb2`` — would double-ship those
files inside the slim image. See :mod:`tolokaforge.core._runner_subset`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import grpc

from tolokaforge.core.grading.substrate_client import GrpcSubstrateClient

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.kb_search import KnowledgeSearch, SearchHit

__all__ = ["LiveRunnerCallbackGradingSubstrate"]


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
        # Local import so ``substrate_live`` module import does not pay the
        # jsonpath library's import cost for callers that never reach for
        # query().
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
    on first use.

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

    def db_probe(self, dsn: str, query: str) -> list[dict[str, Any]]:
        return self._client.run_db_probe(dsn, query)

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
