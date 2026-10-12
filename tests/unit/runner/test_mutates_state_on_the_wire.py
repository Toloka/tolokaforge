"""``mutates_state`` crosses every tool wire as its author declares it (ADR-0057).

A tool declares it through ``DomainToolRegistry.tool(..., mutates_state=...)``,
which sets MCP's ``readOnlyHint`` annotation on the server. The native adapter
reads the annotation back from a real server's ``tools/list`` into
``ToolSchema.mutates_state`` and into the ``fixtures/tools.json`` cache; the
runner returns the flag in ``RegisterTrialResponse.tool_schemas``; and the
engine's gRPC client hands it on. A tool that declares nothing carries no
annotation, no key in the JSON wire and no field set on the proto wire.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.utils.runner_requests import register_request, trial_spec_json
from tests.utils.servicer_runtime import ServicerStub
from tolokaforge.adapters._task_loader import _fetch_mcp_tool_schemas, _load_rich_tool_schemas
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import tool_factory
from tolokaforge.runner.models import TaskDescription, ToolSchema

pytestmark = pytest.mark.unit

_SERVER = """\
from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "records")


@registry.tool("Read one record.", mutates_state=False)
def read_record(data: dict, record_id: str) -> dict:
    return {"id": record_id}


@registry.tool("Write one record.", mutates_state=True)
def write_record(data: dict, record_id: str) -> dict:
    data.setdefault("records", []).append({"id": record_id})
    return {"ok": True}


@registry.tool("A tool that declares nothing.")
def legacy_tool(data: dict) -> dict:
    return {}


if __name__ == "__main__":
    mcp.run(transport="stdio")
"""

_DECLARED = {"read_record": False, "write_record": True, "legacy_tool": None}


def _records_pack(root: Path) -> Path:
    """A flat native pack whose server declares two tools and leaves one undeclared.

    No ``fixtures/tools.json``: the adapter asks the live server and caches the answer.
    """
    task_dir = root / "tasks" / "records"
    task_dir.mkdir(parents=True)
    (task_dir / "mcp_server.py").write_text(_SERVER)
    (task_dir / "initial_state.json").write_text('{"records": []}')
    (task_dir / "grading.yaml").write_text("{}\n")
    (task_dir / "system_prompt.md").write_text("Keep records.\n")
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "records",
                "description": "Keep records.",
                "category": "tool_use",
                "initial_state": {"json_db": "initial_state.json"},
                "tools": {"agent": {"mcp_server": "mcp_server.py", "enabled": list(_DECLARED)}},
                "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
                "system_prompt": "system_prompt.md",
                "grading": "grading.yaml",
            }
        )
    )
    return task_dir


@pytest.fixture(scope="module")
def records_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The module's one records pack: built once, shared by every test that only reads it."""
    root = tmp_path_factory.mktemp("records")
    _records_pack(root)
    return root


def _records_adapter(root: Path) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(root), "tasks_glob": "tasks/**/task.yaml"})


@pytest.fixture(scope="module")
def records_description(records_root: Path) -> TaskDescription:
    """The module's one live description: spawns the server once and writes the cache."""
    description = _records_adapter(records_root).describe_task("records")
    assert (records_root / "tasks" / "records" / "fixtures" / "tools.json").exists()
    return description


def test_the_annotation_round_trips_through_a_real_server(records_root: Path) -> None:
    schemas = _fetch_mcp_tool_schemas(records_root / "tasks" / "records" / "mcp_server.py")

    assert schemas["read_record"]["mutates_state"] is False
    assert schemas["write_record"]["mutates_state"] is True
    assert "mutates_state" not in schemas["legacy_tool"]


class _ListingServer:
    """Stands in for ``MCPServerProcess`` and answers ``tools/list`` with a fixed listing."""

    tools: list[dict[str, Any]] = []

    def __init__(self, *, script_path: str) -> None:
        self.script_path = script_path

    def start(self) -> None: ...

    def send_request(self, method: str, params: dict) -> dict:
        assert method == "tools/list"
        return {"tools": self.tools}

    def stop(self) -> None: ...


def _listing(monkeypatch: pytest.MonkeyPatch, tools: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(_ListingServer, "tools", tools)
    monkeypatch.setattr(tool_factory, "MCPServerProcess", _ListingServer)


@pytest.mark.parametrize(
    "annotations",
    [
        pytest.param([], id="annotations-is-a-list"),
        pytest.param({"readOnlyHint": "false"}, id="read-only-hint-is-a-string"),
        pytest.param({"readOnlyHint": 0}, id="read-only-hint-is-an-int"),
    ],
)
def test_a_malformed_read_only_hint_leaves_the_tool_undeclared(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, annotations: Any
) -> None:
    """Only a bool under an ``annotations`` mapping is a declaration.

    ``annotations: []`` used to raise ``AttributeError`` outside the function's
    ``RuntimeError`` contract, and the string ``"false"`` used to record the tool
    as declared read-only. Either shape now warns, names the tool, and leaves
    ``mutates_state`` undeclared; a well-formed sibling is still read.
    """
    _listing(
        monkeypatch,
        [
            {"name": "odd", "annotations": annotations},
            {"name": "fine", "annotations": {"readOnlyHint": True}},
            {"name": "silent"},
        ],
    )

    with caplog.at_level(logging.WARNING):
        schemas = _fetch_mcp_tool_schemas(Path("/srv/mcp_server.py"))

    assert "mutates_state" not in schemas["odd"]
    assert "mutates_state" not in schemas["silent"]
    assert schemas["fine"]["mutates_state"] is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("'odd'" in w and "readOnlyHint" in w for w in warnings), warnings
    assert not any("'fine'" in w or "'silent'" in w for w in warnings), warnings


def _cache(task_dir: Path, entries: list[dict[str, Any]]) -> Path:
    path = task_dir / "fixtures" / "tools.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))
    return path


def test_a_committed_cache_without_the_key_warns_and_names_the_regeneration_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A committed ``fixtures/tools.json`` wins over the server and is never
    refreshed, so adding ``mutates_state=`` to a tool's decorator changes nothing
    on the wire. The load warns once, naming the file and the tools, and says
    how to regenerate; the cache is read as it is and left alone.
    """
    task_dir = _records_pack(tmp_path)
    before = _cache(
        task_dir,
        [
            {"name": "read_record", "description": "d", "parameters": {}},
            {"name": "write_record", "description": "d", "parameters": {}, "mutates_state": True},
        ],
    ).read_text()

    with caplog.at_level(logging.WARNING):
        schemas = _load_rich_tool_schemas(
            task_dir, task_dir / "mcp_server.py", allow_subprocess=False
        )

    assert "mutates_state" not in schemas["read_record"]
    assert schemas["write_record"]["mutates_state"] is True
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert str(task_dir / "fixtures" / "tools.json") in warnings[0]
    assert "read_record" in warnings[0] and "write_record" not in warnings[0].split(";")[0]
    assert "regenerated" in warnings[0] and "--dry-run" in warnings[0]
    assert (task_dir / "fixtures" / "tools.json").read_text() == before


def test_a_committed_cache_declaring_every_tool_loads_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    task_dir = _records_pack(tmp_path)
    _cache(
        task_dir,
        [
            {"name": "read_record", "description": "d", "parameters": {}, "mutates_state": False},
            {"name": "write_record", "description": "d", "parameters": {}, "mutates_state": True},
        ],
    )

    with caplog.at_level(logging.WARNING):
        schemas = _load_rich_tool_schemas(
            task_dir, task_dir / "mcp_server.py", allow_subprocess=False
        )

    assert {n: e["mutates_state"] for n, e in schemas.items()} == {
        "read_record": False,
        "write_record": True,
    }
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_the_native_adapter_reads_it_into_the_schema_and_the_cache(
    records_root: Path, records_description: TaskDescription
) -> None:
    declared = {tool.name: tool.mutates_state for tool in records_description.agent_tools}
    cached = json.loads(
        (records_root / "tasks" / "records" / "fixtures" / "tools.json").read_text()
    )

    assert declared == _DECLARED
    by_name = {entry["name"]: entry for entry in cached}
    assert by_name["read_record"]["mutates_state"] is False
    assert by_name["write_record"]["mutates_state"] is True
    assert "mutates_state" not in by_name["legacy_tool"]


def test_the_cache_answers_a_second_description_the_same_way(
    records_root: Path, records_description: TaskDescription
) -> None:
    again = _records_adapter(records_root).describe_task("records")

    assert {tool.name: tool.mutates_state for tool in again.agent_tools} == _DECLARED


def test_an_undeclared_tool_dumps_without_the_field() -> None:
    undeclared = ToolSchema(name="t", description="d", parameters={})
    declared = ToolSchema(name="t", description="d", parameters={}, mutates_state=False)

    assert "mutates_state" not in undeclared.model_dump()
    assert "mutates_state" not in json.loads(undeclared.model_dump_json())
    assert json.loads(declared.model_dump_json())["mutates_state"] is False
    assert ToolSchema.model_validate(json.loads(declared.model_dump_json())) == declared


def test_the_description_json_carries_only_declared_flags(
    records_description: TaskDescription,
) -> None:
    wire = json.loads(records_description.model_dump_json())
    by_name = {tool["name"]: tool for tool in wire["agent_tools"]}

    assert by_name["read_record"]["mutates_state"] is False
    assert by_name["write_record"]["mutates_state"] is True
    assert "mutates_state" not in by_name["legacy_tool"]


def _register(description: TaskDescription, trial_id: str) -> pb2.RegisterTrialRequest:
    return register_request(
        trial_spec_json(description.model_dump(mode="json"), trial_id=trial_id),
        trial_id=trial_id,
    )


def test_register_trial_returns_the_declared_flag_on_the_proto_wire(
    records_description: TaskDescription, runner_service: Any, mock_grpc_context: Any
) -> None:
    trial_id = "records:0"
    response = runner_service.RegisterTrial(
        _register(records_description, trial_id), mock_grpc_context
    )
    try:
        assert response.success, response.error
        by_name = {schema.name: schema for schema in response.tool_schemas}

        assert by_name["read_record"].HasField("mutates_state")
        assert by_name["read_record"].mutates_state is False
        assert by_name["write_record"].mutates_state is True
        assert not by_name["legacy_tool"].HasField("mutates_state")
    finally:
        runner_service.CleanupTrial(pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context)


def test_the_engine_client_hands_the_flag_on(
    records_description: TaskDescription, runner_service: Any, mock_grpc_context: Any
) -> None:
    trial_id = "records:1"
    client = GrpcRunnerClient(runner_address="unused:0")
    client.stub = ServicerStub(runner_service, mock_grpc_context)
    spec_json = trial_spec_json(records_description.model_dump(mode="json"), trial_id=trial_id)

    result = client.register_trial(trial_id, spec_json)
    try:
        assert result["success"], result["error"]
        assert {s["name"]: s["mutates_state"] for s in result["tool_schemas"]} == _DECLARED
    finally:
        runner_service.CleanupTrial(pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context)
