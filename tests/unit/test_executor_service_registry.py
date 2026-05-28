"""Executor service uses the unified builtin registry.

Closes #124: per-task tool config is plumbed via ``ToolDefinition.config_json``
(JSON envelope mirroring runner-side ``ToolSchema.tool_config``), and the
executor serves every name the registry knows about (no exclusions).

Closes #121: drift-detection enforces equality between the registry and
the executor's served set.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tolokaforge.executor import executor_pb2
from tolokaforge.executor.service import ExecutorServiceImpl
from tolokaforge.tools.builtin import registry as builtin_registry

pytestmark = pytest.mark.unit


def _register(
    tool_specs: list[tuple[str, dict | None]],
) -> tuple[ExecutorServiceImpl, executor_pb2.RegisterToolsResponse]:
    svc = ExecutorServiceImpl()
    req = executor_pb2.RegisterToolsRequest()
    req.trial_id = "test-trial"
    req.env_config.agent_visible_dir = "/tmp/agent-visible"
    req.env_config.json_db_url = "http://json-db:8000"
    req.env_config.rag_service_url = "http://rag:8001"
    req.env_config.mock_web_url = "http://mock-web:8080"
    for name, config in tool_specs:
        td = req.tools.add()
        td.name = name
        if config is not None:
            td.config_json = json.dumps(config)
    return svc, svc.RegisterTools(req, MagicMock())


def test_executor_registers_every_supported_builtin():
    """Every name the unified registry advertises must instantiate cleanly
    with no per-task config (mobile is the only one that *requires* config,
    so it's not included here — see test_executor_registers_mobile_with_config)."""
    supported = builtin_registry.list_builtins() - {"mobile"}
    _, response = _register([(name, None) for name in sorted(supported)])
    assert response.success, response.error
    assert response.num_tools_registered == len(supported)


def test_executor_serves_every_registry_builtin():
    """Drift detection (#121): equality, not subset.

    Anything less means an executor-side gap will silently widen as the
    registry grows. Construct the executor with mobile-friendly config
    so every tool can instantiate.
    """
    specs: list[tuple[str, dict | None]] = []
    for name in sorted(builtin_registry.list_builtins()):
        if name == "mobile":
            specs.append((name, {"apps": {"CityMap": "http://mock-web:8080/citymap/"}}))
        else:
            specs.append((name, None))
    _, response = _register(specs)
    assert response.success, response.error
    assert response.num_tools_registered == len(builtin_registry.list_builtins())


def test_executor_registers_mobile_with_apps_config():
    """#124: ``mobile`` requires ``apps`` to be passed through. The
    JSON envelope on ``ToolDefinition.config_json`` carries it."""
    apps = {
        "CityMap": "http://mock-web:8080/task/mobile/app_citymap/",
        "Notepad": "http://mock-web:8080/task/mobile/app_notepad/",
    }
    svc, response = _register([("mobile", {"apps": apps})])
    assert response.success, response.error
    assert response.num_tools_registered == 1
    registry = svc.trial_registries["test-trial"]
    mobile_tool = registry.get_tool("mobile")
    assert mobile_tool is not None
    # The apps mapping is what makes mobile usable — verify it landed.
    assert mobile_tool.apps == apps


def test_executor_registers_browser_with_initial_url():
    """Per-task ``browser.initial_url`` flows through the same envelope."""
    svc, response = _register([("browser", {"initial_url": "http://mock-web:8080/task/browser/"})])
    assert response.success, response.error
    registry = svc.trial_registries["test-trial"]
    browser_tool = registry.get_tool("browser")
    assert browser_tool is not None
    assert browser_tool.initial_url == "http://mock-web:8080/task/browser/"


def test_executor_rejects_unknown_config_key():
    """Unknown keys raise rather than silently dropping — same fail-loud
    contract as runner-side ``BuiltinGenericToolWrapper``."""
    _, response = _register([("browser", {"not_a_real_kwarg": "x"})])
    assert not response.success
    assert "not_a_real_kwarg" in response.error
    assert "accepted" in response.error.lower()


def test_executor_rejects_invalid_json_config():
    svc = ExecutorServiceImpl()
    req = executor_pb2.RegisterToolsRequest()
    req.trial_id = "test-trial"
    req.env_config.agent_visible_dir = "/tmp/agent-visible"
    td = req.tools.add()
    td.name = "browser"
    td.config_json = "not valid json"
    response = svc.RegisterTools(req, MagicMock())
    assert not response.success
    assert "config_json" in response.error.lower() or "json" in response.error.lower()


def test_executor_rejects_config_collision_with_runtime_kwargs():
    """A tool's runtime kwargs (e.g. ``base_path`` for read_file) must not
    be silently overridden by config_json — that would mask a YAML mistake
    and could leak agent-visible paths."""
    _, response = _register([("read_file", {"base_path": "/etc"})])
    assert not response.success
    assert "base_path" in response.error


def test_executor_warns_on_unknown_tool_request():
    """A trial requesting a tool the registry doesn't know about gets a
    success response (legacy contract) but a zero registration count for
    that name. The warning is logged but not raised."""
    _, response = _register([("not_a_real_tool", None)])
    assert response.success
    assert response.num_tools_registered == 0
