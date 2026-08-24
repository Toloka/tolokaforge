"""``tolokaforge grader-service`` CLI entry — launches the standalone service.

Runs the gRPC server on the configured port with the ``GraderServiceImpl``
mounted. Production wiring (constructing a real ``LLMJudge``-backed dispatch
from per-task rubric config) is deferred; today the CLI mounts an unwired
dispatch that surfaces ``NotImplementedError`` to the caller, matching the
``judge_only`` in-process grader's shape.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from concurrent import futures

import grpc

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import Grade
from tolokaforge.grader import grader_pb2_grpc
from tolokaforge.grader.service import GradeDispatch, GraderServiceImpl


def _unwired_judge_fn(dispatch: GradeDispatch) -> Grade | None:  # noqa: ARG001
    raise NotImplementedError(
        "grader-service is running with the unwired default judge dispatch. "
        "Production wiring (LLMJudge from per-task rubric config, offline "
        "rejudge integration) is deferred — see ADR-0038 and the "
        "grader-detachment umbrella."
    )


def _resolve_port(cli_port: int | None) -> int:
    if cli_port is not None:
        return cli_port
    env = (os.environ.get("GRADER_SERVICE_PORT") or "").strip()
    if env:
        try:
            return int(env)
        except ValueError as exc:
            raise SystemExit(f"Invalid GRADER_SERVICE_PORT: {env!r}") from exc
    return 50052


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tolokaforge grader-service",
        description="Standalone tolokaforge grader service (ADR-0038).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind the gRPC server on (default 50052 or $GRADER_SERVICE_PORT).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Thread-pool size for the gRPC server (default 10).",
    )
    args = parser.parse_args(argv)

    port = _resolve_port(args.port)
    logger = StructuredLogger("grader-service")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=args.max_workers))
    grader_pb2_grpc.add_GraderServiceServicer_to_server(
        GraderServiceImpl(judge_fn=_unwired_judge_fn, logger=logger),
        server,
    )
    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    server.start()
    logger.info("grader-service started", port=port, workers=args.max_workers)

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("grader-service shutting down", signal=signum)
        server.stop(grace=5).wait()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    server.wait_for_termination()
    return 0


if __name__ == "__main__":
    sys.exit(main())
