"""Canonical wiring lock for the ``bash_session`` provider config axis (#566).

Daemon-free: asserts only that the wrapper *selects* the right backend from
``tool_config`` and *resolves* the compose container name from the per-trial
project convention. The concrete ``docker exec`` behaviour is proved on a real
daemon in ``tests/integration/test_persistent_shell_compose.py``.
"""

from __future__ import annotations

import pytest

from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import PersistentShellToolWrapper
from tolokaforge.tools.persistent_shell import DockerComposeBashSession, LocalBashSession

pytestmark = pytest.mark.canonical


def _wrapper(tool_config: dict | None) -> PersistentShellToolWrapper:
    kwargs = {"tool_config": tool_config} if tool_config is not None else {}
    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
        **kwargs,
    )
    return PersistentShellToolWrapper(schema)


def test_service_config_selects_compose_backend():
    wrapper = _wrapper({"service": "app", "compose_project_prefix": "tolokaforge_"})
    wrapper._trial_id = "t0"
    session = wrapper._new_session()
    assert isinstance(session, DockerComposeBashSession)


def test_no_service_selects_local_backend():
    wrapper = _wrapper(None)
    wrapper._trial_id = "t0"
    session = wrapper._new_session()
    assert isinstance(session, LocalBashSession)


def test_container_name_resolved_from_trial_service_and_prefix():
    name = PersistentShellToolWrapper._resolve_container_name(
        trial_id="abc123", service="app", project_prefix="tolokaforge_"
    )
    assert name == "tolokaforge_abc123_app"


def test_container_name_sanitises_colon_in_trial_id():
    name = PersistentShellToolWrapper._resolve_container_name(
        trial_id="run:abc", service="main", project_prefix="tbench_"
    )
    assert name == "tbench_run_abc_main"


def test_service_without_prefix_fails_loud():
    from tolokaforge.runner.tool_factory import ToolConfigurationError

    with pytest.raises(ToolConfigurationError):
        _wrapper({"service": "app"})


def test_compose_backend_wires_resolved_name_into_session():
    wrapper = _wrapper({"service": "app", "compose_project_prefix": "tolokaforge_"})
    wrapper._trial_id = "abc123"
    session = wrapper._new_session()
    assert isinstance(session, DockerComposeBashSession)
    assert session.container_name == "tolokaforge_abc123_app"
