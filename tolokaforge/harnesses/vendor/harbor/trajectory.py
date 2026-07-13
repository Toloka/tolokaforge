# Copyright 2025 The Harbor Authors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""ATIF construction adapted from Harbor's Claude Code trajectory exporter."""

from __future__ import annotations

from typing import Any

from tolokaforge.core.models import Message, MessageRole


def messages_to_atif(
    messages: list[Message],
    *,
    session_id: str,
    harness_type: str,
    harness_version: str,
    model_name: str | None,
    final_event: dict[str, Any],
) -> dict[str, Any]:
    tool_results = {
        message.tool_call_id: message.content
        for message in messages
        if message.role == MessageRole.TOOL and message.tool_call_id
    }
    steps: list[dict[str, Any]] = []
    for message in messages:
        if message.role == MessageRole.TOOL:
            continue
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": "agent" if message.role == MessageRole.ASSISTANT else "user",
            "message": message.content,
        }
        if message.tool_calls:
            step["tool_calls"] = [
                {
                    "tool_call_id": call.id,
                    "function_name": call.name,
                    "arguments": call.arguments,
                }
                for call in message.tool_calls
            ]
            observations = [
                {"source_call_id": call.id, "content": tool_results[call.id]}
                for call in message.tool_calls
                if call.id in tool_results
            ]
            if observations:
                step["observation"] = {"results": observations}
        steps.append(step)
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "agent": {
            "name": harness_type,
            "version": harness_version,
            "model_name": model_name,
        },
        "steps": steps,
        "final_metrics": {
            "total_cost_usd": float(final_event.get("total_cost_usd", 0.0) or 0.0),
            "num_turns": int(final_event.get("num_turns", 0) or 0),
        },
    }
