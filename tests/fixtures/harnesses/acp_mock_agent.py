#!/usr/bin/env python3
"""Deterministic local ACP agent used by the BYOH protocol smoke test."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from acp import PROTOCOL_VERSION, run_agent, update_agent_message_text
from acp.interfaces import Agent
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    McpCapabilities,
    NewSessionResponse,
    PromptResponse,
)


class MockAgent(Agent):
    def on_connect(self, conn: Any) -> None:
        self.client = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocolVersion=min(protocol_version, PROTOCOL_VERSION),
            agentCapabilities=AgentCapabilities(mcpCapabilities=McpCapabilities(http=True)),
            agentInfo=Implementation(name="tolokaforge-mock", version="1.0.0"),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        return NewSessionResponse(sessionId=f"mock-{uuid.uuid4().hex[:8]}")

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        await self.client.session_update(
            session_id=session_id,
            update=update_agent_message_text("mock completed"),
        )
        return PromptResponse(stopReason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


if __name__ == "__main__":
    asyncio.run(run_agent(MockAgent()))
