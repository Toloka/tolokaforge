"""``tolokaforge grader-service`` CLI entry — launches the standalone service.

Runs the gRPC server on the configured port with the ``GraderServiceImpl``
mounted, wired to a :class:`GraderCompositeDispatch` — the dispatcher that
turns each :class:`GradeDispatch` payload into a real :class:`Grade` by
running the composite grading pipeline over a
:class:`LiveRunnerCallbackGradingSubstrate` dialled at the trial's
``runner_substrate_address``.

The dispatch is stateless per call: every field the composite needs to
grade the trial travels on the wire (see ``grader.proto`` schema v2 and
``docs/GRADER_SERVICE.md``). The service can be pointed at from any
orchestrator without a prior registration handshake.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from concurrent import futures

import grpc

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.grader import grader_pb2_grpc
from tolokaforge.grader.composite_dispatch import GraderCompositeDispatch
from tolokaforge.grader.service import GraderServiceImpl


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
        description="Standalone tolokaforge grader service (ADR-0038, ADR-0039).",
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

    dispatch = GraderCompositeDispatch(logger)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=args.max_workers))
    grader_pb2_grpc.add_GraderServiceServicer_to_server(
        GraderServiceImpl(judge_fn=dispatch.grade, logger=logger),
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
