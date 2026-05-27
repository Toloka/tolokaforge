"""Executor gRPC service - executes tools with access to environment

This service runs in a container with access to the environment network.
It executes tools against environment services (JSON DB, RAG, mock web, etc).
"""

import inspect
import json
import logging
import time
from concurrent import futures
from pathlib import Path

import grpc

from tolokaforge.core.env_state import EnvironmentState, InitialStateConfig
from tolokaforge.executor import executor_pb2, executor_pb2_grpc
from tolokaforge.secrets import install_global_redactor
from tolokaforge.tools.builtin import registry as builtin_registry
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


class ExecutorServiceImpl(executor_pb2_grpc.ExecutorServiceServicer):
    """Executor service implementation"""

    def __init__(self):
        self.trial_registries: dict[str, ToolRegistry] = {}
        self.trial_executors: dict[str, ToolExecutor] = {}
        self.trial_env_states: dict[str, EnvironmentState] = {}
        logger.info("Executor service initialized")

    def RegisterTools(
        self, request: executor_pb2.RegisterToolsRequest, context
    ) -> executor_pb2.RegisterToolsResponse:
        """Register tools for a trial"""
        try:
            trial_id = request.trial_id
            logger.info(f"Registering tools for trial {trial_id}")

            # Create environment state for this trial
            task_dir = Path(request.env_config.agent_visible_dir).parent
            initial_state = InitialStateConfig()
            env_state = EnvironmentState(task_dir, initial_state)

            # Override URLs from request
            if request.env_config.json_db_url:
                env_state.json_db_url = request.env_config.json_db_url
            if request.env_config.rag_service_url:
                env_state.rag_service_url = request.env_config.rag_service_url
            if request.env_config.mock_web_url:
                env_state.mock_web_url = request.env_config.mock_web_url

            self.trial_env_states[trial_id] = env_state

            # Create tool registry
            registry = ToolRegistry()

            # Per-tool runtime kwargs derived from env_state. Imports come
            # from the unified builtin registry so adapter, runner, and
            # executor agree on what's a builtin and where it lives.
            #
            # TODO(#123): align with runner WORK_DIR — runner bakes
            #   /work in BuiltinFileToolWrapper; executor uses
            #   env_state.agent_visible_dir. Same tool, different
            #   filesystem context.
            def _runtime_kwargs(name: str) -> dict:
                if name == "bash":
                    return {"workdir": env_state.agent_visible_dir}
                if name in {"read_file", "write_file", "list_dir"}:
                    return {"base_path": env_state.agent_visible_dir}
                if name in {"db_query", "db_update"}:
                    return {"db_url": env_state.json_db_url}
                if name == "search_kb":
                    return {"rag_url": env_state.rag_service_url}
                return {}

            # Drift-detection: every name the unified registry knows
            # about must be served. Equality (not subset) — anything
            # less means an executor-side gap will go unnoticed.
            executor_supported = builtin_registry.list_builtins()

            num_registered = 0
            for tool_def in request.tools:
                if tool_def.name not in executor_supported:
                    logger.warning(f"Unknown tool: {tool_def.name}")
                    continue

                # Decode per-task __init__ kwargs from the JSON envelope.
                # Same shape as runner-side ToolSchema.tool_config (PR #117).
                try:
                    tool_config = json.loads(tool_def.config_json) if tool_def.config_json else {}
                except json.JSONDecodeError as exc:
                    logger.error(
                        f"Invalid config_json for tool {tool_def.name}: {exc}",
                    )
                    return executor_pb2.RegisterToolsResponse(
                        success=False,
                        error=f"Invalid config_json for {tool_def.name}: {exc}",
                        num_tools_registered=num_registered,
                    )

                runtime = _runtime_kwargs(tool_def.name)
                collisions = set(runtime) & set(tool_config)
                if collisions:
                    msg = (
                        f"Tool {tool_def.name} config_json overrides runtime kwargs "
                        f"{sorted(collisions)}"
                    )
                    logger.error(msg)
                    return executor_pb2.RegisterToolsResponse(
                        success=False, error=msg, num_tools_registered=num_registered
                    )
                merged_kwargs = {**runtime, **tool_config}

                try:
                    cls = builtin_registry.get_class(tool_def.name)
                except ImportError as e:
                    logger.error(f"Optional dependency missing for tool {tool_def.name}: {e}")
                    return executor_pb2.RegisterToolsResponse(
                        success=False, error=str(e), num_tools_registered=num_registered
                    )

                # Validate config_json keys against the tool's __init__,
                # mirroring BuiltinGenericToolWrapper.__init__ on the
                # runner side. Unknown keys raise rather than getting
                # silently dropped (filter would mask YAML typos).
                valid_kwargs = {
                    p for p in inspect.signature(cls.__init__).parameters if p != "self"
                }
                unknown = set(merged_kwargs) - valid_kwargs
                if unknown:
                    msg = (
                        f"Unknown tool_config keys for '{tool_def.name}': "
                        f"{sorted(unknown)}; accepted: {sorted(valid_kwargs)}"
                    )
                    logger.error(msg)
                    return executor_pb2.RegisterToolsResponse(
                        success=False, error=msg, num_tools_registered=num_registered
                    )

                try:
                    registry.register(cls(**merged_kwargs))
                    num_registered += 1
                    logger.debug(f"Registered tool: {tool_def.name}")
                except ImportError as e:
                    logger.error(f"Optional dependency missing for tool {tool_def.name}: {e}")
                    return executor_pb2.RegisterToolsResponse(
                        success=False, error=str(e), num_tools_registered=num_registered
                    )

            self.trial_registries[trial_id] = registry

            # Create tool executor
            executor = ToolExecutor(registry)
            self.trial_executors[trial_id] = executor

            logger.info(f"Registered {num_registered} tools for trial {trial_id}")

            return executor_pb2.RegisterToolsResponse(
                success=True, num_tools_registered=num_registered
            )

        except Exception as e:
            logger.error(f"Error registering tools: {e}", exc_info=True)
            return executor_pb2.RegisterToolsResponse(
                success=False, error=str(e), num_tools_registered=0
            )

    def ExecuteTool(
        self, request: executor_pb2.ExecuteToolRequest, context
    ) -> executor_pb2.ExecuteToolResponse:
        """Execute a tool"""
        try:
            trial_id = request.trial_id
            tool_name = request.tool_name

            logger.info(f"Executing tool {tool_name} for trial {trial_id}")

            # Check if trial is registered
            if trial_id not in self.trial_executors:
                logger.error(f"Trial {trial_id} not registered")
                return executor_pb2.ExecuteToolResponse(
                    success=False,
                    output="",
                    error=f"Trial {trial_id} not registered. Call RegisterTools first.",
                )

            executor = self.trial_executors[trial_id]

            # Parse arguments
            try:
                arguments = json.loads(request.arguments_json)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid arguments JSON: {e}")
                return executor_pb2.ExecuteToolResponse(
                    success=False, output="", error=f"Invalid arguments JSON: {str(e)}"
                )

            # Execute tool
            start_time = time.time()
            result: ToolResult = executor.execute(tool_name=tool_name, arguments=arguments)
            latency = time.time() - start_time

            logger.info(
                f"Tool {tool_name} executed: success={result.success}, latency={latency:.2f}s"
            )

            # Build response
            response = executor_pb2.ExecuteToolResponse(
                success=result.success, output=result.output, error=result.error or ""
            )
            response.metrics.latency_seconds = latency

            return response

        except Exception as e:
            logger.error(f"Error executing tool: {e}", exc_info=True)
            return executor_pb2.ExecuteToolResponse(
                success=False, output="", error=f"Error executing tool: {str(e)}"
            )

    def HealthCheck(
        self, request: executor_pb2.HealthCheckRequest, context
    ) -> executor_pb2.HealthCheckResponse:
        """Health check endpoint"""
        return executor_pb2.HealthCheckResponse(
            status="healthy", version="1.0.0", num_active_trials=len(self.trial_executors)
        )

    def cleanup_trial(self, trial_id: str):
        """Clean up resources for a completed trial"""
        if trial_id in self.trial_executors:
            del self.trial_executors[trial_id]
        if trial_id in self.trial_registries:
            del self.trial_registries[trial_id]
        if trial_id in self.trial_env_states:
            del self.trial_env_states[trial_id]
        logger.info(f"Cleaned up resources for trial {trial_id}")


def serve(bind_address: str = "[::]:50051", max_workers: int = 10):
    """Start the executor gRPC server

    Args:
        bind_address: Address to bind to (TCP)
        max_workers: Maximum number of worker threads
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    service = ExecutorServiceImpl()
    executor_pb2_grpc.add_ExecutorServiceServicer_to_server(service, server)

    # Bind to address
    server.add_insecure_port(bind_address)

    logger.info(f"Starting executor service on {bind_address}")
    server.start()

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down executor service")
        server.stop(grace=5)


def main():
    """Main entry point for executor container"""
    import os
    import sys

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    install_global_redactor()

    # Get bind address from environment or use default
    bind_address = os.environ.get("EXECUTOR_BIND_ADDRESS", "[::]:50051")

    logger.info(f"Executor container starting with bind address: {bind_address}")
    serve(bind_address=bind_address)


if __name__ == "__main__":
    main()
