"""
Unit tests for the custom Python checks system.

Tests cover:
- Pydantic models in checks_interface.py
- Decorator registration
- Helper functions in checks_helpers.py
- CheckRunner execution
"""

from pathlib import Path

import pytest

from tolokaforge.core.grading.check_runner import CheckRunner
from tolokaforge.core.grading.checks_helpers import (
    check_dict_params,
    count_by_key,
    count_tool_calls,
    dict_diff,
    filter_by_key,
    find_by_key,
    find_tool_calls,
    first_tool_name,
    get_nested,
    get_tool_argument,
    last_tool_name,
    normalize_whitespace,
    text_contains_all,
    text_contains_any,
    text_matches_pattern,
    tool_was_called,
)
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckFailed,
    CheckPassed,
    CheckResult,
    CheckResultSet,
    CheckSkipped,
    CheckStatus,
    CustomChecksConfig,
    EnvironmentState,
    Message,
    TaskContext,
    ToolCall,
    ToolCallStatus,
    Transcript,
    check,
    get_init_func,
    get_interface_version,
    get_registered_checks,
    init,
    reset_registry,
)

pytestmark = pytest.mark.unit

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_tool_call() -> ToolCall:
    """Create a sample tool call"""
    return ToolCall(
        name="get_user",
        arguments={"user_id": "user_123"},
        result='{"name": "John", "email": "john@example.com"}',
        status=ToolCallStatus.SUCCESS,
    )


@pytest.fixture
def sample_message(sample_tool_call: ToolCall) -> Message:
    """Create a sample message with tool call"""
    return Message(
        role="assistant",
        content="Let me look up that user for you.",
        tool_calls=[sample_tool_call],
    )


@pytest.fixture
def sample_transcript(sample_message: Message) -> Transcript:
    """Create a sample transcript"""
    return Transcript(
        messages=[
            Message(role="user", content="Show me user 123"),
            sample_message,
            Message(role="assistant", content="Found user John."),
        ]
    )


@pytest.fixture
def sample_initial_state() -> EnvironmentState:
    """Create sample initial state"""
    return EnvironmentState(
        data={
            "users": {
                "user_123": {"name": "John", "email": "john@example.com", "status": "active"},
            },
            "orders": {},
        }
    )


@pytest.fixture
def sample_final_state() -> EnvironmentState:
    """Create sample final state"""
    return EnvironmentState(
        data={
            "users": {
                "user_123": {"name": "John", "email": "john.new@example.com", "status": "active"},
            },
            "orders": {
                "order_1": {"status": "created", "user_id": "user_123"},
            },
        }
    )


@pytest.fixture
def sample_context(
    sample_initial_state: EnvironmentState,
    sample_final_state: EnvironmentState,
    sample_transcript: Transcript,
) -> CheckContext:
    """Create a complete check context"""
    return CheckContext(
        initial_state=sample_initial_state,
        final_state=sample_final_state,
        transcript=sample_transcript,
        task=TaskContext(
            task_id="test-task-001",
            task_name="Test Task",
            task_description="A test task for unit testing",
            domain="test",
        ),
    )


@pytest.fixture(autouse=True)
def reset_check_registry():
    """Reset the decorator registry before each test"""
    reset_registry()
    yield
    reset_registry()


# =============================================================================
# Tests: Pydantic Models
# =============================================================================


class TestToolCall:
    """Tests for ToolCall model"""

    def test_create_with_all_fields(self):
        tc = ToolCall(
            name="update_user",
            arguments={"user_id": "123", "email": "new@example.com"},
            result="success",
            status=ToolCallStatus.SUCCESS,
        )
        assert tc.name == "update_user"
        assert tc.arguments["user_id"] == "123"
        assert tc.result == "success"


class TestTranscript:
    """Tests for Transcript model"""

    def test_all_tool_calls(self, sample_transcript: Transcript):
        calls = sample_transcript.all_tool_calls
        assert len(calls) == 1
        assert calls[0].name == "get_user"

    def test_last_assistant_response(self, sample_transcript: Transcript):
        response = sample_transcript.last_assistant_response
        assert response == "Found user John."


class TestEnvironmentState:
    """Tests for EnvironmentState model"""

    def test_get_simple_path(self, sample_initial_state: EnvironmentState):
        users = sample_initial_state.get("users")
        assert isinstance(users, dict)
        assert "user_123" in users

    def test_get_nested_path(self, sample_initial_state: EnvironmentState):
        name = sample_initial_state.get("users.user_123.name")
        assert name == "John"

    def test_get_missing_path(self, sample_initial_state: EnvironmentState):
        result = sample_initial_state.get("nonexistent.path", default="missing")
        assert result == "missing"


class TestCheckContext:
    """Tests for CheckContext model"""

    def test_effects_property(self, sample_context: CheckContext):
        effects = sample_context.effects
        assert "orders" in effects
        assert "order_1" in effects["orders"]

    def test_tool_calls_property(self, sample_context: CheckContext):
        tool_calls = sample_context.tool_calls
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "get_user"


class TestCheckResults:
    """Tests for CheckResult models"""

    def test_check_passed(self):
        result = CheckPassed(message="All good", score=1.0)
        assert result.message == "All good"
        assert result.score == 1.0
        assert result.details == {}

    def test_check_passed_with_details(self):
        result = CheckPassed(message="Found 3 items", score=0.9, details={"count": 3})
        assert result.details["count"] == 3

    def test_check_failed(self):
        result = CheckFailed(message="Not found", score=0.0)
        assert result.message == "Not found"
        assert result.score == 0.0

    def test_check_skipped(self):
        result = CheckSkipped(message="Precondition not met")
        assert result.message == "Precondition not met"


class TestCheckResultSet:
    """Tests for CheckResultSet"""

    def test_aggregate_score(self):
        result_set = CheckResultSet(
            results=[
                CheckResult(check_name="c1", status=CheckStatus.PASSED, score=1.0),
                CheckResult(check_name="c2", status=CheckStatus.PASSED, score=0.5),
                CheckResult(check_name="c3", status=CheckStatus.FAILED, score=0.0),
            ]
        )
        assert result_set.aggregate_score == pytest.approx(0.5, 0.01)
        assert result_set.passed == 2
        assert result_set.failed == 1
        assert result_set.total == 3

    def test_aggregate_score_excludes_skipped(self):
        result_set = CheckResultSet(
            results=[
                CheckResult(check_name="c1", status=CheckStatus.PASSED, score=1.0),
                CheckResult(check_name="c2", status=CheckStatus.SKIPPED, score=0.0),
            ]
        )
        assert result_set.aggregate_score == 1.0
        assert result_set.skipped == 1

    def test_all_passed(self):
        result_set = CheckResultSet(
            results=[
                CheckResult(check_name="c1", status=CheckStatus.PASSED, score=1.0),
                CheckResult(check_name="c2", status=CheckStatus.PASSED, score=1.0),
            ]
        )
        assert result_set.all_passed is True

    def test_not_all_passed(self):
        result_set = CheckResultSet(
            results=[
                CheckResult(check_name="c1", status=CheckStatus.PASSED, score=1.0),
                CheckResult(check_name="c2", status=CheckStatus.FAILED, score=0.0),
            ]
        )
        assert result_set.all_passed is False


# =============================================================================
# Tests: Decorators
# =============================================================================


class TestDecorators:
    """Tests for @init and @check decorators"""

    def test_check_decorator_registers(self):
        @check
        def my_check():
            return CheckPassed("ok")

        checks = get_registered_checks()
        assert "my_check" in checks
        assert checks["my_check"]() == CheckPassed(message="ok")

    def test_multiple_checks_registered(self):
        @check
        def check_one():
            return CheckPassed("one")

        @check
        def check_two():
            return CheckFailed("two")

        checks = get_registered_checks()
        assert len(checks) == 2
        assert "check_one" in checks
        assert "check_two" in checks

    def test_init_decorator_registers(self):
        @init(interface_version="1.0")
        def setup(ctx: CheckContext):
            pass

        init_func = get_init_func()
        assert init_func is not None
        assert get_interface_version() == "1.0"

    def test_reset_registry(self):
        @check
        def test_check():
            return CheckPassed("test")

        @init(interface_version="1.0")
        def setup(ctx: CheckContext):
            pass

        assert len(get_registered_checks()) == 1
        assert get_init_func() is not None

        reset_registry()

        assert len(get_registered_checks()) == 0
        assert get_init_func() is None


# =============================================================================
# Tests: Helper Functions
# =============================================================================


class TestDictHelpers:
    """Tests for dictionary helper functions"""

    def test_check_dict_params_all_match(self):
        data = {"status": "active", "email": "test@example.com"}
        errors = check_dict_params(data, {"status": "active", "email": "test@example.com"})
        assert errors == []

    def test_check_dict_params_mismatch(self):
        data = {"status": "inactive", "email": "test@example.com"}
        errors = check_dict_params(data, {"status": "active"})
        assert len(errors) == 1
        assert "status" in errors[0]
        assert "active" in errors[0]

    def test_check_dict_params_with_prefix(self):
        data = {"status": "inactive"}
        errors = check_dict_params(data, {"status": "active"}, prefix="user")
        assert "user.status" in errors[0]

    def test_dict_diff(self):
        dict1 = {"a": 1, "b": 2, "c": 3}
        dict2 = {"a": 1, "b": 5, "d": 4}
        diffs = dict_diff(dict1, dict2)
        assert "b" in diffs
        assert diffs["b"] == (2, 5)
        assert "c" in diffs
        assert "d" in diffs

    def test_dict_diff_with_exclude(self):
        dict1 = {"a": 1, "updated_at": "old"}
        dict2 = {"a": 1, "updated_at": "new"}
        diffs = dict_diff(dict1, dict2, exclude_keys=["updated_at"])
        assert len(diffs) == 0

    def test_get_nested_dict(self):
        data = {"users": {"user_1": {"name": "John"}}}
        assert get_nested(data, "users.user_1.name") == "John"
        assert get_nested(data, "users.user_1.missing", default="X") == "X"

    def test_get_nested_list(self):
        data = {"items": [{"id": 1}, {"id": 2}]}
        assert get_nested(data, "items.0.id") == 1
        assert get_nested(data, "items.1.id") == 2


class TestToolCallHelpers:
    """Tests for tool call helper functions"""

    def test_last_tool_name_with_objects(self):
        calls = [ToolCall(name="tool_a"), ToolCall(name="tool_b")]
        assert last_tool_name(calls) == "tool_b"

    def test_last_tool_name_with_dicts(self):
        calls = [{"name": "tool_a"}, {"name": "tool_b"}]
        assert last_tool_name(calls) == "tool_b"

    def test_last_tool_name_empty(self):
        assert last_tool_name([]) is None

    def test_first_tool_name(self):
        calls = [ToolCall(name="tool_a"), ToolCall(name="tool_b")]
        assert first_tool_name(calls) == "tool_a"

    def test_count_tool_calls_all(self):
        calls = [ToolCall(name="a"), ToolCall(name="b"), ToolCall(name="a")]
        assert count_tool_calls(calls) == 3

    def test_count_tool_calls_filtered(self):
        calls = [ToolCall(name="a"), ToolCall(name="b"), ToolCall(name="a")]
        assert count_tool_calls(calls, "a") == 2
        assert count_tool_calls(calls, "b") == 1
        assert count_tool_calls(calls, "c") == 0

    def test_find_tool_calls(self):
        calls = [ToolCall(name="a"), ToolCall(name="b"), ToolCall(name="a")]
        found = find_tool_calls(calls, "a")
        assert len(found) == 2
        assert all(tc.name == "a" for tc in found)

    def test_tool_was_called(self):
        calls = [ToolCall(name="a"), ToolCall(name="b")]
        assert tool_was_called(calls, "a") is True
        assert tool_was_called(calls, "c") is False

    def test_get_tool_argument(self):
        call = ToolCall(name="update", arguments={"id": 123, "value": "test"})
        assert get_tool_argument(call, "id") == 123
        assert get_tool_argument(call, "value") == "test"
        assert get_tool_argument(call, "missing", default="default") == "default"


class TestTextHelpers:
    """Tests for text helper functions"""

    def test_text_contains_any_found(self):
        assert text_contains_any("Error occurred", ["error", "warning"]) is True

    def test_text_contains_any_not_found(self):
        assert text_contains_any("All good", ["error", "warning"]) is False

    def test_text_contains_any_case_sensitive(self):
        assert text_contains_any("Error", ["error"], case_sensitive=True) is False
        assert text_contains_any("error", ["error"], case_sensitive=True) is True

    def test_text_contains_all(self):
        assert text_contains_all("hello world", ["hello", "world"]) is True
        assert text_contains_all("hello world", ["hello", "foo"]) is False

    def test_text_matches_pattern(self):
        assert text_matches_pattern("Order #12345", r"Order #\d+") is True
        assert text_matches_pattern("Order ABC", r"Order #\d+") is False

    def test_normalize_whitespace(self):
        assert normalize_whitespace("  hello   world  ") == "hello world"
        assert normalize_whitespace("a\n\tb") == "a b"


class TestCollectionHelpers:
    """Tests for collection helper functions"""

    def test_find_by_key(self):
        items = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        found = find_by_key(items, "id", 2)
        assert found is not None
        assert found["name"] == "b"

    def test_find_by_key_not_found(self):
        items = [{"id": 1, "name": "a"}]
        found = find_by_key(items, "id", 999)
        assert found is None

    def test_filter_by_key(self):
        items = [
            {"status": "active", "id": 1},
            {"status": "inactive", "id": 2},
            {"status": "active", "id": 3},
        ]
        active = filter_by_key(items, "status", "active")
        assert len(active) == 2

    def test_count_by_key(self):
        items = [
            {"status": "active"},
            {"status": "active"},
            {"status": "inactive"},
        ]
        assert count_by_key(items, "status", "active") == 2
        assert count_by_key(items, "status", "inactive") == 1


# =============================================================================
# Tests: CheckRunner
# =============================================================================


class TestCheckRunner:
    """Tests for CheckRunner execution"""

    def test_run_simple_checks(self, sample_context: CheckContext, tmp_path: Path):
        """Test running a simple checks.py"""
        checks_file = tmp_path / "checks.py"
        checks_file.write_text(
            """
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, CheckFailed, init, check
)

@init(interface_version="1.0")
def setup(ctx: CheckContext):
    global order_created
    order_created = "order_1" in ctx.effects.get("orders", {})

@check
def order_was_created():
    if order_created:
        return CheckPassed("Order was created")
    return CheckFailed("Order was not created")

@check
def always_passes():
    return CheckPassed("This always passes")
"""
        )

        config = CustomChecksConfig(enabled=True, timeout_seconds=30)
        runner = CheckRunner()
        result = runner.run(checks_file, tmp_path, sample_context, config)

        assert result.error is None
        assert result.total == 2
        assert result.passed == 2
        assert result.all_passed

    def test_run_with_failures(self, sample_context: CheckContext, tmp_path: Path):
        """Test running checks with some failures"""
        checks_file = tmp_path / "checks.py"
        checks_file.write_text(
            """
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, CheckFailed, init, check
)

@check
def passing_check():
    return CheckPassed("ok")

@check
def failing_check():
    return CheckFailed("not ok")
"""
        )

        config = CustomChecksConfig(enabled=True)
        runner = CheckRunner()
        result = runner.run(checks_file, tmp_path, sample_context, config)

        assert result.passed == 1
        assert result.failed == 1
        assert not result.all_passed

    def test_run_with_exception(self, sample_context: CheckContext, tmp_path: Path):
        """Test handling of exceptions in checks"""
        checks_file = tmp_path / "checks.py"
        checks_file.write_text(
            """
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, CheckFailed, init, check
)

@check
def raises_exception():
    raise ValueError("Something went wrong")
"""
        )

        config = CustomChecksConfig(enabled=True)
        runner = CheckRunner()
        result = runner.run(checks_file, tmp_path, sample_context, config)

        assert result.errors == 1
        assert result.results[0].status == CheckStatus.ERROR
        assert "Something went wrong" in result.results[0].message
        assert "ValueError" in result.results[0].details.get("traceback", "")

    def test_run_with_skip(self, sample_context: CheckContext, tmp_path: Path):
        """Test skipped checks"""
        checks_file = tmp_path / "checks.py"
        checks_file.write_text(
            """
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, CheckFailed, CheckSkipped, init, check
)

@check
def skipped_check():
    return CheckSkipped("Not applicable")

@check
def passing_check():
    return CheckPassed("ok")
"""
        )

        config = CustomChecksConfig(enabled=True)
        runner = CheckRunner()
        result = runner.run(checks_file, tmp_path, sample_context, config)

        assert result.skipped == 1
        assert result.passed == 1
        assert result.aggregate_score == 1.0

    def test_run_missing_file(self, sample_context: CheckContext, tmp_path: Path):
        """Test handling of missing checks.py"""
        checks_file = tmp_path / "nonexistent.py"
        config = CustomChecksConfig(enabled=True)
        runner = CheckRunner()
        result = runner.run(checks_file, tmp_path, sample_context, config)

        assert result.error is not None
        assert "not found" in result.error.lower()

    def test_run_no_checks(self, sample_context: CheckContext, tmp_path: Path):
        """Test handling of checks.py with no @check functions"""
        checks_file = tmp_path / "checks.py"
        checks_file.write_text(
            """
# Empty checks file with no decorators
def not_a_check():
    pass
"""
        )

        config = CustomChecksConfig(enabled=True)
        runner = CheckRunner()
        result = runner.run(checks_file, tmp_path, sample_context, config)

        assert result.error is not None
        assert "no @check" in result.error.lower()

    def test_relative_imports(self, sample_context: CheckContext, tmp_path: Path):
        """Test relative imports from project level"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        helpers_file = project_dir / "check_helpers.py"
        helpers_file.write_text(
            """
def get_magic_number():
    return 42
"""
        )

        task_dir = project_dir / "tasks" / "task_001"
        task_dir.mkdir(parents=True)

        checks_file = task_dir / "checks.py"
        checks_file.write_text(
            """
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, CheckFailed, init, check
)
from check_helpers import get_magic_number

@check
def test_import():
    if get_magic_number() == 42:
        return CheckPassed("Import worked!")
    return CheckFailed("Import broken")
"""
        )

        config = CustomChecksConfig(
            enabled=True,
            relative_imports=["../.."],
        )
        runner = CheckRunner()
        result = runner.run(checks_file, task_dir, sample_context, config)

        assert result.error is None
        assert result.passed == 1


class TestResultToScore:
    """Tests for result_to_score conversion"""

    def test_result_to_score(self):
        runner = CheckRunner()
        config = CustomChecksConfig(enabled=True, fail_on_error=True)

        result = CheckResultSet(
            results=[
                CheckResult(check_name="c1", status=CheckStatus.PASSED, score=1.0, message="ok"),
                CheckResult(check_name="c2", status=CheckStatus.FAILED, score=0.0, message="fail"),
            ]
        )
        score, reason = runner.result_to_score(result, config)
        assert score == 0.5
        assert "✓ c1" in reason
        assert "✗ c2" in reason

    def test_result_to_score_with_error(self):
        runner = CheckRunner()

        config = CustomChecksConfig(enabled=True, fail_on_error=True)
        result = CheckResultSet(error="Module failed to load")
        score, reason = runner.result_to_score(result, config)
        assert score == 0.0
        assert "error" in reason.lower()

        config = CustomChecksConfig(enabled=True, fail_on_error=False)
        score, reason = runner.result_to_score(result, config)
        assert score == 0.5
        assert "non-fatal" in reason.lower()
