"""The user-simulator conformance kit an external simulator runs against itself.

``tolokaforge.user_simulators`` lets a third-party package supply the user actor
a conversational trial dispatches. The
:class:`~tolokaforge.core.actors.user_simulator.UserSimulator` Protocol states
what such a simulator owes the engine — a ``reply`` returning a
:class:`~tolokaforge.core.llm.client.GenerationResult`, and a
``last_system_prompt`` the runner records — but each obligation is read
downstream rather than by the type checker, so a break produces a wrong artifact,
not an exception.

Three things ship here:

- :class:`UserSimulatorConformanceSuite` — the pytest suite an implementer points
  at their own factory. One fixture to override; the assertions are behavioural.
- :class:`InMemoryUserSimulator` — the reference implementation and the worked
  example to copy. Its :class:`SimulatorDefects` knobs switch obligations off one
  at a time, which is how the suite's own teeth are proven.
- :func:`in_memory_user_simulator_factory` — the reference factory.

Adoption is five lines::

    import pytest
    from tolokaforge.testing.user_simulators import UserSimulatorConformanceSuite

    class TestMySimulatorConformance(UserSimulatorConformanceSuite):
        @pytest.fixture
        def simulator_factory(self):
            return my_user_simulator_factory
"""

from .conformance import (
    UserSimulatorConformanceSuite,
    run_reply,
    scripted_context,
)
from .in_memory import (
    InMemoryUserSimulator,
    InMemoryUserSimulatorCallLog,
    SimulatorDefects,
    in_memory_user_simulator_factory,
)

__all__ = [
    "InMemoryUserSimulator",
    "InMemoryUserSimulatorCallLog",
    "SimulatorDefects",
    "UserSimulatorConformanceSuite",
    "in_memory_user_simulator_factory",
    "run_reply",
    "scripted_context",
]
