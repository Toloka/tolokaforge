"""What a pre-run gate reads off a task: read-only, and honest about what it knows.

The inventory is the one answer to "what tools does this task give the agent, and
what does each tool's schema say about its arguments". A gate that has to spawn the
task's MCP server to answer would mutate the very tree it is validating — the two
shipped MCP tasks with no committed ``fixtures/tools.json`` make that reachable, not
theoretical — so the read-only mode answers ``UNKNOWN`` instead.

The replay world is the second answer, and the one thing the gate reads from ``task.yaml``
rather than from the tools: what a golden-action replay would be executed against. Both
are resolved *under an adapter*, and neither is the reading a foreign adapter uses, so
both report themselves unresolvable outside the native one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tolokaforge.adapters._task_loader import (
    build_tool_inventory,
    load_task_yaml,
    replay_world_under_adapter,
    resolve_agent_tool_schemas,
)
from tolokaforge.core.grading.config_validation import ArgumentSchema, ReplayWorld, ToolInventory
from tolokaforge.core.grading.golden_replay import InitialStateSource
from tolokaforge.core.models import TaskConfig
from tolokaforge.runner import tool_factory
from tolokaforge.runner.models import AdapterType

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_EXAMPLES = _REPO / "examples" / "native"

# The two shipped MCP tasks that commit no ``fixtures/tools.json``. A third task
# declares ``mcp_server`` — ``tests/data/tasks/shop_orders_02`` — and does commit one,
# so it is resolvable and cannot stand in for these.
_FIXTURE_LESS_MCP_TASKS = (
    _REPO / "tests/data/projects/food_delivery_2/tasks/order_modify_with_checks/task.yaml",
    _REPO / "tests/data/projects/food_delivery_2/tasks/order_six_items_golden/task.yaml",
)

_NOTES_TASK = _EXAMPLES / "native_shared_domain/dataset/notes/testcases/add_first_note/task.yaml"
_HELPDESK_TASK = _EXAMPLES / "multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
_NO_TOOLS_TASK = _EXAMPLES / "example-microservices-pack/tasks/api_endpoint_add/task.yaml"


class _CountingMCPServerProcess:
    """Stands in for ``MCPServerProcess`` and records every construction.

    Reporting no tools makes :func:`_fetch_mcp_tool_schemas` raise before it can
    write a fixture, so the counter observes the spawn without the live path
    depositing anything in the task tree.
    """

    def __init__(self, *, script_path: str) -> None:
        self.script_path = script_path

    def start(self) -> None: ...

    def send_request(self, method: str, params: dict) -> dict:
        return {"tools": []}

    def stop(self) -> None: ...


@pytest.fixture
def mcp_constructions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Script paths every ``MCPServerProcess`` construction was asked for."""
    recorded: list[str] = []

    def record(*, script_path: str) -> _CountingMCPServerProcess:
        recorded.append(script_path)
        return _CountingMCPServerProcess(script_path=script_path)

    monkeypatch.setattr(tool_factory, "MCPServerProcess", record)
    return recorded


@pytest.mark.parametrize("task_yaml", _FIXTURE_LESS_MCP_TASKS, ids=lambda p: Path(p).parent.name)
def test_an_unresolvable_mcp_task_reports_unknown_and_leaves_the_tree_alone(
    task_yaml: Path, mcp_constructions: list[str]
) -> None:
    """The read-only mode never spawns a server and never writes a fixture.

    Boundary case, standing lock: every other MCP task in the repo ships a
    fixture, so a sweep over real packs would never reach this arm.
    """
    task, task_dir = load_task_yaml(task_yaml)
    fixture = task_dir / "fixtures" / "tools.json"
    assert not fixture.exists(), f"{fixture} exists — this task can no longer prove the claim"

    inventory = build_tool_inventory(task, task_dir)

    assert inventory.known is True
    assert inventory.declared == frozenset(task.tools.agent["enabled"])
    assert inventory.declared, "the task declares no tools, so UNKNOWN would be vacuous"
    assert {tool: inventory.strictness(tool) for tool in inventory.declared} == dict.fromkeys(
        inventory.declared, ArgumentSchema.UNKNOWN
    )
    assert mcp_constructions == []
    assert not fixture.exists()


def test_the_same_task_does_spawn_a_server_when_a_subprocess_is_allowed(
    mcp_constructions: list[str],
) -> None:
    """The counter that reads zero above is wired to the construction that matters."""
    task_yaml = _FIXTURE_LESS_MCP_TASKS[0]
    task, task_dir = load_task_yaml(task_yaml)
    fixture = task_dir / "fixtures" / "tools.json"
    assert not fixture.exists()

    with pytest.raises(RuntimeError, match="returned no tools"):
        resolve_agent_tool_schemas(task, task_dir, allow_subprocess=True)

    assert mcp_constructions == [str(task_dir / task.tools.agent["mcp_server"])]
    assert not fixture.exists()


@pytest.mark.parametrize(
    ("task_yaml", "tool", "expected_class", "expected_properties"),
    [
        pytest.param(
            _HELPDESK_TASK,
            "http_request",
            ArgumentSchema.CLOSED,
            {"data", "headers", "json", "method", "url"},
            id="closed-builtin",
        ),
        pytest.param(
            _NOTES_TASK,
            "add_note",
            ArgumentSchema.OPEN,
            {"title", "body"},
            id="open-mcp-fixture",
        ),
        pytest.param(
            _NOTES_TASK,
            "list_notes",
            ArgumentSchema.OPEN,
            set(),
            id="open-over-no-arguments",
        ),
        pytest.param(
            _FIXTURE_LESS_MCP_TASKS[0],
            "modify_order",
            ArgumentSchema.UNKNOWN,
            set(),
            id="unknown-no-schema",
        ),
    ],
)
def test_strictness_separates_what_a_schema_says_from_whether_there_is_one(
    task_yaml: Path,
    tool: str,
    expected_class: ArgumentSchema,
    expected_properties: set[str],
) -> None:
    """A zero-argument tool is OPEN over the empty set, not UNKNOWN.

    Boundary cases, standing locks: the ``list_notes`` and ``modify_order`` rows
    both look like "no properties" from outside, and classifying on the property
    count instead of on schema presence collapses them into one answer.
    """
    task, task_dir = load_task_yaml(task_yaml)
    inventory = build_tool_inventory(task, task_dir)

    assert tool in inventory.declared
    assert inventory.strictness(tool) is expected_class
    assert inventory.properties(tool) == frozenset(expected_properties)


def test_a_schema_the_task_never_declared_is_not_in_the_inventory() -> None:
    """An MCP fixture lists tools the task does not enable, and they are dropped.

    The notes fixture carries the harness's own ``_tolokaforge_set_state_``. Left
    in ``parameters``, one grading block naming it draws two findings that
    contradict each other: an undeclared-tool error against ``declared``, and an
    argument verdict off a schema the agent is never handed.
    """
    task, task_dir = load_task_yaml(_NOTES_TASK)
    resolved = resolve_agent_tool_schemas(task, task_dir, allow_subprocess=False)
    inventory = build_tool_inventory(task, task_dir)

    assert set(resolved) - inventory.declared, (
        "the fixture resolves nothing outside the declared set, so this pack can no "
        "longer prove the filter is doing anything"
    )
    assert set(inventory.parameters) <= inventory.declared
    assert inventory.strictness("_tolokaforge_set_state_") is ArgumentSchema.UNKNOWN


def test_a_task_that_declares_no_tools_is_known_and_empty() -> None:
    """Declaring nothing is not the same as being unable to report anything.

    Boundary case, standing lock: every tool name a check mentions is wrong against
    this inventory, and unresolvable against the other.
    """
    task, task_dir = load_task_yaml(_NO_TOOLS_TASK)
    inventory = build_tool_inventory(task, task_dir)

    assert inventory.declared == frozenset()
    assert inventory.known is True
    assert inventory != ToolInventory.unresolvable()
    assert ToolInventory.unresolvable().known is False


def _replayable_task(json_db: Any = "initial_state.json", **tools: Any) -> TaskConfig:
    """A task written the way one declaring a golden path is, in memory.

    Written out rather than loaded from a pack, because two of the three ``json_db``
    shapes below ship nowhere: every pack with a reachable golden replay names a file.
    """
    return TaskConfig(
        task_id="replay_probe",
        description="a task a golden replay could be executed against",
        initial_state={"json_db": json_db},
        tools=tools or {"agent": {"enabled": ["place_order"], "mcp_server": "mcp_server.py"}},
    )


@pytest.mark.parametrize(
    ("json_db", "source"),
    [
        pytest.param("initial_state.json", InitialStateSource.JSON_FILE, id="a_path_to_a_file"),
        pytest.param({"widgets": []}, InitialStateSource.INLINE, id="a_mapping_written_inline"),
        pytest.param(None, InitialStateSource.ABSENT, id="no_json_db_at_all"),
    ],
)
def test_a_replay_world_is_the_native_reading_of_a_task_and_no_other(
    json_db: Any, source: InitialStateSource
) -> None:
    """Which shape a task wrote its initial state in decides whether a replay has a world.

    A replay loads a JSON file under the task directory, so the inline row is a world it
    cannot build and the gate refuses the pack for it — collapsing the two into one
    "declared / not declared" answer would let that pack through to the raise.

    Under a foreign adapter the same task resolves nothing: ``initial_state.json_db`` and
    ``tools.agent.mcp_server`` are the *native* reading of a replay world, and reporting
    one the adapter does not use would refuse packs that run fine.
    """
    task = _replayable_task(json_db)

    assert replay_world_under_adapter(task, AdapterType.NATIVE.value) == ReplayWorld(
        initial_state=source, mcp_server=True
    )
    assert replay_world_under_adapter(task, AdapterType.TAU.value) == ReplayWorld.unresolvable()


@pytest.mark.parametrize(
    ("agent", "declared"),
    [
        pytest.param({"mcp_server": "mcp_server.py"}, True, id="a_module_to_import"),
        pytest.param({"enabled": ["place_order"]}, False, id="an_agent_block_naming_none"),
        pytest.param({}, False, id="an_empty_agent_block"),
    ],
)
def test_the_module_half_of_a_replay_world_reads_the_agent_block(
    agent: dict[str, Any], declared: bool
) -> None:
    """``tools.agent.mcp_server`` is read the way ``BaseAdapter.grade`` reads it.

    The empty row is the boundary: ``tools.agent`` is a bare ``dict``, so an author can
    write it empty, and a reading that indexed it would raise inside the gate instead of
    reporting the pack.
    """
    world = replay_world_under_adapter(_replayable_task(agent=agent), AdapterType.NATIVE.value)

    assert world.mcp_server is declared
    assert world.initial_state is InitialStateSource.JSON_FILE
