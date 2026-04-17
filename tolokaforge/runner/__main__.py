"""
Runner gRPC Server Entry Point

This module provides the main entry point for the Runner gRPC server.
It reads configuration from environment variables, creates the service,
and starts the gRPC server with graceful shutdown handling.

Usage:
    python -m tolokaforge.runner

Environment Variables:
    DB_SERVICE_URL: URL of the DB Service (default: http://localhost:8000)
    RAG_SERVICE_URL: URL of the RAG Service (default: http://localhost:8001)
    RUNNER_PORT: gRPC server port (default: 50051)
    LOG_LEVEL: Logging level (default: INFO)
"""

import asyncio
import logging
import os
import signal
import sys
from concurrent import futures

import grpc

from tolokaforge.runner import add_RunnerServiceServicer_to_server
from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.rag_client import RAGServiceClient
from tolokaforge.runner.service import RunnerServiceImpl

# Default configuration
DEFAULT_DB_SERVICE_URL = "http://localhost:8000"
DEFAULT_RAG_SERVICE_URL = "http://localhost:8001"
DEFAULT_RUNNER_PORT = 50051
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_MAX_WORKERS = 10


def setup_logging(level: str) -> None:
    """
    Configure logging for the Runner service.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def get_config() -> dict:
    """
    Read configuration from environment variables.

    Returns:
        Configuration dictionary
    """
    return {
        "db_service_url": os.environ.get("DB_SERVICE_URL", DEFAULT_DB_SERVICE_URL),
        "rag_service_url": os.environ.get("RAG_SERVICE_URL", DEFAULT_RAG_SERVICE_URL),
        "runner_port": int(os.environ.get("RUNNER_PORT", DEFAULT_RUNNER_PORT)),
        "log_level": os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL),
        "max_workers": int(os.environ.get("MAX_WORKERS", DEFAULT_MAX_WORKERS)),
    }


class RunnerServer:
    """
    Runner gRPC server with graceful shutdown support.

    This class manages the lifecycle of the gRPC server, including:
    - Server creation and startup
    - Signal handling for graceful shutdown
    - Resource cleanup
    """

    def __init__(
        self,
        db_service_url: str,
        port: int,
        max_workers: int = DEFAULT_MAX_WORKERS,
        rag_service_url: str | None = None,
    ):
        """
        Initialize the Runner server.

        Args:
            db_service_url: URL of the DB Service
            port: gRPC server port
            max_workers: Maximum number of worker threads
            rag_service_url: Optional URL of the RAG Service
        """
        self.db_service_url = db_service_url
        self.rag_service_url = rag_service_url
        self.port = port
        self.max_workers = max_workers
        self.server: grpc.Server | None = None
        self.db_client: DBServiceClient | None = None
        self.rag_client: RAGServiceClient | None = None
        self.service: RunnerServiceImpl | None = None
        self._shutdown_event = asyncio.Event()
        self.logger = logging.getLogger(__name__)

    async def start(self) -> None:
        """
        Start the gRPC server.

        Creates the DB client, RAG client, service implementation, and gRPC server,
        then starts listening for requests.
        """
        self.logger.info(f"Starting Runner server on port {self.port}")
        self.logger.info(f"DB Service URL: {self.db_service_url}")
        self.logger.info(f"RAG Service URL: {self.rag_service_url or 'not configured'}")

        # Create DB client
        self.db_client = DBServiceClient(self.db_service_url)

        # Create RAG client if URL is configured
        if self.rag_service_url:
            self.rag_client = RAGServiceClient(self.rag_service_url)
            self.logger.info("RAG client initialized")

        # Create service implementation
        self.service = RunnerServiceImpl(self.db_client, self.rag_client)

        # Create gRPC server
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=self.max_workers),
            options=[
                ("grpc.max_send_message_length", 50 * 1024 * 1024),  # 50MB
                ("grpc.max_receive_message_length", 50 * 1024 * 1024),  # 50MB
            ],
        )

        # Add service to server
        add_RunnerServiceServicer_to_server(self.service, self.server)

        # Bind to port
        listen_addr = f"[::]:{self.port}"
        self.server.add_insecure_port(listen_addr)

        # Start server
        self.server.start()
        self.logger.info(f"Runner server started, listening on {listen_addr}")

        # Check DB Service connectivity
        try:
            health = await self.db_client.health_check()
            self.logger.info(f"DB Service health: {health.status}")
        except Exception as e:
            self.logger.warning(f"DB Service not available: {e}")
            self.logger.warning("Server started but DB Service connectivity is degraded")

        # Check RAG Service connectivity (optional)
        if self.rag_client:
            try:
                rag_healthy = await self.rag_client.is_healthy()
                self.logger.info(f"RAG Service health: {'healthy' if rag_healthy else 'unhealthy'}")
            except Exception as e:
                self.logger.warning(f"RAG Service not available: {e}")
                self.logger.warning("Server started but RAG Service connectivity is degraded")

    async def wait_for_termination(self) -> None:
        """
        Wait for the server to be terminated.

        Blocks until a shutdown signal is received or stop() is called.
        """
        await self._shutdown_event.wait()

    async def stop(self, grace_period: float = 5.0) -> None:
        """
        Stop the gRPC server gracefully.

        Args:
            grace_period: Time in seconds to wait for pending requests
        """
        self.logger.info("Stopping Runner server...")

        # Clean up all trials
        if self.service:
            self.logger.info("Cleaning up active trials...")
            self.service.cleanup_all_trials()

        # Stop gRPC server
        if self.server:
            self.logger.info(f"Stopping gRPC server (grace period: {grace_period}s)...")
            self.server.stop(grace_period)

        # Close DB client
        if self.db_client:
            self.logger.info("Closing DB client...")
            await self.db_client.close()

        # Close RAG client
        if self.rag_client:
            self.logger.info("Closing RAG client...")
            await self.rag_client.close()

        self._shutdown_event.set()
        self.logger.info("Runner server stopped")

    def trigger_shutdown(self) -> None:
        """Trigger server shutdown (called from signal handlers)."""
        asyncio.create_task(self.stop())


async def run_server() -> None:
    """
    Main entry point for running the Runner server.

    Reads configuration, sets up logging, creates the server,
    and handles graceful shutdown on SIGINT/SIGTERM.
    """
    # Read configuration
    config = get_config()

    # Setup logging
    setup_logging(config["log_level"])
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("Tolokaforge Runner Service")
    logger.info("=" * 60)
    logger.info("Configuration:")
    logger.info(f"  DB Service URL: {config['db_service_url']}")
    logger.info(f"  RAG Service URL: {config['rag_service_url']}")
    logger.info(f"  Runner Port: {config['runner_port']}")
    logger.info(f"  Log Level: {config['log_level']}")
    logger.info(f"  Max Workers: {config['max_workers']}")
    logger.info("=" * 60)

    # Create server
    server = RunnerServer(
        db_service_url=config["db_service_url"],
        port=config["runner_port"],
        max_workers=config["max_workers"],
        rag_service_url=config["rag_service_url"],
    )

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler(sig: signal.Signals) -> None:
        logger.info(f"Received signal {sig.name}, initiating shutdown...")
        server.trigger_shutdown()

    # Register signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))

    try:
        # Start server
        await server.start()

        # Wait for termination
        await server.wait_for_termination()

    except Exception as e:
        logger.error(f"Server error: {e}")
        await server.stop()
        raise


def main() -> None:
    """
    Synchronous entry point for the Runner server.

    This is the main function called when running:
        python -m tolokaforge.runner
    """
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        pass  # Handled by signal handler
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
