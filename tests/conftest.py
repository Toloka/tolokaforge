"""Pytest configuration and shared fixtures for test suite."""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests marked with @pytest.mark.requires_api when no API keys are set."""
    api_keys = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
    has_api_key = any(os.environ.get(k) for k in api_keys)
    if has_api_key:
        return
    skip_marker = pytest.mark.skip(reason="No LLM API key set (requires_api)")
    for item in items:
        if "requires_api" in item.keywords:
            item.add_marker(skip_marker)


# Import shared fixtures so they're available to all tests
from tests.utils.containers import (  # noqa: E402
    json_db_container,
    runner_container,
)
from tests.utils.docker_helpers import (  # noqa: E402
    skip_if_no_docker_runner,
)
from tests.utils.fixtures import (  # noqa: E402
    db_client,
    db_test_client,
    mock_env_state,
    mock_grpc_context,
    runner_service,
    temp_output_dir,
    test_data_dir,
    test_task_path,
)
from tests.utils.networks import (  # noqa: E402
    env_files_volume,
    env_network,
    rag_data_volume,
)

__all__ = [
    # Utility fixtures
    "mock_env_state",
    "test_data_dir",
    "test_task_path",
    "temp_output_dir",
    # gRPC / Runner fixtures
    "mock_grpc_context",
    "db_test_client",
    "db_client",
    "runner_service",
    # Docker helper fixtures
    "skip_if_no_docker_runner",
    # Network and volume fixtures
    "env_network",
    "env_files_volume",
    "rag_data_volume",
    # Container fixtures
    "json_db_container",
    "runner_container",
]
