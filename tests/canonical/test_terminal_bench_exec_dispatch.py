"""Canonical wiring lock for :class:`DockerComposeExecToolWrapper` (#1045).

The wrapper does not own the compose stack — the per-trial runtime does. Its
one responsibility is to ``docker exec`` into the container that runtime brought
up. That target container's name is a function of ``trial_id``, ``service``, and
the compose project prefix, computed by
:func:`tolokaforge.runner.compose_naming.compose_container_name` — which is also
what the host-side materialiser writes into the trial's ``.env`` for
``${TOLOKAFORGE_TRIAL_SLUG}``. Both sides must agree on that string, or the
wrapper execs into "no such container" while the stack is happily up. This
file pins:

* the argv shape at the ``subprocess.run`` boundary (no ``docker compose``, no
  ``-p``, no ``-f``, no ``exec -T``); and
* the equivalence between the host-side ``container_name:`` the adapter's
  compose synthesiser emits (once ``${TOLOKAFORGE_TRIAL_SLUG}`` resolves) and
  the runner-side resolution the wrapper does at ``start()`` time.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from tolokaforge_adapter_terminal_bench.compose_synthesis import (
    _TOLOKAFORGE_TRIAL_SLUG_PLACEHOLDER,
    PROJECT_PREFIX,
)

from tolokaforge.runner.compose_naming import compose_container_name, compose_trial_slug
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    ToolLifecycleContext,
)

pytestmark = pytest.mark.canonical


def _wrapper() -> DockerComposeExecToolWrapper:
    schema = ToolSchema(
        name="bash",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    return DockerComposeExecToolWrapper(
        tool_schema=schema,
        service="main",
        compose_project_prefix="tbench_",
    )


class _StubCompletedProcess:
    """Enough of :class:`subprocess.CompletedProcess` for ``_exec_sync``."""

    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def test_exec_argv_pins_docker_exec_shape():
    """Argv is exactly ``docker exec -i <container> bash -c <cmd>`` — no
    ``docker compose``, no ``-p``/``-f``, no ``exec -T``. Any drift would
    break the equivalence with the host-side compose project."""
    wrapper = _wrapper()
    wrapper.start(ToolLifecycleContext(trial_id="task-1:0"))

    with patch("subprocess.run", return_value=_StubCompletedProcess()) as run_mock:
        wrapper._exec_sync("echo hi", 30.0)

    assert run_mock.call_count == 1
    argv = run_mock.call_args.args[0]
    assert argv == [
        "docker",
        "exec",
        "-i",
        "tbench_task-1_0_main",
        "bash",
        "-c",
        "echo hi",
    ]


def test_container_name_matches_host_side_synthesis():
    """The runner-side resolution and the host-side ``container_name:`` the
    adapter's compose synthesiser emits (once ``${TOLOKAFORGE_TRIAL_SLUG}``
    resolves from the trial's ``.env``) are the same string."""
    trial_id = "task-1:0"
    service = "main"
    host_container_name_template = (
        f"{PROJECT_PREFIX}{_TOLOKAFORGE_TRIAL_SLUG_PLACEHOLDER}_{service}"
    )
    # Compose interpolates ``${TOLOKAFORGE_TRIAL_SLUG}`` from the trial's
    # ``.env`` at ``up`` time — the reserved variable is exactly
    # ``compose_trial_slug(trial_id)``.
    host_resolved = host_container_name_template.replace(
        _TOLOKAFORGE_TRIAL_SLUG_PLACEHOLDER, compose_trial_slug(trial_id)
    )
    runner_resolved = compose_container_name(trial_id, service, PROJECT_PREFIX)
    assert host_resolved == runner_resolved == "tbench_task-1_0_main"


def test_start_runs_no_subprocess():
    """``start()`` records the trial id and resolves the container name; it
    must not shell out (the per-trial runtime brings the stack up)."""
    wrapper = _wrapper()
    with patch("subprocess.run") as run_mock, patch("subprocess.Popen") as popen_mock:
        wrapper.start(ToolLifecycleContext(trial_id="task-1:0"))
    run_mock.assert_not_called()
    popen_mock.assert_not_called()
    assert wrapper._container == "tbench_task-1_0_main"
