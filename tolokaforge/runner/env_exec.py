"""The env-exec capability grading reaches a trial's environment through.

Three grading consumers run a command inside the environment a trial's agent
worked in: the ``test_execution`` verifier run
(``RunnerServiceImpl._run_test_suite_via_agent_tools``), the filesystem
snapshot behind state-checks grading
(``RunnerServiceImpl._read_filesystem_for_state``), and the detached grader's
``SubstrateServicer.RunTestSuite``. Each picks its executor out of the trial's
registered ``agent_tools`` with :func:`first_env_exec_tool`.

The capability is structural: a tool qualifies by exposing the two exec
methods, not by belonging to a particular wrapper class. Same discipline as
:data:`tolokaforge.runner.harness_state.BashExec`.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol, runtime_checkable

__all__ = ["SupportsEnvExec", "first_env_exec_tool"]


@runtime_checkable
class SupportsEnvExec(Protocol):
    """Runs a command inside the trial's environment.

    The two methods differ in what they return and in how they treat a
    deadline overrun; grading uses both.
    """

    def exec_in_env(self, command: str, timeout_s: float) -> str:
        """Run ``command`` and return its output.

        A deadline overrun returns whatever the command had already written,
        with a timed-out marker appended, rather than raising.
        """
        ...

    def exec_in_env_with_exit_code(self, command: str, timeout_s: float) -> tuple[int, str]:
        """Run ``command`` and return ``(exit_code, output)``.

        A deadline overrun raises :class:`subprocess.TimeoutExpired`, which the
        callers render as an observable grade outcome.
        """
        ...


def first_env_exec_tool(tools: Collection[Any]) -> SupportsEnvExec | None:
    """Return the first tool among *tools* that can exec in the environment.

    ``None`` means the trial registered no such tool; the caller reports that
    as its own outcome rather than grading against an absent environment.
    """
    for tool in tools:
        if isinstance(tool, SupportsEnvExec):
            return tool
    return None
