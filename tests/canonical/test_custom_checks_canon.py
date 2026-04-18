"""
Canonical tests for custom checks using the food_delivery_2 project.

These tests use actual trial data from the food_delivery_2 project to verify
that custom checks work correctly with real-world scenarios.

Uses shared fixtures from tests/utils/project_fixtures.py where possible.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.canonical

# Use existing project fixtures utilities
from tests.utils.project_fixtures import (
    TEST_PROJECTS_DIR,
)
from tolokaforge.core.grading.check_runner import CheckRunner
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckStatus,
    CustomChecksConfig,
    EnvironmentState,
    Message,
    TaskContext,
    ToolCall,
    Transcript,
)

# Path constants for this specific test task
PROJECT_DIR = TEST_PROJECTS_DIR / "food_delivery_2"
TASK_ID = "order_modify_with_checks"
TASK_DIR = PROJECT_DIR / "tasks" / TASK_ID


def load_yaml(path: Path) -> dict:
    """Load a YAML file."""
    with open(path) as f:
        return yaml.safe_load(f)


@pytest.fixture
def task_dir() -> Path:
    """Get path to task directory."""
    return TASK_DIR


@pytest.fixture
def project_dir() -> Path:
    """Get path to project directory."""
    return PROJECT_DIR


@pytest.fixture
def checks_file(task_dir: Path) -> Path:
    """Get path to checks.py file."""
    return task_dir / "checks.py"


@pytest.fixture
def grading_config(task_dir: Path) -> dict:
    """Load grading configuration."""
    return load_yaml(task_dir / "grading.yaml")


@pytest.fixture
def custom_checks_config(grading_config: dict) -> CustomChecksConfig:
    """Create CustomChecksConfig from grading.yaml."""
    cc = grading_config.get("custom_checks", {})
    return CustomChecksConfig(
        enabled=cc.get("enabled", True),
        file=cc.get("file", "checks.py"),
        relative_imports=cc.get("relative_imports", []),
        timeout_seconds=cc.get("timeout_seconds", 30),
        interface_version=cc.get("interface_version", "1.0"),
    )


class TestFoodDeliveryCheckHelpers:
    """Tests for food_delivery_2 project-level check helpers."""

    @pytest.mark.skipif(not PROJECT_DIR.exists(), reason="Project directory not found")
    def test_helpers_module_importable(self, project_dir: Path):
        """Test that check_helpers.py can be imported."""
        import importlib.util

        # Load module directly from file to avoid cache issues
        spec = importlib.util.spec_from_file_location(
            "food_delivery_check_helpers", project_dir / "check_helpers.py"
        )
        check_helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check_helpers)

        # Verify expected functions exist
        assert hasattr(check_helpers, "get_order")
        assert hasattr(check_helpers, "get_user")
        assert hasattr(check_helpers, "get_menu_item_quantity")
        assert hasattr(check_helpers, "order_was_modified")
        assert hasattr(check_helpers, "validate_order_modification_sequence")


class TestCheckWithSimulatedScenarios:
    """Tests with simulated scenarios to verify edge cases."""

    @pytest.fixture
    def checks_file(self) -> Path:
        return TASK_DIR / "checks.py"

    @pytest.fixture
    def config(self) -> CustomChecksConfig:
        return CustomChecksConfig(
            enabled=True,
            file="checks.py",
            relative_imports=["../.."],
            timeout_seconds=10,
            interface_version="1.0",
        )

    def _create_order_state(
        self,
        branzino_qty: int = 2,
        oysters_qty: int = 1,
    ) -> dict[str, Any]:
        """Create a simulated state with specified quantities."""
        return {
            "agent": {
                "orders": {
                    "order_53": {
                        "order_id": "order_53",
                        "user_id": "user_5247",
                        "restaurant_id": "restaurant_41005549",
                        "menu_items_list": [
                            {
                                "item_id": "restaurant_41005549_item_1",
                                "name": "Whole Roasted Branzino with Mediterranean Herbs",
                                "quantity": branzino_qty,
                                "price": 1271,
                            },
                            {
                                "item_id": "restaurant_41005549_item_0",
                                "name": "Mole Poblano",
                                "quantity": 3,
                                "price": 1628,
                            },
                            {
                                "item_id": "restaurant_41005549_item_7",
                                "name": "Oysters Rockefeller",
                                "quantity": oysters_qty,
                                "price": 1478,
                            },
                        ],
                        "status": "Pending",
                        "total_price": 10735,
                        "updated_at": "2025-03-02T13:00:00",
                    }
                },
                "users": {
                    "user_5247": {
                        "user_id": "user_5247",
                        "name": "Katrina Alexander",
                    }
                },
            }
        }

    def _create_context_with_tool_calls(
        self,
        initial_state: dict,
        final_state: dict,
        tool_calls: list[dict],
    ) -> CheckContext:
        """Create a CheckContext with specified states and tool calls."""
        tc_list = [
            ToolCall(name=tc["name"], arguments=tc.get("arguments", {})) for tc in tool_calls
        ]

        return CheckContext(
            initial_state=EnvironmentState(data=initial_state),
            final_state=EnvironmentState(data=final_state),
            transcript=Transcript(
                messages=[Message(role="assistant", content="...", tool_calls=tc_list)]
            ),
            task=TaskContext(task_id=TASK_ID, task_name="Test"),
        )

    @pytest.mark.skipif(not TASK_DIR.exists(), reason="Task directory not found")
    def test_branzino_increased_correctly(self, checks_file: Path, config: CustomChecksConfig):
        """Test check passes when Branzino is correctly increased."""
        initial = self._create_order_state(branzino_qty=2, oysters_qty=1)
        final = self._create_order_state(branzino_qty=3, oysters_qty=1)
        final["agent"]["orders"]["order_53"]["updated_at"] = "2025-05-15T00:00:00"

        ctx = self._create_context_with_tool_calls(
            initial,
            final,
            [
                {"name": "get_user_details", "arguments": {"user_id": "user_5247"}},
                {"name": "get_order_details", "arguments": {"order_id": "order_53"}},
                {"name": "modify_order", "arguments": {"order_id": "order_53"}},
            ],
        )

        runner = CheckRunner()
        result = runner.run(checks_file, TASK_DIR, ctx, config)

        check = next(
            (r for r in result.results if r.check_name == "branzino_quantity_increased"),
            None,
        )
        assert check is not None
        assert check.status == CheckStatus.PASSED

    @pytest.mark.skipif(not TASK_DIR.exists(), reason="Task directory not found")
    def test_oysters_unchanged(self, checks_file: Path, config: CustomChecksConfig):
        """Test check passes when Oysters quantity remains unchanged."""
        initial = self._create_order_state(branzino_qty=2, oysters_qty=1)
        final = self._create_order_state(branzino_qty=3, oysters_qty=1)
        final["agent"]["orders"]["order_53"]["updated_at"] = "2025-05-15T00:00:00"

        ctx = self._create_context_with_tool_calls(
            initial,
            final,
            [
                {"name": "get_user_details", "arguments": {"user_id": "user_5247"}},
                {"name": "get_order_details", "arguments": {"order_id": "order_53"}},
                {"name": "modify_order", "arguments": {"order_id": "order_53"}},
            ],
        )

        runner = CheckRunner()
        result = runner.run(checks_file, TASK_DIR, ctx, config)

        check = next(
            (r for r in result.results if r.check_name == "oysters_quantity_unchanged"),
            None,
        )
        assert check is not None
        assert check.status == CheckStatus.PASSED

    @pytest.mark.skipif(not TASK_DIR.exists(), reason="Task directory not found")
    def test_oysters_changed_fails(self, checks_file: Path, config: CustomChecksConfig):
        """Test check fails when Oysters quantity is incorrectly changed."""
        initial = self._create_order_state(branzino_qty=2, oysters_qty=1)
        # Oysters reduced to 0 - should fail
        final = self._create_order_state(branzino_qty=3, oysters_qty=0)
        final["agent"]["orders"]["order_53"]["updated_at"] = "2025-05-15T00:00:00"

        ctx = self._create_context_with_tool_calls(
            initial,
            final,
            [
                {"name": "get_user_details", "arguments": {"user_id": "user_5247"}},
                {"name": "get_order_details", "arguments": {"order_id": "order_53"}},
                {"name": "modify_order", "arguments": {"order_id": "order_53"}},
            ],
        )

        runner = CheckRunner()
        result = runner.run(checks_file, TASK_DIR, ctx, config)

        check = next(
            (r for r in result.results if r.check_name == "oysters_quantity_unchanged"),
            None,
        )
        assert check is not None
        assert check.status == CheckStatus.FAILED

    @pytest.mark.skipif(not TASK_DIR.exists(), reason="Task directory not found")
    def test_transfer_to_human_fails(self, checks_file: Path, config: CustomChecksConfig):
        """Test check fails when agent transfers to human operator."""
        initial = self._create_order_state()
        final = self._create_order_state()

        # Include transfer_to_human_agents call
        ctx = self._create_context_with_tool_calls(
            initial,
            final,
            [
                {"name": "get_user_details", "arguments": {"user_id": "user_5247"}},
                {"name": "transfer_to_human_agents", "arguments": {}},  # Bad!
            ],
        )

        runner = CheckRunner()
        result = runner.run(checks_file, TASK_DIR, ctx, config)

        check = next(
            (r for r in result.results if r.check_name == "no_transfer_to_human"),
            None,
        )
        assert check is not None
        assert check.status == CheckStatus.FAILED

    @pytest.mark.skipif(not TASK_DIR.exists(), reason="Task directory not found")
    def test_workflow_sequence_validates(self, checks_file: Path, config: CustomChecksConfig):
        """Test that proper workflow sequence is recognized."""
        initial = self._create_order_state()
        final = self._create_order_state(branzino_qty=3)
        final["agent"]["orders"]["order_53"]["updated_at"] = "2025-05-15T00:00:00"

        # Correct sequence
        ctx = self._create_context_with_tool_calls(
            initial,
            final,
            [
                {"name": "get_user_details", "arguments": {"user_id": "user_5247"}},
                {"name": "get_order_details", "arguments": {"order_id": "order_53"}},
                {"name": "modify_order", "arguments": {"order_id": "order_53"}},
            ],
        )

        runner = CheckRunner()
        result = runner.run(checks_file, TASK_DIR, ctx, config)

        check = next(
            (r for r in result.results if r.check_name == "proper_workflow_sequence"),
            None,
        )
        assert check is not None
        assert check.status == CheckStatus.PASSED
