"""Runner-side ``SubstrateService`` — read-only view of a trial's substrate.

The runner registers this servicer on the same gRPC server + same listen port
as :class:`RunnerService` iff ``RunConfig.grader.expose_substrate`` is true.
An independent grader (:class:`LiveRunnerCallbackGradingSubstrate`) dials
this surface to answer every read the composite grading dispatch makes.

Read-only by construction. The class holds :data:`_READ_ONLY` = ``True`` and
implements no write handler; :func:`test_substrate_service_is_read_only_by_construction`
enumerates the public method set against the generated
:class:`SubstrateServiceServicer` base and refuses any name matching a write
verb (``set_`` / ``insert`` / ``update`` / ``write`` / ``delete`` / ``mutate``).

Every RPC delegates to the ``RunnerServiceImpl`` that owns the trial. No
substrate-side state accumulation; the servicer is a thin adapter.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import grpc

from tolokaforge.core.grading.filesystem_view import (
    is_excluded_rel_path,
    iter_agent_visible_rel_paths,
)
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import runner_pb2_grpc as pb2_grpc
from tolokaforge.runner.db_client import (
    DBServiceError,
)
from tolokaforge.runner.db_client import (
    TrialNotFoundError as DBTrialNotFoundError,
)

if TYPE_CHECKING:
    from tolokaforge.runner.service import RunnerServiceImpl


logger = logging.getLogger(__name__)


class SubstrateServicer(pb2_grpc.SubstrateServiceServicer):
    """Read-only substrate surface backed by a live :class:`RunnerServiceImpl`.

    Constructed once per runner process. The instance holds a reference to
    the runner service and delegates each RPC to it — the runner owns the
    DB client, RAG client, per-trial KB resolution, and the agent-visible
    workspace filter this servicer exposes.
    """

    _READ_ONLY: bool = True
    """Structural read-only invariant. The class implements no write handler;
    the read-only test asserts this constant is set AND that no public method
    name matches a write verb."""

    def __init__(self, runner_service: RunnerServiceImpl) -> None:
        self._runner = runner_service

    # ------------------------------------------------------------------
    # State reads
    # ------------------------------------------------------------------

    def ReadInitialState(  # noqa: N802 — gRPC servicer method casing
        self,
        request: pb2.ReadInitialStateRequest,
        context: grpc.ServicerContext,
    ) -> pb2.ReadStateResponse:
        trial_context = self._runner.trials.get(request.trial_id)
        if trial_context is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Trial '{request.trial_id}' not registered")
            return pb2.ReadStateResponse()
        initial = trial_context.task_description.initial_state
        tables = initial.tables if initial is not None else {}
        return pb2.ReadStateResponse(state_json=json.dumps(tables or {}))

    def ReadFinalDBState(  # noqa: N802
        self,
        request: pb2.ReadFinalDBStateRequest,
        context: grpc.ServicerContext,
    ) -> pb2.ReadStateResponse:
        tables = list(request.tables) if request.tables else None
        try:
            state = self._runner._run_async(
                self._runner.db_client.get_state(request.trial_id, tables)
            )
        except DBTrialNotFoundError:
            return pb2.ReadStateResponse(state_json="{}", trial_not_found=True)
        except DBServiceError as exc:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details(f"DB Service error: {exc.message}")
            return pb2.ReadStateResponse()
        return pb2.ReadStateResponse(state_json=json.dumps(state.data))

    def ReadFinalDBStateStable(  # noqa: N802
        self,
        request: pb2.ReadFinalDBStateStableRequest,
        context: grpc.ServicerContext,
    ) -> pb2.ReadStateResponse:
        try:
            state = self._runner._run_async(
                self._runner.db_client.get_stable_state(request.trial_id)
            )
        except DBTrialNotFoundError:
            return pb2.ReadStateResponse(state_json="{}", trial_not_found=True)
        except DBServiceError as exc:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details(f"DB Service error: {exc.message}")
            return pb2.ReadStateResponse()
        return pb2.ReadStateResponse(state_json=json.dumps(state.data))

    # ------------------------------------------------------------------
    # Filesystem reads
    # ------------------------------------------------------------------

    def ReadFilesystemPath(  # noqa: N802
        self,
        request: pb2.ReadFilesystemPathRequest,
        context: grpc.ServicerContext,
    ) -> pb2.ReadFilesystemPathResponse:
        if request.path and is_excluded_rel_path(request.path):
            return pb2.ReadFilesystemPathResponse(exists=False)
        root = self._workspace_root()
        target = (root / request.path).resolve() if request.path else root
        # Refuse a path that escapes the workspace root — a defensive check
        # against a resolved symlink or a ``..`` component pointing outside
        # AGENT_WORK_DIR. The agent-visible surface is bounded by design.
        try:
            target.relative_to(root)
        except ValueError:
            return pb2.ReadFilesystemPathResponse(exists=False)
        if target.is_symlink() or not target.exists():
            return pb2.ReadFilesystemPathResponse(exists=False)
        if target.is_dir():
            return pb2.ReadFilesystemPathResponse(exists=True, is_dir=True)
        if not target.is_file():
            return pb2.ReadFilesystemPathResponse(exists=False)
        try:
            content = target.read_text(encoding="utf-8")
            return pb2.ReadFilesystemPathResponse(exists=True, is_file=True, content_utf8=content)
        except UnicodeDecodeError:
            data = target.read_bytes()
            return pb2.ReadFilesystemPathResponse(
                exists=True,
                is_file=True,
                content_bytes_b64=base64.b64encode(data).decode("ascii"),
            )
        except OSError as exc:
            logger.warning(
                "SubstrateService.ReadFilesystemPath: could not read %s: %s", target, exc
            )
            return pb2.ReadFilesystemPathResponse(exists=False)

    def ListFilesystemDir(  # noqa: N802
        self,
        request: pb2.ListFilesystemDirRequest,
        context: grpc.ServicerContext,
    ) -> pb2.ListFilesystemDirResponse:
        rel_paths = sorted(iter_agent_visible_rel_paths(self._workspace_root()))
        return pb2.ListFilesystemDirResponse(rel_paths=rel_paths)

    # ------------------------------------------------------------------
    # KB reads
    # ------------------------------------------------------------------

    def KBSearch(  # noqa: N802
        self,
        request: pb2.KBSearchRequest,
        context: grpc.ServicerContext,
    ) -> pb2.KBSearchResponse:
        trial_context = self._runner.trials.get(request.trial_id)
        if trial_context is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Trial '{request.trial_id}' not registered")
            return pb2.KBSearchResponse()
        kb = trial_context.resolve_kb_search()
        if kb is None:
            return pb2.KBSearchResponse(kb_available=False)
        # The judge's KnowledgeSearch is synchronous by contract (see
        # docs/RUBRIC_GRADING_DESIGN.md § "the judge runs on the runner"), so
        # no loop bridge is required here.
        hits = kb.search(request.query, top_k=request.top_k, alpha=request.alpha)
        return pb2.KBSearchResponse(
            kb_available=True,
            hits=[
                pb2.SubstrateSearchHit(
                    doc_id=hit.doc_id,
                    source=hit.source,
                    score=hit.score,
                    text=hit.text,
                )
                for hit in hits
            ],
        )

    # ------------------------------------------------------------------
    # SQL probe
    # ------------------------------------------------------------------

    def RunDbProbe(  # noqa: N802
        self,
        request: pb2.RunDbProbeRequest,
        context: grpc.ServicerContext,
    ) -> pb2.RunDbProbeResponse:
        from tolokaforge.core.grading.db_probes import _fetch_probe_rows

        try:
            rows = self._runner._run_async(_fetch_probe_rows(request.dsn, request.query))
        except Exception as exc:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"RunDbProbe failed: {type(exc).__name__}: {exc}")
            return pb2.RunDbProbeResponse()
        return pb2.RunDbProbeResponse(rows_json=json.dumps(rows, default=str))

    # ------------------------------------------------------------------
    # Test-suite execution
    # ------------------------------------------------------------------

    def RunTestSuite(  # noqa: N802
        self,
        request: pb2.RunTestSuiteRequest,
        context: grpc.ServicerContext,
    ) -> pb2.RunTestSuiteResponse:
        """Execute a pack-declared verifier inside the trial's env container.

        Two first-class outcomes ride the response rather than the gRPC
        status: ``tool_absent`` (the trial completed but the adapter shipped
        no exec-capable lifecycle tool — a pack-authoring issue) and
        ``script_exec_error`` (the exec call raised, e.g. subprocess timeout —
        the trial completed but the verifier crashed). Neither is an RPC
        failure: the caller renders both as observable grade outcomes.

        The reward file is read via ``cat {reward_path} 2>/dev/null || echo
        0.0`` so an absent file yields ``b"0.0\\n"``. A raised exception
        inside the reward-cat call is treated the same way (falls back to
        ``b"0.0\\n"``) — a resilience upgrade over the pre-move runner-side
        path, which would have propagated the exception through the async
        bridge; observable only when the reward-cat call itself raises,
        which the pre-move ``|| echo 0.0`` shell fallback made rare.

        ``stdout`` is wire-capped at 65_536 bytes; the caller further
        truncates for the grade's reasons string.
        """
        _DEFAULT_SCRIPT_TIMEOUT_S = 300.0
        _DEFAULT_REWARD_READ_TIMEOUT_S = 10.0
        _STDOUT_WIRE_CAP_BYTES = 65_536

        trial_context = self._runner.trials.get(request.trial_id)
        if trial_context is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Trial '{request.trial_id}' not registered")
            return pb2.RunTestSuiteResponse()

        from tolokaforge.runner.service import _first_docker_compose_exec_tool

        bash_tool = _first_docker_compose_exec_tool(trial_context.agent_tools.values())
        if bash_tool is None:
            error_msg = (
                "test-execution grading was requested (grading_method='test_execution') "
                "but no exec-capable env tool was found in this trial. Include an "
                "exec-capable lifecycle tool (e.g. DockerComposeExecToolWrapper) in "
                "TaskDescription.agent_tools so the runner can execute the test suite "
                "inside the trial environment."
            )
            return pb2.RunTestSuiteResponse(
                tool_absent=True,
                tool_absent_reason=error_msg,
            )

        script_timeout = request.timeout_s if request.timeout_s > 0.0 else _DEFAULT_SCRIPT_TIMEOUT_S
        reward_read_timeout = (
            request.reward_read_timeout_s
            if request.reward_read_timeout_s > 0.0
            else _DEFAULT_REWARD_READ_TIMEOUT_S
        )

        try:
            exit_code, stdout = bash_tool._exec_sync_with_rc(
                f"cd $(dirname {request.script_path}) && bash {request.script_path} 2>&1",
                script_timeout,
            )
        except Exception as exc:
            return pb2.RunTestSuiteResponse(
                exit_code=-1,
                reward_bytes=b"",
                stdout="",
                script_exec_error=str(exc),
            )

        try:
            _rc, reward_str = bash_tool._exec_sync_with_rc(
                f"cat {request.reward_path} 2>/dev/null || echo 0.0",
                reward_read_timeout,
            )
            reward_bytes = reward_str.encode()
        except Exception as exc:  # noqa: BLE001
            # Reward-cat is a diagnostic read; the fallback bytes match the shell
            # ``|| echo 0.0`` path so the kind renders the same "0.0" reward.
            logger.warning(
                "SubstrateService.RunTestSuite: reward-cat raised for trial %r: "
                "%s: %s; falling back to b'0.0\\n'",
                request.trial_id,
                type(exc).__name__,
                exc,
            )
            reward_bytes = b"0.0\n"

        return pb2.RunTestSuiteResponse(
            exit_code=exit_code,
            reward_bytes=reward_bytes,
            stdout=stdout[:_STDOUT_WIRE_CAP_BYTES],
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def SubstrateHealthCheck(  # noqa: N802
        self,
        request: pb2.SubstrateHealthCheckRequest,  # noqa: ARG002
        context: grpc.ServicerContext,  # noqa: ARG002
    ) -> pb2.SubstrateHealthCheckResponse:
        return pb2.SubstrateHealthCheckResponse(
            status="ready",
            active_trials=len(self._runner.trials),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _workspace_root(self) -> Path:
        # Import at call time to avoid a top-level cycle through service.py.
        from tolokaforge.runner.service import AGENT_WORK_DIR

        return Path(AGENT_WORK_DIR)


__all__ = ["SubstrateServicer"]
