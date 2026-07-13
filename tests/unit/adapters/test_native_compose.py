"""Native compose execution contract and artifact-bundling tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.runner.models import InvocationStyle

pytestmark = pytest.mark.unit


def _write_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _make_task(root: Path) -> tuple[NativeAdapter, Path]:
    task_dir = root / "tasks" / "compose_task"
    _write_yaml(
        task_dir / "task.yaml",
        {
            "task_id": "compose_task",
            "name": "Compose task",
            "category": "coding",
            "description": "Edit the project in the sandbox.",
            "initial_user_message": "Fix it.",
            "initial_state": {},
            "tools": {
                "agent": {
                    "enabled": ["bash"],
                    "bash": {
                        "source": {
                            "invocation_style": "docker_compose_exec",
                            "compose_file": "environment/docker-compose.yaml",
                            "service": "main",
                            "tests_dir": "tests",
                            "env_vars": {"PROJECT_MODE": "test"},
                        }
                    },
                },
                "user": {"enabled": []},
            },
            "user_simulator": {"mode": "scripted", "scripted_flow": []},
            "grading": "grading.yaml",
        },
    )
    _write_yaml(
        task_dir / "grading.yaml",
        {
            "grading_method": "test_execution",
            "combine": {
                "method": "weighted",
                "weights": {"custom_checks": 1.0},
                "pass_threshold": 0.5,
            },
        },
    )
    _write_yaml(
        task_dir / "environment" / "docker-compose.yaml",
        {
            "services": {
                "main": {
                    "build": ".",
                    "env_file": ".env",
                }
            }
        },
    )
    (task_dir / "environment" / "Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
    (task_dir / "environment" / ".env").write_text("MODE=test\n", encoding="utf-8")
    (task_dir / "environment" / "solution").mkdir()
    (task_dir / "environment" / "solution" / "oracle.sh").write_text("exit 0\n", encoding="utf-8")
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test.sh").write_text(
        "mkdir -p /logs/verifier\necho 1 > /logs/verifier/reward.txt\n",
        encoding="utf-8",
    )
    return (
        NativeAdapter({"base_dir": str(root), "tasks_glob": "tasks/**/task.yaml"}),
        task_dir,
    )


def test_native_compose_builds_lifecycle_tool_and_bundles_artifacts(tmp_path: Path) -> None:
    adapter, _ = _make_task(tmp_path)

    description = adapter.to_task_description("compose_task")
    tool = description.agent_tools[0]

    assert tool.name == "bash"
    assert tool.parameters["required"] == ["command"]
    assert tool.source is not None
    assert tool.source.invocation_style == InvocationStyle.DOCKER_COMPOSE_EXEC
    assert tool.source.extra == {
        "compose_file": "environment/docker-compose.yaml",
        "task_dir": "__artifacts__",
        "service": "main",
        "tests_dir": "tests",
        "env_vars": {"PROJECT_MODE": "test"},
    }
    assert description.grading.grading_method == "test_execution"
    assert set(description.tool_artifacts) >= {
        "environment/docker-compose.yaml",
        "environment/Dockerfile",
        "environment/.env",
        "tests/test.sh",
    }
    assert "environment/solution/oracle.sh" not in description.tool_artifacts
    assert base64.b64decode(description.tool_artifacts["tests/test.sh"]).startswith(b"mkdir")
    assert adapter.docker_stack_requirements().enable_dind is True


def test_native_task_can_mix_compose_builtin_and_mcp_tools(tmp_path: Path) -> None:
    adapter, task_dir = _make_task(tmp_path)
    task = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    task["tools"]["agent"]["enabled"] = ["bash", "get_secret"]
    task["tools"]["agent"]["mcp_server"] = "mcp_server.py"
    _write_yaml(task_dir / "task.yaml", task)
    (task_dir / "mcp_server.py").write_text("# schema is cached for this unit test\n")
    fixtures = task_dir / "fixtures"
    fixtures.mkdir()
    (fixtures / "tools.json").write_text(
        json.dumps(
            [
                {
                    "name": "get_secret",
                    "description": "Return a secret.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        ),
        encoding="utf-8",
    )

    description = adapter.to_task_description("compose_task")
    tools = {tool.name: tool for tool in description.agent_tools}
    assert tools["bash"].source is not None
    assert tools["bash"].source.invocation_style == InvocationStyle.DOCKER_COMPOSE_EXEC
    assert tools["get_secret"].source is not None
    assert tools["get_secret"].source.invocation_style == InvocationStyle.MCP_SERVER
    assert tools["get_secret"].source.mcp_server_script == "mcp_server.py"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_compose", "Compose file not found"),
        ("missing_service", "was not found"),
        ("missing_tests", "Tests directory not found"),
        ("missing_test_script", "test_execution grading requires"),
        ("incompatible_grading", "must declare grading_method: test_execution"),
        ("missing_source", "requires an enabled agent tool"),
    ],
)
def test_native_compose_rejects_invalid_contract_early(
    tmp_path: Path, mutation: str, message: str
) -> None:
    adapter, task_dir = _make_task(tmp_path)
    task_data = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    grading_data = yaml.safe_load((task_dir / "grading.yaml").read_text(encoding="utf-8"))
    source = task_data["tools"]["agent"]["bash"]["source"]

    if mutation == "missing_compose":
        source["compose_file"] = "environment/missing.yaml"
    elif mutation == "missing_service":
        source["service"] = "missing"
    elif mutation == "missing_tests":
        source["tests_dir"] = "missing-tests"
    elif mutation == "missing_test_script":
        (task_dir / "tests" / "test.sh").unlink()
    elif mutation == "incompatible_grading":
        grading_data.pop("grading_method")
    elif mutation == "missing_source":
        task_data["tools"]["agent"]["bash"].pop("source")

    _write_yaml(task_dir / "task.yaml", task_data)
    _write_yaml(task_dir / "grading.yaml", grading_data)

    with pytest.raises(ValueError, match=message):
        adapter.to_task_description("compose_task")


def test_native_non_compose_task_does_not_request_dind(tmp_path: Path) -> None:
    adapter, task_dir = _make_task(tmp_path)
    task_data = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    task_data["tools"]["agent"]["bash"].pop("source")
    grading_data = yaml.safe_load((task_dir / "grading.yaml").read_text(encoding="utf-8"))
    grading_data.pop("grading_method")
    _write_yaml(task_dir / "task.yaml", task_data)
    _write_yaml(task_dir / "grading.yaml", grading_data)

    assert adapter.docker_stack_requirements().enable_dind is False
