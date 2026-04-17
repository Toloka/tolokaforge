"""
Runner gRPC Protocol Package

This package contains the gRPC protocol definitions and service implementation
for Host ↔ Runner communication in the Tolokaforge distributed architecture.

Components:
- Protocol definitions (runner_pb2, runner_pb2_grpc)
- DB Service client (db_client)
- Runner service implementation (service)
- Server entry point (__main__)

See docs/GRPC_PROTOCOL.md for the full specification.
"""

from tolokaforge.runner.runner_pb2 import (
    CustomCheckResult,
    ExecuteToolRequest,
    ExecuteToolResponse,
    # Enums
    ExecutionStatus,
    GetStateRequest,
    GetStateResponse,
    Grade,
    GradeComponents,
    GradeTrialRequest,
    GradeTrialResponse,
    HealthCheckRequest,
    HealthCheckResponse,
    # Request/Response messages
    RegisterTrialRequest,
    RegisterTrialResponse,
    ResetTrialRequest,
    ResetTrialResponse,
    ToolMetrics,
    # Supporting messages
    ToolSchema,
)
from tolokaforge.runner.runner_pb2_grpc import (
    RunnerServiceServicer,
    RunnerServiceStub,
    add_RunnerServiceServicer_to_server,
)

__all__ = [
    # Request/Response messages
    "RegisterTrialRequest",
    "RegisterTrialResponse",
    "ExecuteToolRequest",
    "ExecuteToolResponse",
    "GradeTrialRequest",
    "GradeTrialResponse",
    "GetStateRequest",
    "GetStateResponse",
    "ResetTrialRequest",
    "ResetTrialResponse",
    "HealthCheckRequest",
    "HealthCheckResponse",
    # Supporting messages
    "ToolSchema",
    "ToolMetrics",
    "Grade",
    "GradeComponents",
    "CustomCheckResult",
    # Enums
    "ExecutionStatus",
    # Service classes
    "RunnerServiceStub",
    "RunnerServiceServicer",
    "add_RunnerServiceServicer_to_server",
]
