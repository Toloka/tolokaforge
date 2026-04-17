"""Agent gRPC service - runs LLM to generate next actions

This service receives conversation history and generates the next action
(tool call or final response) using the configured LLM.
"""

import json
import logging
import time
from concurrent import futures

import grpc

from tolokaforge.agent import agent_pb2, agent_pb2_grpc
from tolokaforge.core.model_client import LLMClient
from tolokaforge.core.models import Message as CoreMessage
from tolokaforge.core.models import MessageRole
from tolokaforge.core.models import ModelConfig as CoreModelConfig
from tolokaforge.core.models import ToolCall as CoreToolCall

logger = logging.getLogger(__name__)


class AgentServiceImpl(agent_pb2_grpc.AgentServiceServicer):
    """Agent service implementation"""

    def __init__(self):
        self.llm_clients = {}  # trial_id -> LLMClient
        logger.info("Agent service initialized")

    def GetNextAction(self, request: agent_pb2.AgentRequest, context) -> agent_pb2.AgentResponse:
        """Get next action from agent given conversation history"""
        try:
            start_time = time.time()
            logger.info(
                f"GetNextAction for trial {request.trial_id}, conversation length: {len(request.conversation_history)}"
            )

            # Convert protobuf model config to core model config
            model_config = CoreModelConfig(
                provider=request.model_config.provider,
                name=request.model_config.name,
                temperature=request.model_config.temperature,
                seed=request.model_config.seed,
                max_tokens=request.model_config.max_tokens,
            )

            # Get or create LLM client for this trial
            if request.trial_id not in self.llm_clients:
                self.llm_clients[request.trial_id] = LLMClient(model_config)

            llm_client = self.llm_clients[request.trial_id]

            # Convert protobuf messages to core messages
            messages = []

            # Add system prompt if provided
            if request.system_prompt:
                messages.append(CoreMessage(role=MessageRole.SYSTEM, content=request.system_prompt))

            # Add conversation history
            for msg in request.conversation_history:
                # Convert protobuf tool calls to core tool calls
                tool_calls = []
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        tool_calls.append(
                            CoreToolCall(
                                id=tc.id, name=tc.name, arguments=json.loads(tc.arguments_json)
                            )
                        )

                messages.append(
                    CoreMessage(
                        role=MessageRole(msg.role),
                        content=msg.content,
                        tool_calls=tool_calls if tool_calls else None,
                    )
                )

            # Convert protobuf tool schemas to OpenAI format
            tools = []
            for tool_schema in request.available_tools:
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool_schema.name,
                            "description": tool_schema.description,
                            "parameters": json.loads(tool_schema.parameters_json),
                        },
                    }
                )

            # Call LLM
            response_msg, response_metadata = llm_client.generate(
                messages=messages,
                tools=tools if tools else None,
                max_tokens=request.max_tokens or 1000,
            )

            # Build response
            response = agent_pb2.AgentResponse()

            # Add metrics
            response.metrics.prompt_tokens = response_metadata.get("prompt_tokens", 0)
            response.metrics.completion_tokens = response_metadata.get("completion_tokens", 0)
            response.metrics.total_tokens = response_metadata.get("total_tokens", 0)
            response.metrics.cost_usd = response_metadata.get("cost_usd", 0.0)
            response.metrics.latency_seconds = time.time() - start_time

            # Check if response has tool calls
            if response_msg.tool_calls and len(response_msg.tool_calls) > 0:
                # Return tool call
                tool_call = response_msg.tool_calls[0]  # Take first tool call
                response.tool_call.id = tool_call.id
                response.tool_call.name = tool_call.name
                response.tool_call.arguments_json = json.dumps(tool_call.arguments)
                logger.info(f"Agent returning tool call: {tool_call.name}")
            else:
                # Return final response
                response.final_response.content = response_msg.content or ""
                logger.info(f"Agent returning final response: {response_msg.content[:100]}...")

            return response

        except Exception as e:
            logger.error(f"Error in GetNextAction: {e}", exc_info=True)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"Error generating next action: {str(e)}")
            return agent_pb2.AgentResponse()

    def HealthCheck(
        self, request: agent_pb2.HealthCheckRequest, context
    ) -> agent_pb2.HealthCheckResponse:
        """Health check endpoint"""
        return agent_pb2.HealthCheckResponse(status="healthy", version="1.0.0")

    def cleanup_trial(self, trial_id: str):
        """Clean up resources for a completed trial"""
        if trial_id in self.llm_clients:
            del self.llm_clients[trial_id]
            logger.info(f"Cleaned up LLM client for trial {trial_id}")


def serve(bind_address: str = "unix:///tmp/agent.sock", max_workers: int = 10):
    """Start the agent gRPC server

    Args:
        bind_address: Address to bind to (unix socket or tcp)
        max_workers: Maximum number of worker threads
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    service = AgentServiceImpl()
    agent_pb2_grpc.add_AgentServiceServicer_to_server(service, server)

    # Bind to address
    server.add_insecure_port(bind_address)

    logger.info(f"Starting agent service on {bind_address}")
    server.start()

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down agent service")
        server.stop(grace=5)


def main():
    """Main entry point for agent container"""
    import sys

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )

    # Get bind address from environment or use unix socket default
    import os

    bind_address = os.environ.get("AGENT_BIND_ADDRESS", "unix:///tmp/agent.sock")

    logger.info(f"Agent container starting with bind address: {bind_address}")
    serve(bind_address=bind_address)


if __name__ == "__main__":
    main()
