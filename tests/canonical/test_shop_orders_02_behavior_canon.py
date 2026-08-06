"""Behavior canonical tests for shop_orders_02.

Covers seven layers:

  1. MCP tools        — TOOLS[name].invoke() works correctly (grading-mode, no subprocess).
  2. State mutations  — tools mutate the data dict as documented.
  3. Grading pipeline — GradingEngine scores passing / failing trajectories.
  4. MCP transport    — stdio JSON-RPC subprocess (real agent path) works end-to-end.
  5. Adapter integration — NativeAdapter.grade() builds GradingEngine via the same
                           code path the Orchestrator uses, not directly.
  6. Trajectory YAML  — OutputWriter.write_trajectory() → YAML → reload preserves
                        tool_calls, arguments, and metadata needed for grading.
  7. Wrapper contract — the production MCPServerToolWrapper reports the MCP
                        protocol's isError flag beside the output text.

All seven layers operate exclusively within tests/data/tasks/shop_orders_02/ —
the self-contained mcp_server.py there mirrors the production implementation so
tests never depend on tasks/tool_use/shop_orders_02/.
"""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from tests.utils.recorded_calls import records_from_messages
from tolokaforge.adapters.base import AdapterEnvironment
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.trace_event_kind import TraceEventKind
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.models import (
    GradingConfig,
    InitialStateConfig,
    Message,
    MessageRole,
    ToolCall,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output_writer import OutputWriter
from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.models import ToolSchema as RunnerToolSchema
from tolokaforge.runner.tool_factory import MCPServerToolWrapper

pytestmark = pytest.mark.canonical

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _fresh_data(task_dir: Path) -> dict:
    """Return a new deep copy of initial_state.json on every call."""
    return json.loads((task_dir / "initial_state.json").read_text())


def _load_grading_config(task_dir: Path) -> GradingConfig:
    raw = yaml.safe_load((task_dir / "grading.yaml").read_text())
    return GradingConfig(**raw)


def _build_engine(task_dir: Path) -> GradingEngine:
    return GradingEngine(
        grading_config=_load_grading_config(task_dir),
        task_domain="tool_use",
        task_dir=task_dir,
        task_initial_state=InitialStateConfig(json_db="initial_state.json"),
        task_mcp_server="mcp_server.py",
    )


class _McpSubprocess:
    """Minimal stdio JSON-RPC client for an MCP server subprocess.

    Mirrors the essentials of ``MCPServerProcess`` from ``runner/tool_factory.py``
    and adds the record-parsing helpers ``TestShopOrders02McpTransport`` asserts
    against. Serving those cases is its whole reason to exist — importing the
    runner package here is fine, and ``TestShopOrders02McpWrapperContract`` below
    drives the production ``MCPServerToolWrapper``, so a case about wrapper
    behaviour belongs there rather than here.
    """

    def __init__(self, script_path: str) -> None:
        self._script = script_path
        self._proc: subprocess.Popen | None = None
        self._req_id = 0

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, self._script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # MCP initialization handshake
        self._send(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        )
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._proc.stdin.write(json.dumps(notification) + "\n")
        self._proc.stdin.flush()

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def _send(self, method: str, params: dict) -> dict:
        self._req_id += 1
        req = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
        self._proc.stdin.write(json.dumps(req) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed connection")
        resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError(f"JSON-RPC error: {resp['error']}")
        return resp.get("result", {})

    def call_tool(self, name: str, arguments: dict) -> dict | list:
        """Call a tool and return the parsed result.

        FastMCP serialises list-returning tools as one content item per element,
        so we must collect all items when there are multiple content entries.
        """
        raw = self._send("tools/call", {"name": name, "arguments": arguments})
        content = raw.get("content", [])
        if not content or not isinstance(content, list):
            return raw
        if len(content) == 1:
            return json.loads(content[0].get("text", "{}"))
        return [json.loads(item.get("text", "{}")) for item in content]

    def list_tools(self) -> list[str]:
        raw = self._send("tools/list", {})
        return [t["name"] for t in raw.get("tools", [])]

    def get_state(self) -> dict:
        raw = self._send(
            "tools/call",
            {"name": "_tolokaforge_get_state_", "arguments": {}},
        )
        content = raw.get("content", [])
        return json.loads(content[0].get("text", "{}")) if content else {}

    def reset_state(self, state: dict) -> None:
        self._send(
            "tools/call",
            {"name": "_tolokaforge_set_state_", "arguments": {"state_json": json.dumps(state)}},
        )


def _make_passing_trajectory() -> Trajectory:
    """Minimal passing trajectory: all required_actions + all communicate_info."""
    messages = [
        Message(
            role=MessageRole.USER,
            content=(
                "Hi! I'm customer C-101 (Alex Torres). I'd like to buy "
                "1 × Wireless Headphones and 2 × USB-C Hub 7-Port."
            ),
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="Checking the catalog and your account.",
            tool_calls=[
                ToolCall(id="tc-1", name="list_products", arguments={}),
                ToolCall(
                    id="tc-2",
                    name="get_customer",
                    arguments={"customer_id": "C-101"},
                ),
            ],
        ),
        Message(role=MessageRole.TOOL, content="[products]", tool_call_id="tc-1"),
        Message(role=MessageRole.TOOL, content="[customer]", tool_call_id="tc-2"),
        Message(
            role=MessageRole.ASSISTANT,
            content="Placing your order now.",
            tool_calls=[
                ToolCall(
                    id="tc-3",
                    name="place_order",
                    arguments={
                        "customer_id": "C-101",
                        "items": [
                            {"product_id": "P-001", "quantity": 1, "unit_price": 89.99},
                            {"product_id": "P-002", "quantity": 2, "unit_price": 34.5},
                        ],
                    },
                )
            ],
        ),
        Message(
            role=MessageRole.TOOL,
            content='{"id":"O-001","status":"pending"}',
            tool_call_id="tc-3",
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="Processing payment.",
            tool_calls=[
                ToolCall(
                    id="tc-4",
                    name="confirm_payment",
                    arguments={"order_id": "O-001"},
                )
            ],
        ),
        Message(
            role=MessageRole.TOOL,
            content='{"id":"O-001","status":"paid"}',
            tool_call_id="tc-4",
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content=(
                "Order O-001 has been placed and the payment status is paid. "
                "Your remaining balance is $141.01."
            ),
        ),
        Message(role=MessageRole.USER, content="Thank you! ###STOP###"),
    ]
    return Trajectory(
        task_id="shop_orders_02",
        trial_index=0,
        start_ts=datetime(2026, 1, 1),
        end_ts=datetime(2026, 1, 1),
        status=TrialStatus.COMPLETED,
        messages=messages,
        tool_log=records_from_messages(messages),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mcp_tools(shop_orders_02_task_dir) -> dict:
    """Import TOOLS from the real mcp_server.py via importlib.

    The lifespan hook is never triggered on import, so ``_STATE`` remains an
    empty dict.  We pass ``data`` explicitly through every ``invoke()`` call,
    which is exactly what GradingEngine._execute_golden_actions does.

    If this fixture fails to load, the grading hash check will also be broken.
    """
    task_dir_str = str(shop_orders_02_task_dir)
    # Mirror setup_task_server: task_dir must be at sys.path[0]
    mp = pytest.MonkeyPatch()
    mp.syspath_prepend(task_dir_str)

    spec = importlib.util.spec_from_file_location(
        "_mcp_server_shop_orders_02", shop_orders_02_task_dir / "mcp_server.py"
    )
    mcp_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp_module)

    tools = getattr(mcp_module, "TOOLS", None)
    assert tools, "TOOLS dict not found in mcp_server.py — grading will be broken"
    return tools


# ---------------------------------------------------------------------------
# TestShopOrders02McpTools
# ---------------------------------------------------------------------------


class TestShopOrders02McpTools:
    """MCP tools are importable, callable, and return correct results.

    These are pure unit tests of the tool functions themselves.  They bypass
    MCP stdio transport entirely and call ``TOOLS[name].invoke(data=data, ...)``
    directly — the same interface used by the grading engine.
    """

    def test_tools_dict_exposes_all_enabled_tools(self, mcp_tools, shop_orders_02_task_dir):
        """TOOLS dict contains every tool listed in task.yaml ``enabled``."""
        task_cfg = yaml.safe_load((shop_orders_02_task_dir / "task.yaml").read_text())
        enabled = set(task_cfg["tools"]["agent"]["enabled"])
        missing = enabled - set(mcp_tools.keys())
        assert not missing, f"TOOLS is missing: {missing}"

    def test_list_products_returns_all_in_stock(self, mcp_tools, shop_orders_02_task_dir):
        """list_products returns all 4 products (all have stock > 0 initially)."""
        result = mcp_tools["list_products"].invoke(data=_fresh_data(shop_orders_02_task_dir))
        assert isinstance(result, list), "Expected a list of products"
        assert len(result) == 4
        assert {p["id"] for p in result} == {"P-001", "P-002", "P-003", "P-004"}

    def test_get_customer_returns_correct_record(self, mcp_tools, shop_orders_02_task_dir):
        """get_customer returns the C-101 record with name and balance from initial_state."""
        result = mcp_tools["get_customer"].invoke(
            data=_fresh_data(shop_orders_02_task_dir), customer_id="C-101"
        )
        assert result["id"] == "C-101"
        assert result["name"] == "Alex Torres"
        assert abs(result["balance"] - 300.0) < 0.001

    def test_get_customer_unknown_id_returns_error(self, mcp_tools, shop_orders_02_task_dir):
        """get_customer returns {\"error\": ...} for non-existent customer IDs."""
        result = mcp_tools["get_customer"].invoke(
            data=_fresh_data(shop_orders_02_task_dir), customer_id="C-999"
        )
        assert "error" in result, f"Expected error response, got: {result}"

    def test_place_order_returns_pending_order(self, mcp_tools, shop_orders_02_task_dir):
        """place_order creates an order with status 'pending' and correct total."""
        result = mcp_tools["place_order"].invoke(
            data=_fresh_data(shop_orders_02_task_dir),
            customer_id="C-101",
            items=[
                {"product_id": "P-001", "quantity": 1, "unit_price": 89.99},
                {"product_id": "P-002", "quantity": 2, "unit_price": 34.5},
            ],
        )
        assert result["id"] == "O-001"
        assert result["status"] == "pending", "New order must start as 'pending'"
        assert abs(result["total"] - 158.99) < 0.001  # 89.99 + 2×34.50

    def test_place_order_decrements_stock(self, mcp_tools, shop_orders_02_task_dir):
        """place_order reduces stock on the data dict in-place."""
        data = _fresh_data(shop_orders_02_task_dir)
        mcp_tools["place_order"].invoke(
            data=data,
            customer_id="C-101",
            items=[
                {"product_id": "P-001", "quantity": 1, "unit_price": 89.99},
                {"product_id": "P-002", "quantity": 2, "unit_price": 34.5},
            ],
        )
        p001 = next(p for p in data["products"] if p["id"] == "P-001")
        p002 = next(p for p in data["products"] if p["id"] == "P-002")
        assert p001["stock"] == 41, "P-001 stock: 42 − 1 = 41"
        assert p002["stock"] == 13, "P-002 stock: 15 − 2 = 13"

    def test_place_order_insufficient_stock_returns_error(self, mcp_tools, shop_orders_02_task_dir):
        """place_order returns {\"error\": ...} when requested qty exceeds stock."""
        result = mcp_tools["place_order"].invoke(
            data=_fresh_data(shop_orders_02_task_dir),
            customer_id="C-101",
            items=[{"product_id": "P-004", "quantity": 999, "unit_price": 129.0}],
        )
        assert "error" in result, f"Expected error for over-stock order, got: {result}"

    def test_confirm_payment_marks_order_paid_and_deducts_balance(
        self, mcp_tools, shop_orders_02_task_dir
    ):
        """confirm_payment sets order status=paid and deducts total from balance."""
        data = _fresh_data(shop_orders_02_task_dir)
        mcp_tools["place_order"].invoke(
            data=data,
            customer_id="C-101",
            items=[
                {"product_id": "P-001", "quantity": 1, "unit_price": 89.99},
                {"product_id": "P-002", "quantity": 2, "unit_price": 34.5},
            ],
        )
        result = mcp_tools["confirm_payment"].invoke(data=data, order_id="O-001")

        assert result["status"] == "paid"
        customer = next(c for c in data["customers"] if c["id"] == "C-101")
        assert (
            abs(customer["balance"] - 141.01) < 0.001
        ), f"Expected balance 141.01 (300.00 − 158.99), got {customer['balance']}"

    def test_confirm_payment_already_paid_returns_error(self, mcp_tools, shop_orders_02_task_dir):
        """confirm_payment returns {\"error\": ...} if the order is already paid."""
        data = _fresh_data(shop_orders_02_task_dir)
        mcp_tools["place_order"].invoke(
            data=data,
            customer_id="C-101",
            items=[{"product_id": "P-001", "quantity": 1, "unit_price": 89.99}],
        )
        mcp_tools["confirm_payment"].invoke(data=data, order_id="O-001")
        result = mcp_tools["confirm_payment"].invoke(data=data, order_id="O-001")
        assert "error" in result, "Double-payment must be rejected with an error"


# ---------------------------------------------------------------------------
# TestShopOrders02StateAfterGoldenActions
# ---------------------------------------------------------------------------


class TestShopOrders02StateAfterGoldenActions:
    """golden_actions from grading.yaml produce exactly the DB state expected by jsonpaths.

    This is what the GradingEngine hash check verifies — tested here in isolation
    so failures point directly to a broken tool implementation rather than to the
    grading pipeline itself.
    """

    def test_golden_actions_produce_expected_db_state(self, mcp_tools, shop_orders_02_task_dir):
        """Replaying golden_actions on a fresh DB yields values matching every jsonpath."""
        grading = yaml.safe_load((shop_orders_02_task_dir / "grading.yaml").read_text())
        actions = grading["state_checks"]["hash"]["golden_actions"]
        expected = {jp["path"]: jp["equals"] for jp in grading["state_checks"]["jsonpaths"]}

        data = _fresh_data(shop_orders_02_task_dir)
        for action in actions:
            result = mcp_tools[action["name"]].invoke(data=data, **action["kwargs"])
            assert (
                "error" not in result
            ), f"Golden action '{action['name']}' returned an error: {result}"

        order = data["orders"][0]
        customer = data["customers"][0]
        products = {p["id"]: p for p in data["products"]}

        assert order["id"] == expected["$.db.orders[0].id"]
        assert order["customer_id"] == expected["$.db.orders[0].customer_id"]
        assert order["status"] == expected["$.db.orders[0].status"]
        assert abs(order["total"] - expected["$.db.orders[0].total"]) < 0.001
        assert abs(customer["balance"] - expected["$.db.customers[0].balance"]) < 0.001
        assert products["P-001"]["stock"] == expected["$.db.products[0].stock"]
        assert products["P-002"]["stock"] == expected["$.db.products[1].stock"]

    def test_golden_actions_leave_non_ordered_products_unchanged(
        self, mcp_tools, shop_orders_02_task_dir
    ):
        """Products not involved in golden_actions must keep their original stock."""
        grading = yaml.safe_load((shop_orders_02_task_dir / "grading.yaml").read_text())
        initial = _fresh_data(shop_orders_02_task_dir)
        data = _fresh_data(shop_orders_02_task_dir)

        for action in grading["state_checks"]["hash"]["golden_actions"]:
            mcp_tools[action["name"]].invoke(data=data, **action["kwargs"])

        # P-003 and P-004 are never ordered — stock must not change
        for pid in ("P-003", "P-004"):
            before = next(p["stock"] for p in initial["products"] if p["id"] == pid)
            after = next(p["stock"] for p in data["products"] if p["id"] == pid)
            assert after == before, f"{pid} stock changed unexpectedly: {before} → {after}"


# ---------------------------------------------------------------------------
# TestShopOrders02GradingPipeline
# ---------------------------------------------------------------------------


class TestShopOrders02GradingPipeline:
    """GradingEngine produces correct scores for passing and failing trajectories.

    Tests use a synthetically constructed trajectory so they are deterministic
    and never depend on external output directories.
    """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _expected_final_env_state(self, mcp_tools: dict, task_dir) -> dict:
        """Run golden_actions on a fresh DB and wrap in the env_state structure."""
        grading = yaml.safe_load((task_dir / "grading.yaml").read_text())
        data = _fresh_data(task_dir)
        for action in grading["state_checks"]["hash"]["golden_actions"]:
            mcp_tools[action["name"]].invoke(data=data, **action["kwargs"])
        return {"db": data}

    def _passing_trajectory(self) -> Trajectory:
        """Trajectory with all required_actions called and all communicate_info present."""
        messages = [
            Message(
                role=MessageRole.USER,
                content=(
                    "Hi! I'm customer C-101 (Alex Torres). I'd like to buy "
                    "1 × Wireless Headphones and 2 × USB-C Hub 7-Port."
                ),
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content="Checking the catalog and your account.",
                tool_calls=[
                    ToolCall(id="tc-1", name="list_products", arguments={}),
                    ToolCall(
                        id="tc-2",
                        name="get_customer",
                        arguments={"customer_id": "C-101"},
                    ),
                ],
            ),
            Message(role=MessageRole.TOOL, content="[products]", tool_call_id="tc-1"),
            Message(role=MessageRole.TOOL, content="[customer]", tool_call_id="tc-2"),
            Message(
                role=MessageRole.ASSISTANT,
                content="Placing your order now.",
                tool_calls=[
                    ToolCall(
                        id="tc-3",
                        name="place_order",
                        arguments={
                            "customer_id": "C-101",
                            "items": [
                                {"product_id": "P-001", "quantity": 1, "unit_price": 89.99},
                                {"product_id": "P-002", "quantity": 2, "unit_price": 34.5},
                            ],
                        },
                    )
                ],
            ),
            Message(
                role=MessageRole.TOOL,
                content='{"id":"O-001","status":"pending"}',
                tool_call_id="tc-3",
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content="Processing payment.",
                tool_calls=[
                    ToolCall(
                        id="tc-4",
                        name="confirm_payment",
                        arguments={"order_id": "O-001"},
                    )
                ],
            ),
            Message(
                role=MessageRole.TOOL,
                content='{"id":"O-001","status":"paid"}',
                tool_call_id="tc-4",
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content=(
                    "Order O-001 has been placed and the payment status is paid. "
                    "Your remaining balance is $141.01."
                ),
            ),
            Message(role=MessageRole.USER, content="Thank you! ###STOP###"),
        ]
        return Trajectory(
            task_id="shop_orders_02",
            trial_index=0,
            start_ts=datetime(2026, 1, 1),
            end_ts=datetime(2026, 1, 1),
            status=TrialStatus.COMPLETED,
            messages=messages,
            tool_log=records_from_messages(messages),
        )

    def _no_tool_calls_trajectory(self) -> Trajectory:
        """Trajectory with no tool calls and no communicate_info — should fail everything."""
        return Trajectory(
            task_id="shop_orders_02",
            trial_index=0,
            start_ts=datetime(2026, 1, 1),
            end_ts=datetime(2026, 1, 1),
            status=TrialStatus.COMPLETED,
            messages=[
                Message(role=MessageRole.USER, content="Hi, place my order."),
                Message(
                    role=MessageRole.ASSISTANT,
                    content="Sorry, I cannot do that right now.",
                ),
            ],
        )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("builder", ["module_level", "class_level"])
    def test_passing_trajectory_records_what_its_tools_returned(self, builder):
        """Both passing builders carry a record, and it reports what their tools returned.

        ``required_actions`` is a claim about a call the trial made, so a builder
        with no ``tool_log`` pins the record-absent path while its name claims the
        semantic one. A ``TOOL_RESULT``'s text comes from the record whenever one
        exists, so a record built without ``output`` blanks every tool result the
        message view shows.
        """
        trajectory = {
            "module_level": _make_passing_trajectory,
            "class_level": self._passing_trajectory,
        }[builder]()
        timeline = build_trial_timeline(trajectory.messages, trajectory.tool_log, None)

        assert timeline.records_present is True
        recorded_text = [
            event.result for event in timeline.events if event.kind is TraceEventKind.TOOL_RESULT
        ]
        assert recorded_text == [
            message.content for message in trajectory.messages if message.role is MessageRole.TOOL
        ]

    def test_passing_trajectory_scores_1_0(self, mcp_tools, shop_orders_02_task_dir):
        """Correct tool sequence + communicated info + correct final DB → score 1.0."""
        engine = _build_engine(shop_orders_02_task_dir)
        grade = engine.grade_trajectory(
            self._passing_trajectory(),
            self._expected_final_env_state(mcp_tools, shop_orders_02_task_dir),
        )
        assert grade.score == pytest.approx(
            1.0
        ), f"Expected score=1.0, got {grade.score}. Reasons: {grade.reasons}"
        assert grade.binary_pass is True
        assert grade.components.state_checks == pytest.approx(
            1.0
        ), f"state_checks={grade.components.state_checks}"
        assert grade.components.transcript_rules == pytest.approx(
            1.0
        ), f"transcript_rules={grade.components.transcript_rules}"

    def test_failing_trajectory_does_not_pass_threshold(self, mcp_tools, shop_orders_02_task_dir):
        """No tool calls + unchanged DB → score < pass_threshold (0.75) → binary_pass=False."""
        engine = _build_engine(shop_orders_02_task_dir)
        grade = engine.grade_trajectory(
            self._no_tool_calls_trajectory(),
            {"db": _fresh_data(shop_orders_02_task_dir)},  # unchanged state
        )
        assert (
            grade.score < 0.75
        ), f"Failing trajectory must score below pass_threshold=0.75, got {grade.score}"
        assert grade.binary_pass is False

    def test_correct_transcript_wrong_db_state_still_fails(
        self, mcp_tools, shop_orders_02_task_dir
    ):
        """state_checks=0 when DB is unchanged, even if transcript is perfect.

        Verifies that state_checks are independent from transcript_rules and that
        the 70 % weight on state_checks is enough to push the total below 0.75.
        """
        engine = _build_engine(shop_orders_02_task_dir)
        grade = engine.grade_trajectory(
            self._passing_trajectory(),
            {"db": _fresh_data(shop_orders_02_task_dir)},  # ← unchanged DB, but perfect transcript
        )
        assert grade.components.state_checks == pytest.approx(
            0.0
        ), f"state_checks should be 0 for unchanged DB, got {grade.components.state_checks}"
        assert (
            grade.binary_pass is False
        ), "state_checks weight (0.70) means even a perfect transcript cannot save a 0 state score"

    def test_correct_db_state_missing_communicate_info_reduces_score(
        self, mcp_tools, shop_orders_02_task_dir
    ):
        """transcript_rules < 1.0 when required communicate_info values are absent.

        Uses a trajectory where all required_actions are called but the final
        assistant message omits order ID, status, and balance.
        """
        engine = _build_engine(shop_orders_02_task_dir)
        silent_messages = [
            Message(role=MessageRole.USER, content="Place my order."),
            Message(
                role=MessageRole.ASSISTANT,
                content="Done.",  # no O-001, no "paid", no 141.01
                tool_calls=[
                    ToolCall(id="tc-1", name="list_products", arguments={}),
                    ToolCall(
                        id="tc-2",
                        name="get_customer",
                        arguments={"customer_id": "C-101"},
                    ),
                    ToolCall(
                        id="tc-3",
                        name="place_order",
                        arguments={
                            "customer_id": "C-101",
                            "items": [
                                {"product_id": "P-001", "quantity": 1, "unit_price": 89.99},
                                {"product_id": "P-002", "quantity": 2, "unit_price": 34.5},
                            ],
                        },
                    ),
                    ToolCall(
                        id="tc-4",
                        name="confirm_payment",
                        arguments={"order_id": "O-001"},
                    ),
                ],
            ),
        ]
        traj = Trajectory(
            task_id="shop_orders_02",
            trial_index=0,
            start_ts=datetime(2026, 1, 1),
            end_ts=datetime(2026, 1, 1),
            status=TrialStatus.COMPLETED,
            messages=silent_messages,
        )
        grade = engine.grade_trajectory(
            traj, self._expected_final_env_state(mcp_tools, shop_orders_02_task_dir)
        )

        assert grade.components.transcript_rules < 1.0, (
            "transcript_rules should be < 1.0 when communicate_info items are missing "
            f"(got {grade.components.transcript_rules})"
        )


# ---------------------------------------------------------------------------
# TestShopOrders02McpTransport
# ---------------------------------------------------------------------------


class TestShopOrders02McpTransport:
    """MCP stdio/JSON-RPC transport layer — the path the real runner uses.

    While ``TestShopOrders02McpTools`` calls ``TOOLS[name].invoke()`` directly
    (grading mode, no subprocess), these tests use ``MCPServerProcess`` to start
    ``mcp_server.py`` as a subprocess and communicate via stdio JSON-RPC, exactly
    as ``MCPServerToolWrapper`` does during a live trial.

    Catches issues that the TOOLS.invoke() path misses:
      - JSON serialization of nested arguments (int vs float, list[OrderItem])
      - MCP protocol handshake (initialize → notifications/initialized)
      - State persistence across separate JSON-RPC requests
      - Content parsing: MCP wraps results in [{type: text, text: ...}]

    Test ordering note: tests 3-6 are intentionally sequential — place_order
    runs before confirm_payment to exercise state persistence within one server
    process. Pytest runs class methods in definition order.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def mcp_server(cls, shop_orders_02_task_dir) -> _McpSubprocess:
        """Start mcp_server.py subprocess and inject fresh initial state.

        Lifespan does not run when the script is started without file input,
        so _STATE starts empty. reset_state() injects the known initial data
        through the internal _tolokaforge_set_state_ JSON-RPC tool.
        """
        server = _McpSubprocess(str(shop_orders_02_task_dir / "mcp_server.py"))
        server.start()
        server.reset_state(json.loads((shop_orders_02_task_dir / "initial_state.json").read_text()))
        yield server
        server.stop()

    # ------------------------------------------------------------------

    def test_tools_list_contains_all_four_tools(self, mcp_server):
        """tools/list JSON-RPC response includes all 4 shop tools."""
        tool_names = set(mcp_server.list_tools())
        for name in ("list_products", "get_customer", "place_order", "confirm_payment"):
            assert name in tool_names, f"'{name}' missing from tools/list: {tool_names}"

    def test_list_products_via_jsonrpc_returns_four_products(self, mcp_server):
        """list_products via JSON-RPC returns 4 products (read-only, no state change)."""
        result = mcp_server.call_tool("list_products", {})
        assert isinstance(result, list), f"Expected list, got: {result}"
        assert len(result) == 4

    def test_get_customer_via_jsonrpc_returns_correct_record(self, mcp_server):
        """get_customer JSON-RPC correctly deserializes customer_id string argument."""
        result = mcp_server.call_tool("get_customer", {"customer_id": "C-101"})
        assert result["id"] == "C-101"
        assert result["name"] == "Alex Torres"
        assert abs(result["balance"] - 300.0) < 0.001

    def test_place_order_integer_qty_serialized_correctly_via_jsonrpc(self, mcp_server):
        """place_order accepts integer quantity through JSON-RPC serialization.

        JSON encodes integers as numbers without decimal point. This verifies
        that quantity=1 (integer) is not rejected by Pydantic's ``ge=1``
        constraint after JSON-RPC round-trip (could fail if coerced to float).
        """
        result = mcp_server.call_tool(
            "place_order",
            {
                "customer_id": "C-101",
                "items": [
                    {"product_id": "P-001", "quantity": 1, "unit_price": 89.99},
                    {"product_id": "P-002", "quantity": 2, "unit_price": 34.5},
                ],
            },
        )
        assert "error" not in result, f"place_order failed over JSON-RPC: {result}"
        assert result["id"] == "O-001"
        assert result["status"] == "pending"
        assert abs(result["total"] - 158.99) < 0.001

    def test_state_persists_across_jsonrpc_requests(self, mcp_server):
        """confirm_payment sees the order created by the previous place_order call.

        State must live in a single _STATE dict shared across all JSON-RPC
        requests to the same subprocess. If the server re-loads state on each
        request, confirm_payment would return 'order not found'.
        """
        result = mcp_server.call_tool("confirm_payment", {"order_id": "O-001"})
        assert (
            "error" not in result
        ), f"confirm_payment failed — state not persisted across JSON-RPC calls: {result}"
        assert result["status"] == "paid"

    def test_get_state_shows_correct_mutations_after_full_workflow(
        self, mcp_server, shop_orders_02_task_dir
    ):
        """get_state() after place_order + confirm_payment reflects all expected changes."""
        state = mcp_server.get_state()
        expected = {
            jp["path"]: jp["equals"]
            for jp in yaml.safe_load((shop_orders_02_task_dir / "grading.yaml").read_text())[
                "state_checks"
            ]["jsonpaths"]
        }
        order = state["orders"][0]
        customer = state["customers"][0]
        products = {p["id"]: p for p in state["products"]}

        assert order["id"] == expected["$.db.orders[0].id"]
        assert order["status"] == expected["$.db.orders[0].status"]
        assert abs(order["total"] - expected["$.db.orders[0].total"]) < 0.001
        assert abs(customer["balance"] - expected["$.db.customers[0].balance"]) < 0.001
        assert products["P-001"]["stock"] == expected["$.db.products[0].stock"]
        assert products["P-002"]["stock"] == expected["$.db.products[1].stock"]


# ---------------------------------------------------------------------------
# TestShopOrders02AdapterGradingIntegration
# ---------------------------------------------------------------------------


class TestShopOrders02AdapterGradingIntegration:
    """NativeAdapter.grade() integration — same code path as the Orchestrator.

    In ``TestShopOrders02GradingPipeline``, GradingEngine is constructed
    directly with hardcoded parameters. Here, we go through:

        NativeAdapter.get_task("shop_orders_02")
        → NativeAdapter.get_task_dir()       (resolves to tests/data/tasks/shop_orders_02/)
        → NativeAdapter.get_grading_config()  (reads grading.yaml)
        → base.BaseAdapter.grade()            (builds GradingEngine, calls grade_trajectory)

    Uses the test-data directory so tests stay within tests/data/.
    The self-contained mcp_server.py there mirrors the production implementation.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def real_adapter(cls, test_data_dir) -> NativeAdapter:
        """NativeAdapter pointing at tests/data/ — same base_dir as TestNativeAdapterCanon."""
        return NativeAdapter(
            {
                "base_dir": str(test_data_dir),
                "tasks_glob": "tasks/**/task.yaml",
            }
        )

    def _expected_env_state(self, mcp_tools: dict, shop_orders_02_task_dir) -> dict:
        grading = yaml.safe_load((shop_orders_02_task_dir / "grading.yaml").read_text())
        data = _fresh_data(shop_orders_02_task_dir)
        for action in grading["state_checks"]["hash"]["golden_actions"]:
            mcp_tools[action["name"]].invoke(data=data, **action["kwargs"])
        return {"db": data}

    def test_grade_via_adapter_passes_for_correct_state(
        self, real_adapter, mcp_tools, shop_orders_02_task_dir
    ):
        """NativeAdapter.grade() returns score=1.0 for a trajectory with correct final DB.

        Tests the full integration chain: adapter config loading → GradingEngine
        construction → tau-style hash check using the real mcp_server.py.
        """
        env = AdapterEnvironment(
            data=_fresh_data(shop_orders_02_task_dir), tools=[], wiki="", rules=[]
        )
        grade = real_adapter.grade(
            "shop_orders_02",
            _make_passing_trajectory(),
            self._expected_env_state(mcp_tools, shop_orders_02_task_dir),
            env,
        )
        assert grade.binary_pass is True, f"Expected pass, reasons: {grade.reasons}"
        assert grade.score == pytest.approx(1.0)

    def test_grade_via_adapter_fails_for_unchanged_db(self, real_adapter, shop_orders_02_task_dir):
        """NativeAdapter.grade() returns fail when DB is unchanged (no tools called).

        Verifies that the adapter correctly wires task_dir and mcp_server_ref so
        the GradingEngine can detect the missing mutations.
        """
        env = AdapterEnvironment(
            data=_fresh_data(shop_orders_02_task_dir), tools=[], wiki="", rules=[]
        )
        grade = real_adapter.grade(
            "shop_orders_02",
            _make_passing_trajectory(),
            {"db": _fresh_data(shop_orders_02_task_dir)},
            env,
        )
        assert grade.binary_pass is False
        assert grade.components.state_checks == pytest.approx(
            0.0
        ), f"state_checks should be 0 for unchanged DB, got {grade.components.state_checks}"

    def test_adapter_task_dir_points_to_functional_mcp_server(
        self, real_adapter, shop_orders_02_task_dir
    ):
        """adapter.get_task_dir() must return tests/data/tasks/shop_orders_02/ with
        a functional mcp_server.py — not an empty stub — so GradingEngine
        golden_action execution can find TOOLS.
        """
        task_dir = real_adapter.get_task_dir("shop_orders_02")
        mcp_path = task_dir / "mcp_server.py"
        assert mcp_path.exists(), f"mcp_server.py not found at {mcp_path}"
        content = mcp_path.read_text()
        assert (
            "TOOLS" in content
        ), f"mcp_server.py at {mcp_path} has no TOOLS — golden_action execution will fail"
        assert (
            "create_server" in content
        ), f"mcp_server.py at {mcp_path} does not use create_server — may not register tools"


# ---------------------------------------------------------------------------
# TestShopOrders02TrajectoryRoundTrip
# ---------------------------------------------------------------------------


class TestShopOrders02TrajectoryRoundTrip:
    """OutputWriter.write_trajectory() YAML round-trip preserves grading-critical fields.

    The Orchestrator writes trajectory.yaml after every trial. Canonical test
    replays and manual grading audits reconstruct messages from that YAML. If
    any field is silently dropped or type-coerced, grading and audit results
    will diverge from the original run.

    Fields critical for grading:
      - tool_calls[].name  → ActionEvaluator.required_actions matching
      - tool_calls[].arguments → compare_args matching (future use)
      - assistant content → CommunicateEvaluator.communicate_info matching
      - task_id / status  → trajectory provenance
    """

    def test_tool_call_names_survive_yaml_round_trip(self, tmp_path):
        """All 4 tool names written to trajectory.yaml can be read back intact."""
        writer = OutputWriter(tmp_path)
        writer.write_trajectory(_make_passing_trajectory())

        raw = yaml.safe_load((tmp_path / "trajectory.yaml").read_text())
        all_tool_names = [
            tc["name"] for msg in raw["messages"] for tc in (msg.get("tool_calls") or [])
        ]
        for expected_name in ("list_products", "get_customer", "place_order", "confirm_payment"):
            assert (
                expected_name in all_tool_names
            ), f"'{expected_name}' not found in reloaded tool_calls: {all_tool_names}"

    def test_nested_place_order_arguments_survive_yaml_round_trip(self, tmp_path):
        """place_order nested items list survives YAML serialization with correct types.

        YAML can coerce numeric strings to numbers and integers to floats.
        Verifies that quantity stays as int and unit_price stays as float after
        writing → reading — wrong types would break Pydantic validation if the
        trajectory is ever replayed.
        """
        writer = OutputWriter(tmp_path)
        writer.write_trajectory(_make_passing_trajectory())

        raw = yaml.safe_load((tmp_path / "trajectory.yaml").read_text())
        place_order_calls = [
            tc
            for msg in raw["messages"]
            for tc in (msg.get("tool_calls") or [])
            if tc["name"] == "place_order"
        ]
        assert len(place_order_calls) == 1, "Expected exactly one place_order call"

        items = place_order_calls[0]["arguments"]["items"]
        assert len(items) == 2

        p001 = next(i for i in items if i["product_id"] == "P-001")
        assert p001["quantity"] == 1, f"quantity should be 1 (int), got {p001['quantity']!r}"
        assert abs(p001["unit_price"] - 89.99) < 0.001

        p002 = next(i for i in items if i["product_id"] == "P-002")
        assert p002["quantity"] == 2
        assert abs(p002["unit_price"] - 34.5) < 0.001

    def test_communicate_info_values_survive_in_assistant_messages(self, tmp_path):
        """Assistant message content with O-001/paid/141.01 is preserved verbatim.

        CommunicateEvaluator does substring search on assistant message content.
        If YAML escaping mangles the text (e.g., dollar sign or decimals),
        communicate_info checks would silently fail on replayed trajectories.
        """
        writer = OutputWriter(tmp_path)
        writer.write_trajectory(_make_passing_trajectory())

        raw = yaml.safe_load((tmp_path / "trajectory.yaml").read_text())
        assistant_texts = " ".join(
            msg.get("content", "") for msg in raw["messages"] if msg.get("role") == "assistant"
        )
        for info in ("O-001", "paid", "141.01"):
            assert info in assistant_texts, (
                f"'{info}' not found in reloaded assistant messages — "
                "CommunicateEvaluator would mis-score replayed trajectory"
            )

    def test_trajectory_metadata_fields_survive_yaml_round_trip(self, tmp_path):
        """task_id, trial_index, status, start_ts, end_ts are preserved."""
        writer = OutputWriter(tmp_path)
        writer.write_trajectory(_make_passing_trajectory())

        raw = yaml.safe_load((tmp_path / "trajectory.yaml").read_text())
        assert raw["task_id"] == "shop_orders_02"
        assert raw["trial_index"] == 0
        assert raw["status"] == "completed"
        assert raw["start_ts"] is not None
        assert raw["end_ts"] is not None


# ---------------------------------------------------------------------------
# TestShopOrders02McpWrapperContract
# ---------------------------------------------------------------------------


class TestShopOrders02McpWrapperContract:
    """The production ``MCPServerToolWrapper`` over the real subprocess.

    MCP answers a call the tool never completed with ``isError: true`` beside
    error prose, and answers a business failure the tool *declared* with the
    flag unset and a ``{"error": ...}`` payload the tool wrote itself. The two
    are disjoint on this substrate by construction: ``DomainToolRegistry.tool``'s
    generated MCP function catches ``ToolError`` and returns it as a payload, so
    only an exception the tool did not declare reaches FastMCP's error handler.
    These cases pin both sides of that split, and pin that ``execute`` hands the
    agent the same text either way.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def wrapper_for(cls, shop_orders_02_task_dir):
        """Build wrappers for one shared ``mcp_server.py`` subprocess.

        ``MCPServerToolWrapper._servers`` caches subprocesses on the class keyed
        by script path and the instance ``cleanup()`` is a no-op, so the
        classmethod is the only teardown there is — without it the subprocess
        outlives the session. The cases below share the one server deliberately:
        an argument-validation failure and two reads, none depending on what the
        others left behind.

        ``db_client`` is never reached by a tool call, so the fixture hands over a
        real client pointed at an unroutable host: touching it fails loudly
        instead of being absorbed by a mock.
        """
        script = str(shop_orders_02_task_dir / "mcp_server.py")

        def build(tool_name: str) -> MCPServerToolWrapper:
            return MCPServerToolWrapper(
                tool_schema=RunnerToolSchema(
                    name=tool_name,
                    description=f"{tool_name} on the shop_orders_02 MCP server",
                    parameters={},
                ),
                server_script=script,
                db_client=DBServiceClient("http://db-service.invalid"),
                trial_id="shop_orders_02:0",
            )

        yield build
        MCPServerToolWrapper.cleanup_all_servers()

    # ------------------------------------------------------------------

    async def test_an_argument_the_signature_does_not_declare_is_a_declared_failure(
        self, wrapper_for
    ):
        """A kwarg typo never runs the tool, and the wrapper says so.

        FastMCP validates arguments against the generated pydantic model, so the
        call fails before ``confirm_payment``'s body decides anything. Its prose
        names the field that is missing, which is the actionable part for whoever
        authored the call.
        """
        wrapper = wrapper_for("confirm_payment")

        outcome = await wrapper.execute_call({"order_idd": "O-001"})

        assert outcome.declared_failure is True
        assert outcome.output.startswith("Error executing tool confirm_payment:")
        assert "order_id" in outcome.output

    async def test_a_failure_the_tool_declared_is_not_a_substrate_failure(self, wrapper_for):
        """A ``ToolError`` comes back as a payload with the flag unset.

        The tool ran and declined the request, which is a different defect from
        the call never happening — the flag must not claim otherwise, or the two
        populations collapse into one.
        """
        wrapper = wrapper_for("confirm_payment")

        outcome = await wrapper.execute_call({"order_id": "O-999"})

        assert outcome.declared_failure is False
        assert json.loads(outcome.output) == {"error": "Order 'O-999' not found"}

    async def test_a_read_that_succeeds_is_not_a_declared_failure(self, wrapper_for):
        wrapper = wrapper_for("list_products")

        outcome = await wrapper.execute_call({})

        assert outcome.declared_failure is False
        assert "Wireless Headphones" in outcome.output

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("confirm_payment", {"order_idd": "O-001"}),
            ("confirm_payment", {"order_id": "O-999"}),
            ("list_products", {}),
        ],
    )
    async def test_execute_hands_back_the_same_text_the_outcome_carries(
        self, wrapper_for, tool_name, arguments
    ):
        """The agent path is unchanged, the flagged call included.

        A model recovers from its own bad call by reading the error prose, so the
        flagged case must reach ``execute`` as text rather than as a raise.
        """
        wrapper = wrapper_for(tool_name)

        assert await wrapper.execute(arguments) == (await wrapper.execute_call(arguments)).output
