"""Agent container module - gRPC service for LLM-based action generation

This module provides the agent service that runs in an isolated container.
It receives conversation history and generates the next action (tool call or final response).
"""

from tolokaforge.agent.service import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
