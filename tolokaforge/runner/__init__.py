"""
Runner Package

Hosts the runtime-independence library surface (:func:`run_trial`) alongside
the gRPC protocol definitions and service implementation used for
Host ↔ Runner communication.

Components:
- ``run_trial`` — single-trial library entry (ADR-0019 § Surface 2)
- Protocol definitions (runner_pb2, runner_pb2_grpc)
- DB Service client (db_client)
- Runner service implementation (service)
- Server entry point (__main__)

See docs/GRPC_PROTOCOL.md for the gRPC specification and
docs/API.md#run_trial for the library entry.
"""

from tolokaforge.runner.runner_pb2 import (
    CleanupTrialRequest,
    CleanupTrialResponse,
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


def __getattr__(name: str):
    # Lazy re-export so importing this package for its gRPC types (the runner
    # service hot path) does not pull in adapters / litellm / conductor.
    if name == "run_trial":
        import importlib

        obj = importlib.import_module("tolokaforge.core.run_trial").run_trial
        globals()[name] = obj
        return obj
    raise AttributeError(f"module 'tolokaforge.runner' has no attribute {name!r}")


__all__ = [
    # Library entry (ADR-0019 § Surface 2)
    "run_trial",
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
    "CleanupTrialRequest",
    "CleanupTrialResponse",
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
