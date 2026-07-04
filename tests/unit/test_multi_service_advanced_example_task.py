"""Static-shape tests for the ``orders_customers_join_01`` task pack.

The task pack under ``examples/native/multi_service_advanced/`` is the
Phase 4 demonstration workload for the shared+task-declared substrate
path with **multiple** task-specific HTTP services (Case B in ADR-0018).
Real docker execution lives in the docker integration suite; here we pin
the task pack's static shape so a future refactor doesn't silently break
the example.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.trial import EnvironmentManifest, TaskIsolation

pytestmark = pytest.mark.unit


_TASK_DIR = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "native"
    / "multi_service_advanced"
    / "dataset"
    / "tasks"
    / "multi_service"
    / "orders_customers_join_01"
)


class TestTaskLayoutOnDisk:
    def test_task_yaml_exists(self) -> None:
        assert (_TASK_DIR / "task.yaml").is_file()

    def test_compose_file_exists(self) -> None:
        assert (_TASK_DIR / "environment.compose.yaml").is_file()

    def test_grading_file_exists(self) -> None:
        assert (_TASK_DIR / "grading.yaml").is_file()

    def test_orders_fixture_exists(self) -> None:
        assert (_TASK_DIR / "fixtures" / "orders.json").is_file()

    def test_customers_fixture_exists(self) -> None:
        assert (_TASK_DIR / "fixtures" / "customers.json").is_file()


class TestTaskYaml:
    def test_loads_via_task_loader(self) -> None:
        task_config, task_root = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.task_id == "orders_customers_join_01"
        assert task_root == _TASK_DIR

    def test_declares_environment_manifest(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.environment_manifest is not None
        assert isinstance(task_config.environment_manifest, EnvironmentManifest)

    def test_isolation_is_shared_ok(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.environment_manifest.isolation == TaskIsolation.SHARED_OK

    def test_runner_service_matches_compose_declaration(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        compose_body = yaml.safe_load(task_config.environment_manifest.compose_file.read_text())
        declared_services = set(compose_body.get("services", {}).keys())
        assert task_config.environment_manifest.runner_service in declared_services

    def test_agent_tools_reach_apis(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        enabled = set(task_config.tools.agent.get("enabled", []))
        assert "bash" in enabled
        assert "write_file" in enabled


class TestComposeShape:
    """The compose file must declare four services: the runner +
    db-service + the two task-specific HTTP services (orders-api +
    customers-api)."""

    def _load_compose(self) -> dict:
        return yaml.safe_load((_TASK_DIR / "environment.compose.yaml").read_text())

    def test_declares_four_services(self) -> None:
        compose = self._load_compose()
        assert set(compose["services"].keys()) == {
            "runner",
            "db-service",
            "orders-api",
            "customers-api",
        }

    def test_runner_uses_local_alias(self) -> None:
        compose = self._load_compose()
        assert compose["services"]["runner"]["image"] == "tolokaforge-runner:local"

    def test_db_service_uses_local_alias(self) -> None:
        compose = self._load_compose()
        assert compose["services"]["db-service"]["image"] == "tolokaforge-db-service:local"

    def test_app_services_use_pinned_nginx(self) -> None:
        compose = self._load_compose()
        for svc in ("orders-api", "customers-api"):
            image = compose["services"][svc]["image"]
            assert image.startswith("nginx:")
            tag = image.split(":", 1)[1]
            assert tag not in {"latest", "edge", "stable", "main", "master"}

    def test_runner_depends_on_health_of_downstreams(self) -> None:
        """The runner must wait for db-service AND both APIs to become
        healthy before starting — otherwise trials hit the runner
        before the substrate is ready."""
        compose = self._load_compose()
        depends = compose["services"]["runner"]["depends_on"]
        assert depends["db-service"]["condition"] == "service_healthy"
        assert depends["orders-api"]["condition"] == "service_healthy"
        assert depends["customers-api"]["condition"] == "service_healthy"

    def test_fixture_bind_mounts_relative(self) -> None:
        """The compose file uses relative bind-mount paths (validator
        rejects absolute or ``..`` paths)."""
        compose = self._load_compose()
        orders_mount = compose["services"]["orders-api"]["volumes"][0]
        customers_mount = compose["services"]["customers-api"]["volumes"][0]
        assert orders_mount.startswith("./fixtures/orders.json:")
        assert customers_mount.startswith("./fixtures/customers.json:")


class TestFixtureAggregation:
    """The task's grading pins the top-3 customers by paid-order total.
    A silent fixture reorder would score every future correct agent 0/6
    on state_checks. Pin the aggregation up-front."""

    def _load_json(self, name: str) -> list[dict]:
        import json

        return json.loads((_TASK_DIR / "fixtures" / name).read_text())

    def test_top_three_customers_by_paid_total(self) -> None:
        orders = self._load_json("orders.json")
        customers = {c["customer_id"]: c for c in self._load_json("customers.json")}
        totals: dict[str, int] = {}
        for order in orders:
            if order["status"] != "paid":
                continue
            totals[order["customer_id"]] = totals.get(order["customer_id"], 0) + order["amount"]
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:3]
        names = [customers[cid]["name"] for cid, _ in ranked]
        values = [total for _, total in ranked]
        assert names == ["Acme Robotics", "Vector Industries", "Nimbus Analytics"]
        assert values == [12000, 8500, 5000]

    def test_status_filter_actually_matters(self) -> None:
        """The fixture must include non-``paid`` orders large enough to
        change the ranking if an agent skips the ``status: paid`` filter.
        Without this the task-yaml's "only count paid orders" instruction
        is a formality — the grading would pass whether or not the agent
        respects it. Pins that the fixture actually exercises the filter."""
        orders = self._load_json("orders.json")
        customers = {c["customer_id"]: c for c in self._load_json("customers.json")}
        totals_unfiltered: dict[str, int] = {}
        for order in orders:
            totals_unfiltered[order["customer_id"]] = (
                totals_unfiltered.get(order["customer_id"], 0) + order["amount"]
            )
        wrong_ranked = sorted(totals_unfiltered.items(), key=lambda kv: kv[1], reverse=True)[:3]
        wrong_names = [customers[cid]["name"] for cid, _ in wrong_ranked]
        # The unfiltered top-3 must be different from the filtered top-3
        # (otherwise the filter is decorative).
        assert wrong_names != ["Acme Robotics", "Vector Industries", "Nimbus Analytics"]
