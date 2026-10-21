"""``tolokaforge trial-node`` entry — launches the standalone trial node service.

Runs the gRPC server on the configured port with the ``TrialNodeServiceImpl``
mounted. The node serves one trial at a time; the server's other workers keep
health probes and ``NodeBusy`` replies answerable while a trial runs.

Configuration:

* ``--port`` / ``$TRIAL_NODE_PORT`` — listen port (default 50061).
* ``--output-root`` / ``$TRIAL_NODE_OUTPUT_ROOT`` — where trial artifacts are
  written, under ``<output-root>/<run_id>`` (default ``<tmp>/tolokaforge-trial-node``).
* ``--task-pack-root`` / ``$TRIAL_NODE_TASK_PACK_ROOT`` — where task packs are
  mounted, one directory per task id; unset reads each pack from the path the
  engine sent.
* ``$TOLOKAFORGE_GRPC_MAX_MESSAGE_MB`` — the gRPC message bound (default 50 MiB);
  set it to at least the engine's ``compute.grpc_max_message_mb``.
* ``$TOLOKAFORGE_SECRETS_JSON`` — the node's credential payload; the trial inputs
  carry ``${secret:NAME}`` references only, resolved from it.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import tempfile
from collections.abc import Sequence
from concurrent import futures
from pathlib import Path

import grpc

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.trial_node import install_container_secrets
from tolokaforge.runner.grpc_limits import (
    channel_options,
    keepalive_options,
    max_message_mb_from_env,
)
from tolokaforge.trial_node import trial_node_pb2_grpc
from tolokaforge.trial_node.protocol import NODE_PROTOCOL_VERSION
from tolokaforge.trial_node.service import TrialNodeServiceImpl

_DEFAULT_PORT = 50061
_SERVER_WORKERS = 4


def _resolve_port(cli_port: int | None) -> int:
    if cli_port is not None:
        return cli_port
    env = (os.environ.get("TRIAL_NODE_PORT") or "").strip()
    if env:
        try:
            return int(env)
        except ValueError as exc:
            raise SystemExit(f"Invalid TRIAL_NODE_PORT: {env!r}") from exc
    return _DEFAULT_PORT


def _resolve_path(cli_value: Path | None, env_var: str) -> Path | None:
    if cli_value is not None:
        return cli_value
    env = (os.environ.get(env_var) or "").strip()
    return Path(env) if env else None


def build_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Standalone tolokaforge trial node: runs one whole trial per RunTrial call.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to bind the gRPC server on (default {_DEFAULT_PORT} or $TRIAL_NODE_PORT).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Directory trial artifacts are written under (default <tmp>/tolokaforge-trial-node "
        "or $TRIAL_NODE_OUTPUT_ROOT).",
    )
    parser.add_argument(
        "--task-pack-root",
        type=Path,
        default=None,
        help="Directory holding one mounted task pack per task id "
        "(default: read packs from the engine-sent path; or $TRIAL_NODE_TASK_PACK_ROOT).",
    )
    return parser


def run_server(
    *, port: int, output_root: Path, task_pack_root: Path | None, logger: StructuredLogger
) -> int:
    """Serve until SIGINT / SIGTERM; returns the process exit code."""
    # A malformed credential payload fails the node at startup, not on its first trial.
    install_container_secrets()
    max_message_mb = max_message_mb_from_env()
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=_SERVER_WORKERS),
        options=channel_options(max_message_mb) + keepalive_options(),
    )
    trial_node_pb2_grpc.add_TrialNodeServiceServicer_to_server(
        TrialNodeServiceImpl(
            logger=logger,
            max_message_mb=max_message_mb,
            output_root=output_root,
            task_pack_root=task_pack_root,
        ),
        server,
    )
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(
        "trial-node started",
        port=port,
        node_protocol_version=NODE_PROTOCOL_VERSION,
        grpc_max_message_mb=max_message_mb,
        output_root=str(output_root),
        task_pack_root=str(task_pack_root) if task_pack_root else None,
    )

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("trial-node shutting down", signal=signum)
        server.stop(grace=5).wait()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    server.wait_for_termination()
    return 0


def main(argv: Sequence[str] | None = None, *, prog: str = "tolokaforge trial-node") -> int:
    args = build_parser(prog).parse_args(argv)
    output_root = _resolve_path(args.output_root, "TRIAL_NODE_OUTPUT_ROOT") or (
        Path(tempfile.gettempdir()) / "tolokaforge-trial-node"
    )
    return run_server(
        port=_resolve_port(args.port),
        output_root=output_root,
        task_pack_root=_resolve_path(args.task_pack_root, "TRIAL_NODE_TASK_PACK_ROOT"),
        logger=StructuredLogger("trial-node"),
    )


if __name__ == "__main__":
    sys.exit(main(prog="python -m tolokaforge.trial_node"))
