"""Canonical wiring lock for the ``str_replace_editor`` provider config axis (#567).

Daemon-free: asserts only that the wrapper *selects* the right backend from
``tool_config`` and *resolves* the compose container name from the per-trial
project convention. The concrete ``docker exec`` behaviour is proved on a real
daemon in ``tests/integration/test_str_replace_editor_compose.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    StrReplaceEditorToolWrapper,
    ToolConfigurationError,
)
from tolokaforge.tools.str_replace_editor import DockerComposeEditor, LocalFilesystemEditor

pytestmark = pytest.mark.canonical


def _wrapper(tool_config: dict | None, trial_id: str = "t0") -> StrReplaceEditorToolWrapper:
    kwargs = {"tool_config": tool_config} if tool_config is not None else {}
    schema = ToolSchema(
        name="str_replace_editor",
        description="x",
        parameters={"type": "object", "properties": {}},
        **kwargs,
    )
    return StrReplaceEditorToolWrapper(schema, trial_id=trial_id)


def test_service_config_selects_compose_backend():
    wrapper = _wrapper({"service": "app", "compose_project_prefix": "foo_"})
    assert isinstance(wrapper._backend, DockerComposeEditor)


def test_no_service_selects_local_backend():
    wrapper = _wrapper(None)
    assert isinstance(wrapper._backend, LocalFilesystemEditor)


def test_container_name_resolved_from_trial_service_and_prefix():
    name = StrReplaceEditorToolWrapper._resolve_container_name(
        trial_id="abc", service="app", project_prefix="foo_"
    )
    assert name == "foo_abc_app"


def test_container_name_sanitises_colon_in_trial_id():
    name = StrReplaceEditorToolWrapper._resolve_container_name(
        trial_id="run:abc", service="main", project_prefix="tbench_"
    )
    assert name == "tbench_run_abc_main"


def test_compose_backend_wires_resolved_name_into_engine():
    wrapper = _wrapper({"service": "app", "compose_project_prefix": "foo_"}, trial_id="abc")
    assert isinstance(wrapper._backend, DockerComposeEditor)
    assert wrapper._backend.container_name == "foo_abc_app"


def test_service_without_prefix_fails_loud():
    with pytest.raises(ToolConfigurationError):
        _wrapper({"service": "app"})


def test_working_root_wires_into_local_backend(tmp_path):
    root = str(tmp_path / "srv" / "agent")
    wrapper = _wrapper({"working_root": root})
    assert isinstance(wrapper._backend, LocalFilesystemEditor)
    assert wrapper._backend._base == Path(root).resolve()


def test_working_root_wires_into_compose_backend():
    wrapper = _wrapper(
        {"service": "app", "compose_project_prefix": "foo_", "working_root": "/srv/agent"}
    )
    assert isinstance(wrapper._backend, DockerComposeEditor)
    assert wrapper._backend.base_path == "/srv/agent"


def test_working_root_defaults_to_work_for_both_backends():
    local = _wrapper(None)
    assert isinstance(local._backend, LocalFilesystemEditor)
    assert local._backend._base == Path("/work").resolve()

    compose = _wrapper({"service": "app", "compose_project_prefix": "foo_"})
    assert isinstance(compose._backend, DockerComposeEditor)
    assert compose._backend.base_path == "/work"


def test_compose_backend_default_user_is_none():
    """No ``user`` in ``tool_config`` → no ``--user`` flag; the editor
    inherits the container's default user. Preserves prior behaviour."""
    wrapper = _wrapper({"service": "app", "compose_project_prefix": "foo_"})
    assert isinstance(wrapper._backend, DockerComposeEditor)
    assert wrapper._backend.user is None


def test_compose_backend_threads_user_from_tool_config():
    """``tool_config.user`` threads through to the ``docker exec --user
    <user>`` flag — symmetric with ``bash_session``."""
    wrapper = _wrapper({"service": "app", "compose_project_prefix": "foo_", "user": "model"})
    assert isinstance(wrapper._backend, DockerComposeEditor)
    assert wrapper._backend.user == "model"


def test_compose_editor_exec_argv_includes_user_flag_when_set():
    """Every editor subprocess invokes ``docker exec [--user U]``. Lock
    the argv shape both ways: user set → ``--user`` present; user unset
    → ``--user`` absent."""
    from unittest.mock import patch

    editor_user = DockerComposeEditor("some-container", "/work", user="model")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = b""
        mock_run.return_value.stderr = b""
        try:
            editor_user._exec("script", "arg1", allow_failure=True)
        except Exception:
            pass
        assert mock_run.call_args is not None, "subprocess.run was never called"
        argv = mock_run.call_args.args[0]
        assert "--user" in argv, argv
        assert argv[argv.index("--user") + 1] == "model"

    editor_default = DockerComposeEditor("some-container", "/work")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = b""
        mock_run.return_value.stderr = b""
        try:
            editor_default._exec("script", "arg1", allow_failure=True)
        except Exception:
            pass
        assert mock_run.call_args is not None, "subprocess.run was never called"
        argv = mock_run.call_args.args[0]
        assert "--user" not in argv, argv
