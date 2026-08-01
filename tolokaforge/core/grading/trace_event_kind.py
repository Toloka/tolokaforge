"""The four kinds of event a trial timeline carries.

A leaf with no first-party imports, because both ends of the trace vocabulary need
it: :mod:`tolokaforge.core.grading.trace_timeline` builds events of these kinds,
and ``TraceMatcher`` in :mod:`tolokaforge.runner.models` selects on them. Declaring
it in either of those would force the other to import a module that imports it
back — ``trace_timeline`` reads ``core.models``, which reads ``runner.models`` at
module level.
"""

from enum import Enum


class TraceEventKind(str, Enum):
    """What a timeline event is."""

    ASSISTANT_MESSAGE = "assistant_message"
    USER_MESSAGE = "user_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
