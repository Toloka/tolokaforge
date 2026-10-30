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

__all__ = [
    "SupportsEnvExec",
    "SupportsServiceResolution",
    "SupportsUserOverride",
    "exec_tool_for_service",
    "first_env_exec_tool",
]


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


@runtime_checkable
class SupportsServiceResolution(SupportsEnvExec, Protocol):
    """A per-trial compose executor that can resolve sibling-service containers.

    A separate-verifier / ``service=`` / sidecar-artifact grade runs against a
    service other than the agent's: it needs the name of that sibling's
    container and an executor bound to it. A tool offers this capability
    structurally — by naming its own resolved container and by minting a sibling
    executor for another service of the same per-trial compose stack — not by
    belonging to one concrete wrapper class. Grading (``exec_tool_for_service``
    and the runner's ``_materialise_path``) accepts any implementer, so a
    compose-backed trial grades the same whether its agent ran on the dedicated
    ``docker exec`` wrapper or the session-lifetime shell on its compose backend.
    """

    @property
    def container_name(self) -> str | None:
        """The container this tool execs into, or ``None`` when none is resolvable.

        ``None`` means no per-trial container can be named yet: the tool has not
        started, or it runs against a local (non-compose) environment that has
        no sibling-service containers.
        """
        ...

    def for_service(self, service: str) -> SupportsServiceResolution:
        """A sibling executor bound to another service of the same per-trial stack."""
        ...


@runtime_checkable
class SupportsUserOverride(SupportsEnvExec, Protocol):
    """A per-trial executor that can mint a sibling running as a chosen user.

    A pack may declare that its verifier runs as a different user than its
    agent — typically ``root``, so the suite can read an oracle the agent must
    not see and write a reward the agent must not be able to forge. Grading
    needs an executor with that identity without disturbing the agent's own
    session, so the capability is a sibling-minting seam, like
    :class:`SupportsServiceResolution`, rather than a setter.

    Offered structurally: grading accepts any implementer, and a tool that does
    not implement it simply cannot honour a user request — which the caller
    reports rather than silently grading as the wrong user.
    """

    def for_user(self, user: str) -> SupportsUserOverride:
        """A sibling executor on the same container, running as *user*."""
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


def exec_tool_for_service(
    bash_tool: SupportsEnvExec, service: str | None
) -> tuple[SupportsEnvExec | None, str | None]:
    """Resolve the exec tool for *service*, or the agent's own tool when ``None``.

    ``service`` of ``None`` returns *bash_tool* unchanged — the suite runs in the
    agent's container. A service name returns a sibling exec tool for that
    container of the per-trial compose stack, which only a compose-backed
    executor can provide. Returns ``(tool, None)`` on success or
    ``(None, reason)`` when a sibling cannot be resolved, so both the in-process
    and detached grading paths refuse a separate-verifier run the same way.
    """
    if service is None:
        return bash_tool, None
    if not isinstance(bash_tool, SupportsServiceResolution):
        return None, (
            f"test-execution grading asked to run in service {service!r} but this "
            "trial's env tool is not a compose-backed executor, so a sibling "
            "container cannot be resolved. A separate verifier requires a "
            "docker-compose runtime."
        )
    if bash_tool.container_name is None:
        # No per-trial container is resolvable: the compose wrapper has not
        # started yet (``for_service`` would raise before start()), or the tool
        # runs against a local environment with no sibling services. Reporting it
        # as a clean refusal keeps the caller on its ungradeable path instead of
        # letting a RuntimeError escape.
        return None, (
            f"test-execution grading asked to run in service {service!r} but the "
            "trial's compose env tool has not started (or targets a local "
            "environment), so its sibling container cannot be resolved."
        )
    return bash_tool.for_service(service), None
