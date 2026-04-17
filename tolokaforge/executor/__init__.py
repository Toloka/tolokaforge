"""Executor container module - gRPC service for tool execution

This module provides the executor service that runs in a container with env-net access.
It executes tools against environment services and returns results.
"""

from tolokaforge.executor.service import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
