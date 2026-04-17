#!/usr/bin/env python3
"""Test that UserSimulator can accept tool schemas (pure in-memory)."""

import pytest

pytestmark = pytest.mark.unit

from datetime import datetime, timezone

from tolokaforge.core.model_client import GenerationResult, UserSimulator
from tolokaforge.core.models import Message, MessageRole


def test_user_simulator_tools():
    """Test that UserSimulator can accept tool schemas"""
    # Create mock tool schemas
    tool_schemas = [
        {
            "type": "function",
            "function": {
                "name": "check_status_bar",
                "description": "Check the status bar on device",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        }
    ]

    # Create user simulator with tools (scripted mode for simple test)
    user_sim = UserSimulator(mode="scripted", tool_schemas=tool_schemas)

    # Test reply returns GenerationResult
    context = [
        Message(
            role=MessageRole.ASSISTANT,
            content="Hi! How can I help?",
            ts=datetime.now(tz=timezone.utc),
        )
    ]

    result = user_sim.reply(context)

    # Verify result type
    assert isinstance(result, GenerationResult), f"Expected GenerationResult, got {type(result)}"
    assert isinstance(result.text, str), "Result should have text"
    assert isinstance(result.tool_calls, list), "Result should have tool_calls list"
