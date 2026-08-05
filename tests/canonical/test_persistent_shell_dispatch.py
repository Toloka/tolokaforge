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


def test_compose_backend_default_user_is_none():
    """No ``user`` in ``tool_config`` → the compose backend inherits the
    container's default user (no ``--user`` flag on the exec argv).
    Preserves prior behaviour for every current pack."""
    wrapper = _wrapper({"service": "app", "compose_project_prefix": "tolokaforge_"})
    wrapper._trial_id = "abc123"
    session = wrapper._new_session()
    assert isinstance(session, DockerComposeBashSession)
    assert session.user is None


def test_compose_backend_threads_user_from_tool_config():
    """``tool_config.user`` threads through to the ``docker exec --user
    <user>`` flag. Task packs whose grader container runs as root use
    this to drop privileges on the agent-facing exec session."""
    wrapper = _wrapper(
        {
            "service": "app",
            "compose_project_prefix": "tolokaforge_",
            "user": "model",
        }
    )
    wrapper._trial_id = "abc123"
    session = wrapper._new_session()
    assert isinstance(session, DockerComposeBashSession)
    assert session.user == "model"


def test_compose_backend_accepts_uid_gid_user():
    """Numeric ``uid:gid`` is a valid ``docker exec --user`` value —
    useful when the container doesn't have a named user for the
    intended UID."""
    wrapper = _wrapper(
        {
            "service": "app",
            "compose_project_prefix": "tolokaforge_",
            "user": "1000:1000",
        }
    )
    wrapper._trial_id = "abc123"
    session = wrapper._new_session()
    assert session.user == "1000:1000"


def test_compose_backend_popen_argv_includes_user_flag_when_set():
    """The ``--user`` flag lands in the exec argv exactly when
    ``session.user`` is set. Locks the argv shape so a future refactor
    of ``_popen`` doesn't silently drop the flag."""
    from unittest.mock import patch

    # ``slave_fd=0`` is nonsensical as a real PTY fd (it's stdin) but
    # ``subprocess.Popen`` is mocked, so nothing actually reads/writes
    # to it — only the argv the mock was called with matters here.
    session = DockerComposeBashSession("some-container", user="model")
    with patch("subprocess.Popen") as mock_popen:
        try:
            session._popen(cwd=None, slave_fd=0)
        except Exception:
            pass
        assert mock_popen.call_args is not None, "subprocess.Popen was never called"
        argv = mock_popen.call_args.args[0]
        assert "--user" in argv, argv
        assert argv[argv.index("--user") + 1] == "model"

    # Symmetric: absence of ``user`` means no ``--user`` in argv.
    session_default = DockerComposeBashSession("some-container")
    with patch("subprocess.Popen") as mock_popen:
        try:
            session_default._popen(cwd=None, slave_fd=0)
        except Exception:
            pass
        assert mock_popen.call_args is not None, "subprocess.Popen was never called"
        argv = mock_popen.call_args.args[0]
        assert "--user" not in argv, argv
