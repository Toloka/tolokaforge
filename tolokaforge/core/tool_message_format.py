"""How a tool call's outcome is worded in the ``role: tool`` message.

The message view carries no execution status. A failed call is marked in the
message body and nowhere else, by the prefix declared here, so a trial re-graded
from its messages alone — a bundle with no ``tool_log.yaml`` — recovers the
result text by stripping exactly what was written. The loop that writes the
message and the grading path that reads it back both import this name instead of
spelling the literal twice: a mismatch would make every failed call read as a
successful one to a ``result:`` trace check, which is a wrong grade rather than
an error.

Stdlib only, for the same reason :mod:`tolokaforge.core.tool_call_ids` is: the
runtime and the grading path both depend on it, and neither may reach the other
through it.
"""

from __future__ import annotations

__all__ = ["TOOL_ERROR_MESSAGE_PREFIX"]

TOOL_ERROR_MESSAGE_PREFIX = "Error: "
"""Prefix the ``role: tool`` message content of a **failed** tool call carries."""
