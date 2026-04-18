"""Calculator tool for safe arithmetic"""

import ast
import operator
from typing import Any

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult


class CalculatorTool(Tool):
    """Safe arithmetic calculator"""

    def __init__(self):
        policy = ToolPolicy(
            timeout_s=5.0,
            category=ToolCategory.COMPUTE,
            visibility=["agent"],
        )
        super().__init__(
            name="calculator",
            description="Perform safe arithmetic calculations",
            policy=policy,
        )
        # Allowed operations
        self.ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
        }

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Arithmetic expression to evaluate (e.g., '2 + 2', '(10 * 5) / 2')",
                        }
                    },
                    "required": ["expression"],
                    "additionalProperties": False,
                },
            },
        }

    def _eval_expr(self, node: ast.AST) -> float:
        """Safely evaluate expression AST"""
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        elif isinstance(node, ast.BinOp):  # binary operation
            op = self.ops.get(type(node.op))
            if not op:
                raise ValueError(f"Unsupported operation: {type(node.op).__name__}")
            return op(self._eval_expr(node.left), self._eval_expr(node.right))
        elif isinstance(node, ast.UnaryOp):  # unary operation
            op = self.ops.get(type(node.op))
            if not op:
                raise ValueError(f"Unsupported operation: {type(node.op).__name__}")
            return op(self._eval_expr(node.operand))
        else:
            raise ValueError(f"Unsupported expression type: {type(node).__name__}")

    def execute(self, expression: str) -> ToolResult:
        """Evaluate arithmetic expression"""
        try:
            # Parse expression
            tree = ast.parse(expression, mode="eval")

            # Evaluate safely
            result = self._eval_expr(tree.body)

            return ToolResult(
                success=True,
                output=str(result),
                metadata={"expression": expression, "result": result},
            )
        except SyntaxError:
            return ToolResult(
                success=False,
                output="",
                error="Invalid expression syntax",
            )
        except ValueError as e:
            return ToolResult(
                success=False,
                output="",
                error=str(e),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Calculation failed: {str(e)}",
            )
