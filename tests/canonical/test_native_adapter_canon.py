"""Canonical tests for adapter output — compares against golden snapshots."""

import json
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.canonical

TEST_DATA_DIR = Path(__file__).parent.parent / "data"
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

_SHOP_SRC = TEST_DATA_DIR / "tasks" / "shop_orders_02"
_SHOP_SNAP = SNAPSHOT_DIR / "native_shop_orders_02"


@pytest.fixture
def native_adapter() -> NativeAdapter:
    """Create NativeAdapter pointed at tests/data/tasks/."""
    return NativeAdapter(
        {
            "base_dir": str(TEST_DATA_DIR),
            "tasks_glob": "tasks/**/task.yaml",
        }
    )


class TestNativeAdapterCanon:
    """Canonical tests for NativeAdapter task loading."""

    def test_calc_basic_task_config(self, native_adapter, canon_snapshot):
        """NativeAdapter loads calc_basic and produces expected TaskConfig."""
        task = native_adapter.get_task("calc_basic")
        snap = canon_snapshot("native_calc_basic")

        actual = task.model_dump(mode="json")
        snap.assert_match(actual, "task_config.json")

    def test_calc_basic_grading_config(self, native_adapter, canon_snapshot):
        """NativeAdapter loads grading for calc_basic."""
        grading = native_adapter.get_grading_config("calc_basic")
        snap = canon_snapshot("native_calc_basic")

        actual = grading.model_dump(mode="json")
        snap.assert_match(actual, "grading_config.json")

    def test_browser_basic_task_config(self, native_adapter, canon_snapshot):
        """NativeAdapter loads browser_basic and produces expected TaskConfig."""
        task = native_adapter.get_task("browser_basic")
        snap = canon_snapshot("native_browser_basic")

        actual = task.model_dump(mode="json")
        snap.assert_match(actual, "task_config.json")

    def test_browser_basic_grading_config(self, native_adapter, canon_snapshot):
        """NativeAdapter loads grading for browser_basic."""
        grading = native_adapter.get_grading_config("browser_basic")
        snap = canon_snapshot("native_browser_basic")

        actual = grading.model_dump(mode="json")
        snap.assert_match(actual, "grading_config.json")


class TestShopOrders02Canon:
    """Canonical tests for NativeAdapter on shop_orders_02.

    Validates parsing of the three elements that distinguish this task from
    existing fixtures: MCP server tool references, initial json_db state, and
    state_checks with golden_actions.
    """

    def test_task_config(self, native_adapter, canon_snapshot):
        """TaskConfig includes mcp_server ref, enabled tools, and user_simulator."""
        task = native_adapter.get_task("shop_orders_02")
        snap = canon_snapshot("native_shop_orders_02")

        snap.assert_match(task.model_dump(mode="json"), "task_config.json")

    def test_grading_config(self, native_adapter, canon_snapshot):
        """GradingConfig captures golden_actions, jsonpaths, and combine weights."""
        grading = native_adapter.get_grading_config("shop_orders_02")
        snap = canon_snapshot("native_shop_orders_02")

        snap.assert_match(grading.model_dump(mode="json"), "grading_config.json")

    def test_tool_schemas(self, native_adapter, canon_snapshot):
        """Agent tool schemas: names, descriptions, and parameter JSON Schemas.

        Protects against unintentional changes to fixtures/tools.json or the
        adapter logic that loads and filters it — both directly affect what the
        LLM agent sees as tool definitions.
        """
        td = native_adapter.to_task_description("shop_orders_02")
        snap = canon_snapshot("native_shop_orders_02")

        actual = [t.model_dump(mode="json") for t in td.agent_tools]
        snap.assert_match(actual, "tool_schemas.json")

    def test_initial_state_tables(self, native_adapter, canon_snapshot):
        """Initial DB tables parsed from initial_state.json.

        Protects against changes to product prices/stock, customer balances, or
        the adapter's JSON→table conversion logic — all of which affect grading.
        """
        td = native_adapter.to_task_description("shop_orders_02")
        snap = canon_snapshot("native_shop_orders_02")

        actual = td.initial_state.model_dump(mode="json")
        snap.assert_match(actual, "initial_state_tables.json")


class TestShopOrders02SnapshotIntegrity:
    """Cross-validate snapshots against their source files WITHOUT using the adapter.

    These tests catch the scenario where ``--update-canon`` was run after a
    buggy adapter change: the adapter tests in TestShopOrders02Canon would
    still pass (adapter output matches snapshot), but these tests would fail
    because the snapshot no longer mirrors the raw source files.

    Red-flag pattern::

        snapshot changes  +  initial_state.json / tools.json / grading.yaml unchanged
        => the adapter changed behaviour and baked the wrong output into canon

    These tests derive expected values from first principles and never instantiate
    the adapter, so they act as an independent oracle.
    """

    def test_initial_state_snapshot_mirrors_source(self):
        """Snapshot tables must be byte-for-byte derivable from initial_state.json.

        If this test fails while TestShopOrders02Canon.test_initial_state_tables
        passes, the adapter introduced a transformation that diverges from the
        source data.
        """
        source = json.loads((_SHOP_SRC / "initial_state.json").read_text())
        snapshot = json.loads((_SHOP_SNAP / "initial_state_tables.json").read_text())

        tables = snapshot["tables"]

        def by_id(lst: list) -> list:
            return sorted(lst, key=lambda x: x["id"])

        assert by_id(tables["products"]) == by_id(source["products"]), (
            "Snapshot products differ from initial_state.json — "
            "run --update-canon only if initial_state.json was intentionally changed"
        )
        assert by_id(tables["customers"]) == by_id(
            source["customers"]
        ), "Snapshot customers differ from initial_state.json"
        assert (
            tables["orders"] == source["orders"]
        ), "Snapshot orders differ from initial_state.json"

    def test_grading_snapshot_mirrors_source(self):
        """Snapshot grading_config must faithfully reflect grading.yaml without adapter.

        Checks combine weights, golden_actions, jsonpaths, and communicate_info so
        that a silent adapter serialisation bug cannot hide here.
        """
        source = yaml.safe_load((_SHOP_SRC / "grading.yaml").read_text())
        snapshot = json.loads((_SHOP_SNAP / "grading_config.json").read_text())

        assert snapshot["combine"]["weights"] == source["combine"]["weights"]
        assert snapshot["combine"]["pass_threshold"] == pytest.approx(
            source["combine"]["pass_threshold"]
        )

        snap_actions = snapshot["state_checks"]["hash"]["golden_actions"]
        src_actions = source["state_checks"]["hash"]["golden_actions"]
        assert len(snap_actions) == len(
            src_actions
        ), f"golden_actions count: snapshot={len(snap_actions)}, source={len(src_actions)}"
        for snap_act, src_act in zip(snap_actions, src_actions):
            assert snap_act["name"] == src_act["name"]
            assert snap_act["kwargs"] == src_act["kwargs"]

        snap_paths = {jp["path"]: jp["equals"] for jp in snapshot["state_checks"]["jsonpaths"]}
        src_paths = {jp["path"]: jp["equals"] for jp in source["state_checks"]["jsonpaths"]}
        assert set(snap_paths.keys()) == set(
            src_paths.keys()
        ), "jsonpath keys differ between snapshot and grading.yaml"
        for path, expected in src_paths.items():
            if isinstance(expected, float):
                assert (
                    abs(snap_paths[path] - expected) < 1e-9
                ), f"jsonpath {path}: snapshot={snap_paths[path]}, source={expected}"
            else:
                assert (
                    snap_paths[path] == expected
                ), f"jsonpath {path}: snapshot={snap_paths[path]!r}, source={expected!r}"

        snap_info = {ci["info"] for ci in snapshot["transcript_rules"]["communicate_info"]}
        src_info = {ci["info"] for ci in source["transcript_rules"]["communicate_info"]}
        assert (
            snap_info == src_info
        ), f"communicate_info mismatch: snapshot={snap_info}, source={src_info}"

    def test_tool_schemas_snapshot_respects_enabled_order(self):
        """Snapshot tool list must contain exactly the tools from task.yaml `enabled`, in order.

        Two invariants:
        1. Names appear in the same order as the ``enabled`` list — this is what
           the LLM agent sees, so order matters for prompt construction.
        2. Descriptions and parameter schemas match tools.json verbatim — the
           adapter must not silently alter what the agent sees.
        """
        task_cfg = yaml.safe_load((_SHOP_SRC / "task.yaml").read_text())
        enabled: list[str] = task_cfg["tools"]["agent"]["enabled"]

        source_tools = {
            t["name"]: t for t in json.loads((_SHOP_SRC / "fixtures" / "tools.json").read_text())
        }
        snapshot_tools = json.loads((_SHOP_SNAP / "tool_schemas.json").read_text())

        snapshot_names = [t["name"] for t in snapshot_tools]
        assert snapshot_names == enabled, (
            f"Snapshot tool order {snapshot_names} does not match enabled list {enabled}. "
            "This order is what the LLM agent sees."
        )

        for snap_tool in snapshot_tools:
            name = snap_tool["name"]
            assert (
                name in source_tools
            ), f"Snapshot contains tool '{name}' not present in fixtures/tools.json"
            assert (
                snap_tool["description"] == source_tools[name]["description"]
            ), f"Tool '{name}' description mismatch between snapshot and tools.json"
            assert (
                snap_tool["parameters"] == source_tools[name]["parameters"]
            ), f"Tool '{name}' parameter schema mismatch between snapshot and tools.json"

    def test_grading_arithmetic_consistency(self):
        """Expected final values in grading.yaml must be arithmetically derivable from initial_state.json.

        This test uses no adapter and no snapshot — it verifies the source files
        are internally self-consistent. If it fails, someone edited prices, stock
        levels, or golden_actions without updating the jsonpath expected values.

        Equations checked::

            order_total  = sum(item.unit_price * item.quantity)
            balance_after = customer.balance - order_total
            stock_after[P] = product.stock - qty_ordered[P]
        """
        initial = json.loads((_SHOP_SRC / "initial_state.json").read_text())
        grading = yaml.safe_load((_SHOP_SRC / "grading.yaml").read_text())

        products = {p["id"]: p for p in initial["products"]}
        customers = {c["id"]: c for c in initial["customers"]}

        actions = grading["state_checks"]["hash"]["golden_actions"]
        place = next(a for a in actions if a["name"] == "place_order")
        confirm = next(a for a in actions if a["name"] == "confirm_payment")

        customer_id: str = place["kwargs"]["customer_id"]
        order_items: list = place["kwargs"]["items"]
        order_id: str = confirm["kwargs"]["order_id"]

        expected_total = round(
            sum(item["unit_price"] * item["quantity"] for item in order_items), 10
        )
        expected_balance = round(customers[customer_id]["balance"] - expected_total, 10)

        jsonpaths = {jp["path"]: jp["equals"] for jp in grading["state_checks"]["jsonpaths"]}

        assert jsonpaths["$.db.orders[0].id"] == order_id
        assert jsonpaths["$.db.orders[0].customer_id"] == customer_id
        assert jsonpaths["$.db.orders[0].status"] == "paid"

        assert abs(jsonpaths["$.db.orders[0].total"] - expected_total) < 0.001, (
            f"grading.yaml total {jsonpaths['$.db.orders[0].total']} != "
            f"computed {expected_total} from golden_actions"
        )
        assert abs(jsonpaths["$.db.customers[0].balance"] - expected_balance) < 0.001, (
            f"grading.yaml balance {jsonpaths['$.db.customers[0].balance']} != "
            f"computed {expected_balance} "
            f"(initial {customers[customer_id]['balance']} − total {expected_total})"
        )

        product_list = initial["products"]
        for item in order_items:
            pid = item["product_id"]
            product_idx = next(i for i, p in enumerate(product_list) if p["id"] == pid)
            expected_stock = products[pid]["stock"] - item["quantity"]
            stock_path = f"$.db.products[{product_idx}].stock"
            assert (
                stock_path in jsonpaths
            ), f"grading.yaml has no jsonpath for {pid} stock (expected path: {stock_path})"
            assert abs(jsonpaths[stock_path] - expected_stock) < 0.001, (
                f"Stock for {pid}: grading.yaml says {jsonpaths[stock_path]}, "
                f"computed {expected_stock} "
                f"(initial {products[pid]['stock']} − qty {item['quantity']})"
            )
