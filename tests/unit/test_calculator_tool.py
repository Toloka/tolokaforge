"""Unit tests for the CalculatorTool safe-arithmetic evaluator."""

import pytest

from tolokaforge.tools.builtin.calculator import CalculatorTool
from tolokaforge.tools.registry import ToolCategory

pytestmark = pytest.mark.unit


@pytest.fixture
def calc() -> CalculatorTool:
    """Shared CalculatorTool instance."""
    return CalculatorTool()


# ---------------------------------------------------------------------------
# Basic arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculatorArithmetic:
    """Verify correct results for basic operations."""

    def test_addition(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="2 + 3")
        assert result.success is True
        assert float(result.output) == pytest.approx(5.0)

    def test_subtraction(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="10 - 4")
        assert result.success is True
        assert float(result.output) == pytest.approx(6.0)

    def test_multiplication(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="7 * 6")
        assert result.success is True
        assert float(result.output) == pytest.approx(42.0)

    def test_division(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="15 / 4")
        assert result.success is True
        assert float(result.output) == pytest.approx(3.75)

    def test_power(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="2 ** 10")
        assert result.success is True
        assert float(result.output) == pytest.approx(1024.0)

    def test_unary_negation(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="-5")
        assert result.success is True
        assert float(result.output) == pytest.approx(-5.0)

    def test_compound_expression(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="(10 + 5) * 2 / 3")
        assert result.success is True
        assert float(result.output) == pytest.approx(10.0)

    def test_nested_parentheses(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="((2 + 3) * (4 - 1))")
        assert result.success is True
        assert float(result.output) == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Error handling (works regardless of Python version)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculatorErrors:
    """Verify graceful error handling."""

    def test_division_by_zero(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="1 / 0")
        assert result.success is False
        assert result.error is not None

    def test_invalid_syntax(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="2 +* 3")
        assert result.success is False
        assert result.error is not None

    def test_unsupported_operation_name_access(self, calc: CalculatorTool) -> None:
        """Variable names are not numbers — should fail."""
        result = calc.execute(expression="x + 1")
        assert result.success is False

    def test_empty_expression(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="")
        assert result.success is False

    def test_string_literal_rejected(self, calc: CalculatorTool) -> None:
        """String literals in expressions should be rejected."""
        result = calc.execute(expression="'hello'")
        assert result.success is False


# ---------------------------------------------------------------------------
# Schema / interface
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculatorSchema:
    """Validate tool metadata and schema contract."""

    def test_tool_name(self, calc: CalculatorTool) -> None:
        assert calc.name == "calculator"

    def test_tool_category(self, calc: CalculatorTool) -> None:
        assert calc.policy.category == ToolCategory.COMPUTE

    def test_schema_has_expression_parameter(self, calc: CalculatorTool) -> None:
        schema = calc.get_schema()
        params = schema["function"]["parameters"]
        assert "expression" in params["properties"]
        assert "expression" in params["required"]

    def test_schema_type_is_function(self, calc: CalculatorTool) -> None:
        schema = calc.get_schema()
        assert schema["type"] == "function"

    def test_result_metadata_contains_expression(self, calc: CalculatorTool) -> None:
        """Successful results should include expression and numeric result in metadata."""
        result = calc.execute(expression="3 + 4")
        assert result.metadata["expression"] == "3 + 4"
        assert result.metadata["result"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculatorEdgeCases:
    """Boundary and edge-case inputs."""

    def test_very_large_numbers(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="10 ** 20 + 1")
        assert result.success is True
        assert float(result.output) == pytest.approx(1e20 + 1)

    def test_negative_result(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="3 - 10")
        assert result.success is True
        assert float(result.output) == pytest.approx(-7.0)

    def test_floating_point_precision(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="0.1 + 0.2")
        assert result.success is True
        assert float(result.output) == pytest.approx(0.3)

    def test_zero_result(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="5 - 5")
        assert result.success is True
        assert float(result.output) == pytest.approx(0.0)

    def test_negative_times_negative(self, calc: CalculatorTool) -> None:
        result = calc.execute(expression="(-3) * (-4)")
        assert result.success is True
        assert float(result.output) == pytest.approx(12.0)
