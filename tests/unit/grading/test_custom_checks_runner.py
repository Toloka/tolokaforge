"""
Integration tests for custom Python checks.

These tests verify that custom checks work correctly in a realistic
grading scenario.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

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

# Path to test task (tests/data/tasks/calc_custom_checks — 3 levels up from tests/unit/grading/)
TASK_DIR = Path(__file__).parent.parent.parent / "data" / "tasks" / "calc_custom_checks"


@pytest.fixture
def task_dir() -> Path:
    """Get path to calc_custom_checks task."""
    return TASK_DIR


@pytest.fixture
def checks_file(task_dir: Path) -> Path:
    """Get path to checks.py file."""
    return task_dir / "checks.py"


@pytest.fixture
def default_config() -> CustomChecksConfig:
    """Default config for custom checks."""
    return CustomChecksConfig(
        enabled=True,
        file="checks.py",
        interface_version="1.0",
        timeout_seconds=10,
    )


def create_context(
    initial_state: dict,
    final_state: dict,
    tool_calls: list | None = None,
) -> CheckContext:
    """Create a CheckContext with given states and tool calls."""
    messages = []
    if tool_calls:
        messages.append(
            Message(
                role="assistant",
                content="Processing...",
                tool_calls=[
                    ToolCall(name=tc["name"], arguments=tc.get("arguments", {}))
                    for tc in tool_calls
                ],
            )
        )

    return CheckContext(
        initial_state=EnvironmentState(data=initial_state),
        final_state=EnvironmentState(data=final_state),
        transcript=Transcript(messages=messages),
        task=TaskContext(task_id="calc_custom_checks", task_name="Test Task"),
    )


class TestMinimalCustomChecks:
    """Tests using the calc_custom_checks test task."""

    def test_checks_load_successfully(
        self, checks_file: Path, task_dir: Path, default_config: CustomChecksConfig
    ):
        """Verify the checks.py file can be loaded."""
        runner = CheckRunner()

        # Create minimal context just for loading
        ctx = create_context(
            initial_state={"counter": 0},
            final_state={"counter": 0},
        )

        # Should not raise
        result = runner.run(checks_file, task_dir, ctx, default_config)

        # Should have loaded the checks
        assert result.error is None
        assert result.total > 0

    def test_counter_incremented_passes(
        self, checks_file: Path, task_dir: Path, default_config: CustomChecksConfig
    ):
        """Test that counter_was_incremented check passes when counter increases."""
        runner = CheckRunner()

        ctx = create_context(
            initial_state={"counter": 0, "operations": []},
            final_state={"counter": 7, "operations": []},
            tool_calls=[{"name": "update_counter", "arguments": {"delta": 7}}],
        )

        result = runner.run(checks_file, task_dir, ctx, default_config)

        assert result.error is None

        # Find the counter_was_incremented result
        counter_check = next(
            (r for r in result.results if r.check_name == "counter_was_incremented"),
            None,
        )
        assert counter_check is not None
        assert counter_check.status == CheckStatus.PASSED

    def test_counter_not_incremented_fails(
        self, checks_file: Path, task_dir: Path, default_config: CustomChecksConfig
    ):
        """Test that counter_was_incremented check fails when counter doesn't change."""
        runner = CheckRunner()

        ctx = create_context(
            initial_state={"counter": 5, "operations": []},
            final_state={"counter": 5, "operations": []},  # No change
        )

        result = runner.run(checks_file, task_dir, ctx, default_config)

        counter_check = next(
            (r for r in result.results if r.check_name == "counter_was_incremented"),
            None,
        )
        assert counter_check is not None
        assert counter_check.status == CheckStatus.FAILED

    def test_counter_reached_target_passes(
        self, checks_file: Path, task_dir: Path, default_config: CustomChecksConfig
    ):
        """Test counter_reached_target check when counter >= 5."""
        runner = CheckRunner()

        ctx = create_context(
            initial_state={"counter": 0, "operations": []},
            final_state={"counter": 5, "operations": []},
        )

        result = runner.run(checks_file, task_dir, ctx, default_config)

        target_check = next(
            (r for r in result.results if r.check_name == "counter_reached_target"),
            None,
        )
        assert target_check is not None
        assert target_check.status == CheckStatus.PASSED

    def test_counter_reached_target_partial_credit(
        self, checks_file: Path, task_dir: Path, default_config: CustomChecksConfig
    ):
        """Test counter_reached_target gives partial credit when counter < 5."""
        runner = CheckRunner()

        ctx = create_context(
            initial_state={"counter": 0, "operations": []},
            final_state={"counter": 3, "operations": []},  # Only 3/5
        )

        result = runner.run(checks_file, task_dir, ctx, default_config)

        target_check = next(
            (r for r in result.results if r.check_name == "counter_reached_target"),
            None,
        )
        assert target_check is not None
        assert target_check.status == CheckStatus.FAILED
        # Should have partial credit: 3/5 = 0.6
        assert target_check.score == pytest.approx(0.6, 0.01)

    def test_all_checks_pass_scenario(
        self, checks_file: Path, task_dir: Path, default_config: CustomChecksConfig
    ):
        """Test a scenario where all applicable checks pass."""
        runner = CheckRunner()

        ctx = create_context(
            initial_state={"counter": 0, "operations": []},
            final_state={"counter": 10, "operations": ["increment by 5", "increment by 5"]},
            tool_calls=[
                {"name": "update_counter", "arguments": {"delta": 5}},
                {"name": "update_counter", "arguments": {"delta": 5}},
            ],
        )

        result = runner.run(checks_file, task_dir, ctx, default_config)

        assert result.error is None

        # Count results by status
        passed = [r for r in result.results if r.status == CheckStatus.PASSED]
        failed = [r for r in result.results if r.status == CheckStatus.FAILED]
        [r for r in result.results if r.status == CheckStatus.SKIPPED]

        # All checks should pass or skip (operations_were_logged may skip if no ops)
        assert len(failed) == 0
        assert (
            len(passed) >= 3
        )  # counter_was_incremented, counter_reached_target, update_counter_tool_was_called

        # Aggregate score should be high
        assert result.aggregate_score >= 0.9

    def test_result_to_score(
        self, checks_file: Path, task_dir: Path, default_config: CustomChecksConfig
    ):
        """Test converting results to score and reason."""
        runner = CheckRunner()

        ctx = create_context(
            initial_state={"counter": 0, "operations": []},
            final_state={"counter": 5, "operations": ["op1"]},
            tool_calls=[{"name": "update_counter"}],
        )

        result = runner.run(checks_file, task_dir, ctx, default_config)
        score, reason = runner.result_to_score(result, default_config)

        # Should have a reasonable score
        assert 0.0 <= score <= 1.0

        # Reason should contain check names
        assert "counter_was_incremented" in reason or "✓" in reason

    def test_timeout_enforcement(
        self, task_dir: Path, default_config: CustomChecksConfig, tmp_path: Path
    ):
        """Test that slow checks are terminated."""
        # Create a slow checks.py
        slow_checks = tmp_path / "checks.py"
        slow_checks.write_text(
            """
import time
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, init, check
)

@init(interface_version="1.0")
def setup(ctx: CheckContext):
    pass

@check
def slow_check():
    time.sleep(10)  # Sleep for 10 seconds
    return CheckPassed("Never reached")
"""
        )

        config = CustomChecksConfig(
            enabled=True,
            timeout_seconds=1,  # 1 second timeout
        )

        runner = CheckRunner()
        ctx = create_context(
            initial_state={},
            final_state={},
        )

        result = runner.run(slow_checks, tmp_path, ctx, config)

        # Should have timed out
        assert result.error is not None
        assert "timed out" in result.error.lower()
