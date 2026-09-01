"""``GrpcSubstrateClient`` — the wire adapter for :class:`SubstrateService`.

One instance per trial, bound to a :class:`grpc.Channel` the caller owns (or
opened by :class:`LiveRunnerCallbackGradingSubstrate`). Every method issues one
unary RPC and translates:

* ``grpc.RpcError`` → :class:`SubstrateUnreachableError` — the runner is gone /
  unreachable; the seam translates that into ``GradingFailedError`` at the
  composite dispatch.
* ``ReadStateResponse.trial_not_found`` → :class:`DBTrialNotFoundError` — the
  DB service has no rows for this trial; the composite catches this at
  :func:`~tolokaforge.core.grading.composite.grade_state_checks_reads` and
  degrades to an empty DB view so filesystem-only tasks still grade.

Layering — this module lives at ``tolokaforge/core/grading/`` and imports one
symbol (:class:`~tolokaforge.runner.db_client.TrialNotFoundError`) from the
runner package. Composite/substrate → runner is the same one-way exception
already accepted for the composite (see ADR-0040); the runner is the sole
owner of the DB-service error hierarchy the substrate mirrors on the wire.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import grpc

from tolokaforge.core.grading.kb_search import SearchHit
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import runner_pb2_grpc as pb2_grpc
from tolokaforge.runner.db_client import TrialNotFoundError as DBTrialNotFoundError


@dataclass(frozen=True)
class FilesystemEntry:
    """One :func:`GrpcSubstrateClient.read_filesystem_path` response.

    ``content_utf8`` and ``content_bytes`` are mutually exclusive — the servicer
    populates one branch or the other per the same UTF-8-decode filter
    :func:`~tolokaforge.core.grading.filesystem_view.read_agent_visible_filesystem`
    ships. A missing / symlink / non-file target yields ``exists=False`` with
    both branches empty.
    """

    exists: bool
    is_file: bool = False
    is_dir: bool = False
    content_utf8: str = ""
    content_bytes: bytes = b""


@dataclass(frozen=True)
class KBSearchResult:
    """One :func:`GrpcSubstrateClient.kb_search` response.

    ``kb_available=False`` is the first-class "this trial has no KB" signal
    matching the :class:`GradingSubstrate` Protocol's ``knowledge_search() ->
    None`` contract; the callback substrate returns ``None`` from
    :meth:`knowledge_search` when this bool is false rather than dialing an
    unreachable endpoint.
    """

    kb_available: bool
    hits: list[SearchHit]


class GrpcSubstrateClient:
    """Read-only client for the runner's :class:`SubstrateService`.

    Bound to one ``(channel, trial_id)`` pair; every RPC passes the bound
    ``trial_id`` and returns a plain-Python value shaped to the same semantics
    the InProcess substrate would return. The client owns no state beyond the
    stub and the trial id.
    """

    def __init__(self, channel: grpc.Channel, trial_id: str) -> None:
        self._trial_id = trial_id
        self._stub = pb2_grpc.SubstrateServiceStub(channel)

    def read_initial_state(self) -> dict[str, Any]:
        try:
            response = self._stub.ReadInitialState(
                pb2.ReadInitialStateRequest(trial_id=self._trial_id)
            )
        except grpc.RpcError as err:
            raise SubstrateUnreachableError(str(err)) from err
        return self._decode_state(response.state_json)

    def read_final_db_state(self, tables: list[str] | None = None) -> dict[str, Any]:
        try:
            response = self._stub.ReadFinalDBState(
                pb2.ReadFinalDBStateRequest(
                    trial_id=self._trial_id,
                    tables=list(tables) if tables else [],
                )
            )
        except grpc.RpcError as err:
            raise SubstrateUnreachableError(str(err)) from err
        if response.trial_not_found:
            raise DBTrialNotFoundError(self._trial_id)
        return self._decode_state(response.state_json)

    def read_final_db_state_stable(self) -> dict[str, Any]:
        try:
            response = self._stub.ReadFinalDBStateStable(
                pb2.ReadFinalDBStateStableRequest(trial_id=self._trial_id)
            )
        except grpc.RpcError as err:
            raise SubstrateUnreachableError(str(err)) from err
        if response.trial_not_found:
            raise DBTrialNotFoundError(self._trial_id)
        return self._decode_state(response.state_json)

    def read_filesystem_path(self, rel_path: str) -> FilesystemEntry:
        try:
            response = self._stub.ReadFilesystemPath(
                pb2.ReadFilesystemPathRequest(trial_id=self._trial_id, path=rel_path)
            )
        except grpc.RpcError as err:
            raise SubstrateUnreachableError(str(err)) from err
        content_bytes = (
            base64.b64decode(response.content_bytes_b64) if response.content_bytes_b64 else b""
        )
        return FilesystemEntry(
            exists=response.exists,
            is_file=response.is_file,
            is_dir=response.is_dir,
            content_utf8=response.content_utf8,
            content_bytes=content_bytes,
        )

    def list_filesystem_dir(self) -> list[str]:
        try:
            response = self._stub.ListFilesystemDir(
                pb2.ListFilesystemDirRequest(trial_id=self._trial_id)
            )
        except grpc.RpcError as err:
            raise SubstrateUnreachableError(str(err)) from err
        return list(response.rel_paths)

    def kb_search(self, query: str, top_k: int, alpha: float) -> KBSearchResult:
        try:
            response = self._stub.KBSearch(
                pb2.KBSearchRequest(
                    trial_id=self._trial_id,
                    query=query,
                    top_k=top_k,
                    alpha=alpha,
                )
            )
        except grpc.RpcError as err:
            raise SubstrateUnreachableError(str(err)) from err
        hits = [
            SearchHit(
                doc_id=hit.doc_id,
                source=hit.source,
                score=hit.score,
                text=hit.text,
            )
            for hit in response.hits
        ]
        return KBSearchResult(kb_available=response.kb_available, hits=hits)

    def health_check(self) -> str:
        try:
            response = self._stub.SubstrateHealthCheck(pb2.SubstrateHealthCheckRequest())
        except grpc.RpcError as err:
            raise SubstrateUnreachableError(str(err)) from err
        return response.status

    @staticmethod
    def _decode_state(state_json: str) -> dict[str, Any]:
        if not state_json:
            return {}
        decoded = json.loads(state_json)
        if not isinstance(decoded, dict):
            raise SubstrateUnreachableError(
                f"SubstrateService returned a non-object state_json: {decoded!r}"
            )
        return decoded


__all__ = [
    "FilesystemEntry",
    "GrpcSubstrateClient",
    "KBSearchResult",
]
