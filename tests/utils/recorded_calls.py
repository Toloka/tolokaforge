"""Builder for :class:`RecordedToolCall` test fixtures.

``RecordedToolCall`` has nine required fields, most of which are noise for a
consumer test that only cares about the tool name and whether the call
succeeded. This names the fields a test is actually asserting on and fills the
rest with inert values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tolokaforge.core.models import (
    RecordedToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
)

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def recorded_call(
    tool_name: str,
    *,
    sequence: int = 0,
    status: ToolExecutionStatus = ToolExecutionStatus.SUCCESS,
    arguments: dict[str, Any] | None = None,
    executor: ToolExecutorIdentity = ToolExecutorIdentity.AGENT,
    output: str = "",
    latency_seconds: float = 0.0,
    call_id: str | None = None,
) -> RecordedToolCall:
    """One recorded tool call. ``call_id`` defaults to a value unique per sequence."""
    return RecordedToolCall(
        call_id=call_id if call_id is not None else f"toolu_{tool_name}_{sequence}",
        sequence=sequence,
        tool_name=tool_name,
        arguments=arguments or {},
        executor=executor,
        status=status,
        output=output,
        latency_seconds=latency_seconds,
        timestamp=_EPOCH,
    )
