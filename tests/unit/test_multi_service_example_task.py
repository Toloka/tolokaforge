"""Static-shape tests for the ``multi_service_example_01`` task pack.

The task pack under ``examples/native/multi_service/`` is the
demonstration workload for the shared+task-declared substrate path
(Case B in ADR-0018). Real docker execution lives in the docker
integration suite; here we pin the task pack's static shape so a
future refactor doesn't silently break the example.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.models import EnvironmentPatch
from tolokaforge.core.trial import TaskIsolation

pytestmark = pytest.mark.unit


_TASK_DIR = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "native"
    / "multi_service"
    / "dataset"
    / "tasks"
    / "multi_service"
    / "multi_service_example_01"
)


class TestTaskLayoutOnDisk:
    """The example is authored to be integration-test-friendly. That
    only holds if every declared file is actually present on disk."""

    def test_task_yaml_exists(self) -> None:
        assert (_TASK_DIR / "task.yaml").is_file()

    def test_compose_file_exists(self) -> None:
        assert (_TASK_DIR / "environment.compose.yaml").is_file()

    def test_grading_file_exists(self) -> None:
        assert (_TASK_DIR / "grading.yaml").is_file()

    def test_products_fixture_exists(self) -> None:
        """The compose file bind-mounts ``fixtures/products.json`` into
        ``app-service`` — the fixture must exist or nginx will 404."""
        assert (_TASK_DIR / "fixtures" / "products.json").is_file()


class TestTaskYaml:
    """The task.yaml declaration is what the orchestrator's
    ``_extract_run_env_manifest`` reads to route the run to Case B."""

    def test_loads_via_task_loader(self) -> None:
        task_config, task_root = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.task_id == "multi_service_example_01"
        assert task_root == _TASK_DIR

    def test_declares_environment_manifest(self) -> None:
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.environment_manifest is not None
        assert isinstance(task_config.environment_manifest, EnvironmentPatch)

    def test_isolation_is_shared_ok(self) -> None:
        """Case B requires ``isolation: shared_ok`` — a per_trial
        declaration would route to ``PerTrialRuntimeBackend`` instead
        of the new shared+env_manifest path."""
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        assert task_config.environment_manifest.isolation == TaskIsolation.SHARED_OK

    def test_runner_service_matches_compose_declaration(self) -> None:
        """``runner_service`` in the patch must name a service actually
        declared in the compose file — otherwise
        ``resolve_runner_endpoint`` fails at connect time with a typed
        ProvisionError."""
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        stack = task_config.environment_manifest.stack
        assert stack is not None
        assert stack.compose_file is not None
        compose_body = yaml.safe_load(stack.compose_file.read_text())
        declared_services = set(compose_body.get("services", {}).keys())
        assert stack.runner_service in declared_services

    def test_agent_tools_reach_app_service(self) -> None:
        """The task expects the agent to query ``app-service`` — it
        needs ``bash`` (which has curl/wget in the runner container) and
        ``write_file`` (to submit the report)."""
        task_config, _ = load_task_yaml(_TASK_DIR / "task.yaml")
        enabled = set(task_config.tools.agent.get("enabled", []))
        assert "bash" in enabled
        assert "write_file" in enabled


class TestComposeShape:
    """The compose file must declare the three services the Case B
    materialisation path needs to resolve: the ``runner`` named by the
    manifest, the ``db-service`` HTTP endpoint the runner's tool layer
    reaches for state persistence, plus the task-specific
    ``app-service`` this example exists to demonstrate."""

    def _load_compose(self) -> dict:
        return yaml.safe_load((_TASK_DIR / "environment.compose.yaml").read_text())

    def test_declares_three_services(self) -> None:
        compose = self._load_compose()
        assert set(compose["services"].keys()) == {"runner", "db-service", "app-service"}

    def test_runner_uses_local_alias(self) -> None:
        """The runner service references the ``:local`` alias applied
        by the engine at run start (see RUNTIME_BACKENDS.md). A
        floating tag (``:latest`` / ``:edge`` / …) would be rejected
        by the manifest validator."""
        compose = self._load_compose()
        assert compose["services"]["runner"]["image"] == "tolokaforge-runner:local"

    def test_db_service_uses_local_alias(self) -> None:
        compose = self._load_compose()
        assert compose["services"]["db-service"]["image"] == "tolokaforge-db-service:local"

    def test_app_service_is_pinned_nginx(self) -> None:
        """The example demonstrates a task-provided service — a pinned
        (non-floating) tag keeps runs reproducible and satisfies the
        manifest validator."""
        compose = self._load_compose()
        assert compose["services"]["app-service"]["image"].startswith("nginx:")
        # The tag isn't ``latest`` / ``edge`` / etc.
        tag = compose["services"]["app-service"]["image"].split(":", 1)[1]
        assert tag not in {"latest", "edge", "stable", "main", "master"}

    def test_runner_depends_on_health_of_downstreams(self) -> None:
        """The runner must wait for both db-service AND app-service to
        become healthy before starting — otherwise trials hit the
        runner before it can register tools that touch either."""
        compose = self._load_compose()
        depends = compose["services"]["runner"]["depends_on"]
        assert depends["db-service"]["condition"] == "service_healthy"
        assert depends["app-service"]["condition"] == "service_healthy"


class TestProductFixture:
    """The task's grading expects the top-3-by-price in-stock products.
    If the fixture ordering ever changes, the grading breaks silently
    (agent gets 0/4 on state checks). Pin the expected top-3."""

    def _load_products(self) -> list[dict]:
        import json

        return json.loads((_TASK_DIR / "fixtures" / "products.json").read_text())

    def test_top_three_in_stock_by_price(self) -> None:
        products = self._load_products()
        in_stock = [p for p in products if p["in_stock"]]
        top_three = sorted(in_stock, key=lambda p: p["price"], reverse=True)[:3]
        names = [p["name"] for p in top_three]
        # Names the grading references.
        assert names == ["Prism Workstation", "Cascade Desktop Pro", "Nebula Laptop 15"]
        # And the price the grading references (as an unformatted integer).
        assert int(top_three[0]["price"]) == 3299
