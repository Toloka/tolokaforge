"""Docker runtime validation tests

Tests Docker infrastructure: service health, communication, and integration.
Tests now use testcontainers for automatic container lifecycle management.

Run tests with:
    pytest tests/integration/test_docker_services.py -v
"""

import subprocess

import pytest

import docker
from tests.utils.validators import validate_grading_result

pytestmark = pytest.mark.integration


@pytest.mark.integration
@pytest.mark.requires_docker
class TestDockerVolumes:
    """Test Docker volume configuration using testcontainers"""

    def test_env_files_volume_exists(self, env_files_volume):
        """Verify env_files volume exists and is usable"""
        # Volume is created by the fixture
        assert env_files_volume is not None

        # Verify volume exists in Docker
        client = docker.from_env()
        volumes = client.volumes.list()
        volume_names = [v.name for v in volumes]
        assert env_files_volume in volume_names, f"env_files volume '{env_files_volume}' not found"

        # Verify volume is accessible
        volume = client.volumes.get(env_files_volume)
        assert volume.attrs is not None

    def test_rag_data_volume_exists(self, rag_data_volume):
        """Verify rag_data volume exists and is usable"""
        # Volume is created by the fixture
        assert rag_data_volume is not None

        # Verify volume exists in Docker
        client = docker.from_env()
        volumes = client.volumes.list()
        volume_names = [v.name for v in volumes]
        assert rag_data_volume in volume_names, f"rag_data volume '{rag_data_volume}' not found"

        # Verify volume is accessible
        volume = client.volumes.get(rag_data_volume)
        assert volume.attrs is not None


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.requires_api
class TestDockerE2E:
    """End-to-end trial execution tests using Docker runtime.

    These tests start Docker services (runner, json-db, rag) via
    testcontainer fixtures and run the CLI in a subprocess with
    EXECUTOR_ADDRESS pointing to the testcontainer runner.
    """

    def test_complete_trial_execution(self, test_task_path, temp_output_dir, runner_container):
        """Test complete trial execution with test data and output validation"""
        import os
        import tempfile

        # Get the runner container's exposed address for the subprocess
        runner_host = runner_container.get_container_host_ip()
        runner_port = runner_container.get_exposed_port(50051)
        executor_address = f"{runner_host}:{runner_port}"

        # Use minimal test task instead of production task
        task_path = test_task_path("calc_basic")

        # Create minimal config for quick integration test
        config = f"""
models:
  agent:
    provider: anthropic
    name: claude-3-5-sonnet-20241022
    temperature: 0.0
    max_tokens: 500
  user:
    provider: anthropic
    name: claude-3-5-sonnet-20241022
    temperature: 0.7

evaluation:
  tasks_glob: "{task_path}/task.yaml"
  output_dir: {temp_output_dir}

orchestrator:
  runtime: docker
  workers: 1
  repeats: 1
  max_turns: 10
  timeouts:
    turn_s: 30
    episode_s: 120
  stuck_heuristics:
    max_repeated_tool_calls: 3
    max_idle_turns: 2
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config)
            config_path = f.name

        try:
            # Pass EXECUTOR_ADDRESS so the orchestrator connects to the
            # testcontainer runner instead of the default "executor:50051".
            env = os.environ.copy()
            env["EXECUTOR_ADDRESS"] = executor_address

            result = subprocess.run(
                ["python", "-m", "tolokaforge.cli.main", "run", "--config", config_path],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )

            assert result.returncode == 0, f"Trial failed: {result.stderr}"
            # Check for task loading (new logging format or CLI output)
            assert (
                "Tasks loaded" in result.stdout or "Found 1 tasks" in result.stdout
            ), f"Task not loaded. Output: {result.stdout}"
            assert (
                "Starting new run" in result.stdout or "run_id" in result.stdout
            ), f"Trial did not start. Output: {result.stdout}"
            assert (
                "Run complete" in result.stdout or "✓" in result.stdout
            ), f"Trial did not complete. Output: {result.stdout}"

            # Only validate if trial completed - this is an integration test
            # so we're checking the infrastructure works, not the task quality
            if (temp_output_dir / "trials" / "calc_basic" / "0").exists():
                grading_result = validate_grading_result(
                    output_dir=temp_output_dir,
                    task_id="calc_basic",
                    trial_num=0,
                    min_score=0.0,  # Just check it ran, don't require success
                    max_score=1.0,
                )

                # Verify trajectory has expected structure
                assert "messages" in grading_result["trajectory"], "Trajectory should have messages"

                # Tool data may be in tool_calls or tool_log
                has_tools = (
                    "tool_calls" in grading_result["trajectory"]
                    or "tool_log" in grading_result["trajectory"]
                )
                assert has_tools, "Trajectory should have tool_calls or tool_log"

                # Verify the test infrastructure worked
                assert grading_result["metrics"], "Metrics should be present"
            else:
                # If trial didn't create output, just verify command succeeded
                pytest.fail("Trial ran but didn't create expected output structure")

        finally:
            if os.path.exists(config_path):
                os.unlink(config_path)
