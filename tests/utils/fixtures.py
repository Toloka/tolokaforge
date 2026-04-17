"""Shared test fixtures"""

import tempfile
from pathlib import Path

import pytest

from tolokaforge.core.env_state import EnvironmentState
from tolokaforge.core.models import InitialStateConfig, ModelConfig

# ---------------------------------------------------------------------------
# Environment fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_env_state():
    """Create mock EnvironmentState for testing"""
    with tempfile.TemporaryDirectory() as tmpdir:
        task_dir = Path(tmpdir)
        initial_state = InitialStateConfig()
        env_state = EnvironmentState(task_dir, initial_state)
        yield env_state


@pytest.fixture
def test_model_config():
    """Model config for testing - uses cheap model"""
    return ModelConfig(
        provider="openrouter",
        name="google/gemini-2.5-flash-lite",
        temperature=0.0,
        seed=42,
        max_tokens=500,
    )


@pytest.fixture
def temp_workdir():
    """Create temporary working directory"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def test_data_dir() -> Path:
    """Get tests/data directory for test tasks and fixtures

    Returns:
        Path to tests/data directory
    """
    return Path(__file__).parent.parent / "data"


@pytest.fixture
def test_task_path(test_data_dir):
    """Get path to a specific test task

    Usage:
        def test_something(test_task_path):
            task_dir = test_task_path("calc_basic")
            task_yaml = task_dir / "task.yaml"

    Args:
        task_name: Name of the test task directory

    Returns:
        Function that returns Path to test task directory
    """

    def _get(task_name: str) -> Path:
        task_path = test_data_dir / "tasks" / task_name
        if not task_path.exists():
            pytest.fail(f"Test task not found: {task_path}")
        return task_path

    return _get


@pytest.fixture
def test_fixture_path(test_data_dir):
    """Get path to a test fixture file

    Usage:
        def test_something(test_fixture_path):
            html_file = test_fixture_path("sample.html")

    Args:
        fixture_name: Name of the fixture file

    Returns:
        Function that returns Path to fixture file
    """

    def _get(fixture_name: str) -> Path:
        fixture_path = test_data_dir / "fixtures" / fixture_name
        if not fixture_path.exists():
            pytest.fail(f"Test fixture not found: {fixture_path}")
        return fixture_path

    return _get


@pytest.fixture
def temp_output_dir():
    """Create temporary output directory with auto-cleanup

    This should be used instead of writing to output/ directory.
    The directory is automatically cleaned up after the test.

    Usage:
        def test_something(temp_output_dir):
            config = {
                "output_dir": str(temp_output_dir)
            }
            # Use temp_output_dir for test outputs
            # Automatically cleaned up after test

    Yields:
        Path to temporary output directory
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "test_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        yield output_dir


# ---------------------------------------------------------------------------
# gRPC / Runner integration fixtures (D5 + D6)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_grpc_context():
    """Create a mock gRPC context for runner service tests."""
    from unittest.mock import MagicMock

    context = MagicMock()
    context.set_code = MagicMock()
    context.set_details = MagicMock()
    return context


@pytest.fixture
def db_test_client():
    """Create a TestClient for the DB service."""
    from fastapi.testclient import TestClient

    from tolokaforge.env.json_db_service.app import app as db_app

    return TestClient(db_app)


@pytest.fixture
def db_client(db_test_client):
    """Create a DBServiceClient that uses the TestClient."""
    from tests.utils.mock_clients import MockAsyncClient
    from tolokaforge.runner.db_client import DBServiceClient

    client = DBServiceClient("http://testserver")
    client.set_test_client(MockAsyncClient(db_test_client, "http://testserver"))
    return client


@pytest.fixture
def runner_service(db_client):
    """Create a RunnerServiceImpl with the test DB client."""
    from tolokaforge.runner.service import RunnerServiceImpl

    return RunnerServiceImpl(db_client)
