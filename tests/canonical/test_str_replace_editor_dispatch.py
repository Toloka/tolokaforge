"""Canonical wiring lock for the ``str_replace_editor`` provider config axis (#567).

Daemon-free: asserts only that the wrapper *selects* the right backend from
``tool_config`` and *resolves* the compose container name from the per-trial
project convention. The concrete ``docker exec`` behaviour is proved on a real
daemon in ``tests/integration/test_str_replace_editor_compose.py``.
"""

from __future__ import annotations

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
