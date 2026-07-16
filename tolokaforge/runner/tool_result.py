"""Fail-closed classification of tool execution results."""

from __future__ import annotations

import json
from typing import Any


def tool_error_message(result: Any) -> str | None:
    """Return an explicit error message for MCP or error-shaped tool output."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return None

    if not isinstance(result, dict):
        return None
    if result.get("isError") is True:
        return _content_text(result) or "MCP tool returned isError=true"
    if result.get("error") not in (None, "", False):
        return str(result["error"])

    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            nested = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(nested, dict) and nested.get("error") not in (None, "", False):
            return str(nested["error"])
    return None


def _content_text(result: dict[str, Any]) -> str | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        item["text"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "\n".join(texts) or None
