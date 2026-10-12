"""The agent-loop conformance kit an external loop runs against itself.

``tolokaforge.agent_loops`` lets a third-party package drive a whole trial. The
:class:`~tolokaforge.core.loop.AgentLoop` Protocol states what such a loop owes
the engine, but every obligation is enforced downstream in grading rather than
by the type checker — so a loop that breaks one produces a plausible trajectory
and a wrong grade, not an exception.

Three things ship here:

- :class:`AgentLoopConformanceSuite` — the pytest suite an implementer points at
  their own factory. One fixture to override; the assertions are behavioural,
  driven through a scripted episode and read off the artifacts grading reads.
- :class:`InMemoryAgentLoop` — the reference implementation. The shortest loop
  that satisfies every obligation, and the worked example to copy. Its
  :class:`LoopDefects` knobs switch obligations off one at a time, which is how
  the suite's own teeth are proven.
- The harness — :class:`EpisodeHarness` and the scripted seams it assembles, for
  an implementer writing loop tests of their own beyond the suite.

Adoption is five lines::

    import pytest
    from tolokaforge.testing.agent_loops import AgentLoopConformanceSuite

    class TestMyLoopConformance(AgentLoopConformanceSuite):
        @pytest.fixture
        def loop_factory(self):
            return my_agent_loop_factory
"""

from .conformance import (
    AgentLoopConformanceSuite,
    EpisodeResult,
    run_episode,
    tool_calls_by_id,
    tool_results_by_id,
)
from .harness import (
    EpisodeHarness,
    GenerationCall,
    RecordingMetricsSink,
    RecordingTerminationPolicy,
    RecordingToolExecutor,
    ScriptedLLMClient,
    ScriptedUserTurn,
    TerminationCall,
    ToolExecution,
    UserTurnCall,
    assistant_turn,
    stop_when_no_tool_calls,
    tool_call,
    tool_output_for,
)
from .in_memory import (
    InMemoryAgentLoop,
    InMemoryAgentLoopCallLog,
    LoopDefects,
    in_memory_agent_loop_factory,
)

__all__ = [
    "AgentLoopConformanceSuite",
    "EpisodeHarness",
    "EpisodeResult",
    "GenerationCall",
    "InMemoryAgentLoop",
    "InMemoryAgentLoopCallLog",
    "LoopDefects",
    "RecordingMetricsSink",
    "RecordingTerminationPolicy",
    "RecordingToolExecutor",
    "ScriptedLLMClient",
    "ScriptedUserTurn",
    "TerminationCall",
    "ToolExecution",
    "UserTurnCall",
    "assistant_turn",
    "in_memory_agent_loop_factory",
    "run_episode",
    "stop_when_no_tool_calls",
    "tool_call",
    "tool_calls_by_id",
    "tool_output_for",
    "tool_results_by_id",
]
