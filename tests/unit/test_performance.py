"""Load testing suite for Tolokaforge

Tests parallel execution, resource management, and performance benchmarks.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pytest

pytestmark = pytest.mark.unit

from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry, ToolResult


class TestParallelExecution:
    """Test parallel trial execution with multiple workers"""

    def test_concurrent_tool_execution(self):
        """Test that multiple tools can execute concurrently"""
        import threading

        class SlowTool(Tool):
            def __init__(self):
                super().__init__("slow_tool", "Slow tool for testing")
                self.call_count = 0
                self.lock = threading.Lock()

            def get_schema(self) -> dict[str, Any]:
                return {
                    "type": "function",
                    "function": {
                        "name": "slow_tool",
                        "description": "Slow",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                }

            def execute(self, **kwargs) -> ToolResult:
                with self.lock:
                    self.call_count += 1
                time.sleep(0.1)  # Simulate slow operation
                return ToolResult(success=True, output="OK")

        tool = SlowTool()
        registry = ToolRegistry()
        registry.register(tool)

        # Execute 10 calls concurrently
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for _i in range(10):
                executor_instance = ToolExecutor(registry)
                future = executor.submit(executor_instance.execute, "slow_tool", {})
                futures.append(future)

            results = [f.result() for f in as_completed(futures)]

        elapsed = time.time() - start_time

        # All should succeed
        assert all(r.success for r in results)

        # Should take ~0.1s (concurrent), not 1.0s (sequential)
        # Allow some overhead, but should be much less than 1.0s
        assert elapsed < 0.5, f"Took {elapsed:.2f}s (not concurrent?)"

        # All calls should have been counted
        assert tool.call_count == 10
