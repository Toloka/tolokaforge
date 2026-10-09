"""An MCP server under ``_shared/`` imports a pinned tool library (ADR-0056).

The runner extracts ``tool_artifacts`` into the trial's directory and starts each
MCP server as a subprocess of its own interpreter. The script's own directory
(``_shared/``) is what Python puts on that subprocess's path, so a library
merged at the artefact root resolves only because the runner puts the root
ahead of ``PYTHONPATH``. The end-to-end case drives a real shared-domain pack
through :meth:`NativeAdapter.describe_task`, ``RegisterTrial`` and
``ExecuteTool`` against an in-process runner and a real server subprocess.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.utils.runner_requests import execute_request, register_request, trial_spec_json
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import plugin_registry
from tolokaforge.core.plugin_registry import TOOL_LIBRARIES_GROUP
from tolokaforge.core.tool_libraries import InMemoryToolLibrary
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.tool_factory import MCPServerProcess

pytestmark = pytest.mark.unit

_LIBRARY = InMemoryToolLibrary(
    "acme_tools",
    "1.2.0",
    shared={
        "acme_tools/__init__.py": b"from acme_tools.core import answer\n",
        "acme_tools/core.py": b"def answer():\n    return 42\n",
    },
)

_SERVER = """\
import acme_tools

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "acme")


@registry.tool("Answer from the shared library.")
def library_answer(data: dict) -> dict:
    return {"answer": acme_tools.answer(), "module": acme_tools.__file__}


if __name__ == "__main__":
    mcp.run(transport="stdio")
"""


class _FakeEntryPoint:
    name = "acme_tools"
    dist = None

    def load(self) -> object:
        return _LIBRARY


@pytest.fixture
def library_installed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``acme_tools`` registered under ``tolokaforge.tool_libraries``; other groups real."""
    real_entry_points = importlib.metadata.entry_points

    def entry_points(**kwargs: Any) -> Any:
        if kwargs.get("group") == TOOL_LIBRARIES_GROUP:
            return [_FakeEntryPoint()]
        return real_entry_points(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    plugin_registry._clear_discovery_cache()
    yield
    plugin_registry._clear_discovery_cache()


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _shared_domain_pack(root: Path) -> NativeAdapter:
    """A shared-domain pack whose ``_shared/mcp_server.py`` imports ``acme_tools``."""
    domain = root / "acme_domain"
    _write_yaml(
        domain / "_shared" / "domain.yaml",
        {
            "category": "tool_use",
            "tools": {"agent": {"mcp_server": "mcp_server.py", "enabled": ["library_answer"]}},
            "tool_libraries": [{"name": "acme_tools", "version": "1.2.0"}],
            "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
            "system_prompt": "system_prompt.md",
        },
    )
    (domain / "_shared" / "mcp_server.py").write_text(_SERVER)
    (domain / "_shared" / "system_prompt.md").write_text("Answer the user.\n")
    (domain / "fixtures").mkdir()
    (domain / "fixtures" / "tools.json").write_text(
        json.dumps(
            [
                {
                    "name": "library_answer",
                    "description": "Answer from the shared library.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        )
    )
    case = domain / "testcases" / "case_a"
    _write_yaml(
        case / "task.yaml",
        {
            "task_id": "acme_case_a",
            "description": "Ask the library.",
            "domain": "../../_shared/domain.yaml",
            "initial_state": {"json_db": "initial_state.json"},
            "grading": "grading.yaml",
        },
    )
    (case / "initial_state.json").write_text('{"items": []}')
    (case / "grading.yaml").write_text("{}\n")
    return NativeAdapter({"base_dir": str(root), "tasks_glob": "**/testcases/*/task.yaml"})


def test_a_server_under_shared_imports_the_library_from_the_artefact_root(
    tmp_path: Path, library_installed: None, runner_service: Any, mock_grpc_context: Any
) -> None:
    description = _shared_domain_pack(tmp_path).describe_task("acme_case_a")
    assert description.metadata["mcp_server_ref"] == "_shared/mcp_server.py"
    assert "acme_tools/core.py" in description.tool_artifacts
    trial_id = "acme_case_a:0"

    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(description.model_dump(mode="json"), trial_id=trial_id),
            trial_id=trial_id,
        ),
        mock_grpc_context,
    )
    assert registered.success, registered.error
    try:
        response = runner_service.ExecuteTool(
            execute_request(trial_id, "library_answer"), mock_grpc_context
        )

        assert response.status == pb2.EXECUTION_STATUS_SUCCESS, response.error_message
        result = json.loads(response.output)
        assert result["answer"] == 42
        artefact_root = Path(runner_service._artifact_dirs[trial_id])
        assert Path(result["module"]).resolve().is_relative_to(artefact_root.resolve())
    finally:
        runner_service.CleanupTrial(pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context)


def test_the_artefact_root_goes_ahead_of_an_inherited_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/inherited")
    server = MCPServerProcess(script_path="mcp_server.py", python_path=("/artefacts",))

    environment = server._subprocess_environment()

    assert environment is not None
    assert environment["PYTHONPATH"] == os.pathsep.join(["/artefacts", "/inherited"])
    assert environment["HOME"] == os.environ["HOME"]


def test_without_an_inherited_pythonpath_the_root_is_the_whole_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    server = MCPServerProcess(script_path="mcp_server.py", python_path=("/artefacts",))

    environment = server._subprocess_environment()

    assert environment is not None
    assert environment["PYTHONPATH"] == "/artefacts"


def test_a_server_without_artefacts_inherits_the_environment_unchanged() -> None:
    assert MCPServerProcess(script_path="mcp_server.py")._subprocess_environment() is None
