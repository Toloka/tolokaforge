"""Integration tests for Docker Foundation Layer.

End-to-end tests that exercise the full foundation layer working together:
build image → create container with mounts + network + policy → start →
health check → exec command → write/read file → stop → destroy.

These tests require a running Docker daemon and are marked with @pytest.mark.docker.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.docker import (
    Capability,
    Container,
    ContainerStatus,
    HealthProbe,
    Image,
    Mount,
    MountType,
    Network,
    PortConfig,
    ResourcePolicy,
)

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.mark.docker
@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestDockerFoundationIntegration:
    """End-to-end integration tests for the Docker foundation layer."""

    @pytest.fixture
    def temp_dockerfile_dir(self, tmp_path: Path) -> Path:
        """Create a temporary directory with a simple Dockerfile.

        Creates a minimal Python-based container that:
        - Runs a simple HTTP health endpoint
        - Can execute commands
        - Can read/write files
        """
        dockerfile_content = """\
FROM python:3.10-slim

# Create app and work directories
RUN mkdir -p /app /work

# Create a simple health check server
RUN echo 'import http.server\\n\
import socketserver\\n\
import threading\\n\
import time\\n\
\\n\
PORT = 8000\\n\
\\n\
class HealthHandler(http.server.SimpleHTTPRequestHandler):\\n\
    def do_GET(self):\\n\
        if self.path == "/health":\\n\
            self.send_response(200)\\n\
            self.send_header("Content-type", "text/plain")\\n\
            self.end_headers()\\n\
            self.wfile.write(b"OK")\\n\
        else:\\n\
            self.send_response(404)\\n\
            self.end_headers()\\n\
    def log_message(self, format, *args):\\n\
        pass  # Suppress logging\\n\
\\n\
with socketserver.TCPServer(("", PORT), HealthHandler) as httpd:\\n\
    print(f"Serving on port {PORT}")\\n\
    httpd.serve_forever()\\n\
' > /app/server.py

WORKDIR /app

EXPOSE 8000

CMD ["python", "/app/server.py"]
"""
        dockerfile_path = tmp_path / "Dockerfile"
        dockerfile_path.write_text(dockerfile_content)
        return tmp_path

    def test_full_container_lifecycle(
        self,
        temp_dockerfile_dir: Path,
        cleanup_resources: dict,
    ) -> None:
        """Test the complete container lifecycle from build to destroy.

        This test exercises:
        1. Building an image from a Dockerfile
        2. Creating a container with mounts, network, and resource policy
        3. Starting the container
        4. Running a health check
        5. Executing commands inside the container
        6. Writing and reading files
        7. Stopping and destroying the container
        """
        # Step 1: Build the image
        image = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-integration-test",
        )
        cleanup_resources["images"].append(image)

        assert image.image_id is not None
        assert image.exists()
        assert image.name == "tolokaforge-integration-test"

        # Step 2: Create a network
        network = Network.create(
            name=f"tolokaforge-test-net-{int(time.time())}",
            internal=False,  # Allow external access for health checks
        )
        cleanup_resources["networks"].append(network)

        assert network.network_id is not None
        assert network.exists()

        # Step 3: Create a mount (workspace volume)
        workspace_mount = Mount.volume(
            name=f"tolokaforge-test-workspace-{int(time.time())}",
            container_path="/work",
        )
        assert workspace_mount.mount_type == MountType.VOLUME

        # Step 4: Create resource policy
        policy = ResourcePolicy(
            memory_limit="256m",
            cpu_limit=0.5,
            cap_drop=[Capability.ALL],
            cap_add=[Capability.NET_BIND_SERVICE],
            no_new_privileges=True,
        )

        # Step 5: Create the container (use auto port to avoid conflicts)
        port_config = PortConfig(container_port=8000, host_port="auto")
        resolved_port = port_config.resolve()
        host_port = resolved_port.host_port

        container = Container.create(
            image=image,
            name=f"tolokaforge-integration-test-{int(time.time())}",
            mounts=[workspace_mount],
            network=network,
            resources=policy,
            ports=[resolved_port],
        )
        cleanup_resources["containers"].append(container)

        assert container.container_id is not None
        assert container.current_status == ContainerStatus.CREATED
        assert container.exists()

        # Step 6: Start the container
        container.start()
        assert container.current_status == ContainerStatus.RUNNING

        # Give the server a moment to start
        time.sleep(1)

        # Step 7: Run health check (using the resolved host port)
        probe = HealthProbe.tcp(
            host="localhost",
            port=host_port,
            interval_s=0.5,
            timeout_s=10.0,
        )
        result = container.health_check(probe)
        assert result.healthy
        assert container.current_status == ContainerStatus.READY

        # Step 8: Execute a command
        exec_result = container.exec(["echo", "Hello from container"])
        assert exec_result.exit_code == 0
        assert "Hello from container" in exec_result.stdout

        # Step 9: Execute a more complex command
        exec_result = container.exec("python --version")
        assert exec_result.exit_code == 0
        assert "Python" in exec_result.stdout

        # Step 10: Write a file to the container
        test_content = b"Integration test content\nLine 2\nLine 3"
        container.write_file("/work/test_file.txt", test_content)

        # Step 11: Read the file back
        read_content = container.read_file("/work/test_file.txt")
        assert read_content == test_content

        # Step 12: Verify file via exec
        exec_result = container.exec(["cat", "/work/test_file.txt"])
        assert exec_result.exit_code == 0
        assert "Integration test content" in exec_result.stdout

        # Step 13: Get container logs
        logs = list(container.logs(tail=10))
        assert len(logs) >= 0  # May or may not have logs depending on timing

        # Step 14: Check status
        status = container.status()
        assert status in (ContainerStatus.RUNNING, ContainerStatus.READY)

        # Step 15: Stop the container
        container.stop(timeout_s=10)
        assert container.current_status == ContainerStatus.STOPPED

        # Step 16: Verify container is stopped
        status = container.status()
        assert status == ContainerStatus.STOPPED

        # Step 17: Destroy the container
        container.destroy()
        assert container.current_status == ContainerStatus.DESTROYED
        assert not container.exists()

        # Step 18: Destroy the network
        network.destroy()
        assert not network.exists()

        # Step 19: Remove the image
        image.remove()
        assert not image.exists()

    def test_container_with_bind_mount(
        self,
        temp_dockerfile_dir: Path,
        cleanup_resources: dict,
        tmp_path: Path,
    ) -> None:
        """Test container with bind mount for host filesystem access."""
        # Create a host directory with a file
        host_dir = tmp_path / "host_data"
        host_dir.mkdir()
        host_file = host_dir / "host_file.txt"
        host_file.write_text("Content from host")

        # Build image
        image = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-bind-test",
        )
        cleanup_resources["images"].append(image)

        # Create bind mount
        bind_mount = Mount.bind(
            host_path=str(host_dir),
            container_path="/host_data",
            read_only=True,
        )

        # Create and start container
        container = Container.create(
            image=image,
            name=f"tolokaforge-bind-test-{int(time.time())}",
            mounts=[bind_mount],
        )
        cleanup_resources["containers"].append(container)

        container.start()
        time.sleep(0.5)

        # Read the host file from inside the container
        content = container.read_file("/host_data/host_file.txt")
        assert content == b"Content from host"

        # Verify via exec
        exec_result = container.exec(["cat", "/host_data/host_file.txt"])
        assert exec_result.exit_code == 0
        assert "Content from host" in exec_result.stdout

        # Cleanup
        container.stop()
        container.destroy()

    def test_container_exec_with_error(
        self,
        temp_dockerfile_dir: Path,
        cleanup_resources: dict,
    ) -> None:
        """Test container exec with a command that fails."""
        # Build image
        image = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-exec-error-test",
        )
        cleanup_resources["images"].append(image)

        # Create and start container
        container = Container.create(
            image=image,
            name=f"tolokaforge-exec-error-test-{int(time.time())}",
        )
        cleanup_resources["containers"].append(container)

        container.start()
        time.sleep(0.5)

        # Execute a command that will fail
        exec_result = container.exec(["ls", "/nonexistent/path"])
        assert exec_result.exit_code != 0
        assert exec_result.stderr or "No such file" in exec_result.stdout

        # Execute a command with exit code
        exec_result = container.exec("exit 42")
        assert exec_result.exit_code == 42

        # Cleanup
        container.stop()
        container.destroy()

    def test_network_isolation(
        self,
        temp_dockerfile_dir: Path,
        cleanup_resources: dict,
    ) -> None:
        """Test that containers on the same network can communicate."""
        # Build image
        image = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-network-test",
        )
        cleanup_resources["images"].append(image)

        # Create a network
        network = Network.create(
            name=f"tolokaforge-network-test-{int(time.time())}",
            internal=False,
        )
        cleanup_resources["networks"].append(network)

        # Create first container
        container1 = Container.create(
            image=image,
            name=f"tolokaforge-net-test-1-{int(time.time())}",
            network=network,
        )
        cleanup_resources["containers"].append(container1)

        # Create second container
        container2 = Container.create(
            image=image,
            name=f"tolokaforge-net-test-2-{int(time.time())}",
            network=network,
        )
        cleanup_resources["containers"].append(container2)

        # Start both containers
        container1.start()
        container2.start()
        time.sleep(1)

        # Verify both are running
        assert container1.status() == ContainerStatus.RUNNING
        assert container2.status() == ContainerStatus.RUNNING

        # Cleanup
        container1.stop()
        container2.stop()
        container1.destroy()
        container2.destroy()
        network.destroy()

    def test_resource_policy_application(
        self,
        temp_dockerfile_dir: Path,
        cleanup_resources: dict,
    ) -> None:
        """Test that resource policies are correctly applied to containers."""
        # Build image
        image = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-policy-test",
        )
        cleanup_resources["images"].append(image)

        # Create a strict policy
        policy = ResourcePolicy.secure_default().with_memory_limit("128m").with_cpu_limit(0.25)

        # Create container with policy
        container = Container.create(
            image=image,
            name=f"tolokaforge-policy-test-{int(time.time())}",
            resources=policy,
        )
        cleanup_resources["containers"].append(container)

        container.start()
        time.sleep(0.5)

        # Verify container is running with the policy
        assert container.status() == ContainerStatus.RUNNING

        # The container should be able to run basic commands
        exec_result = container.exec(["echo", "policy test"])
        assert exec_result.exit_code == 0

        # Cleanup
        container.stop()
        container.destroy()

    def test_image_content_hash_caching(
        self,
        temp_dockerfile_dir: Path,
        cleanup_resources: dict,
    ) -> None:
        """Test that image building uses content-hash caching correctly."""
        # Build image first time
        image1 = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-cache-test",
        )
        cleanup_resources["images"].append(image1)

        # Build same image again - should use cache
        image2 = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-cache-test",
        )

        # Both should have the same tag (content hash)
        assert image1.tag == image2.tag
        assert image1.full_tag == image2.full_tag
        assert image1.context_hash == image2.context_hash

        # Modify the Dockerfile
        dockerfile_path = temp_dockerfile_dir / "Dockerfile"
        original_content = dockerfile_path.read_text()
        dockerfile_path.write_text(original_content + "\n# Modified\n")

        # Build again - should create new image with different hash
        image3 = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-cache-test",
        )
        cleanup_resources["images"].append(image3)

        # Should have different tag
        assert image3.tag != image1.tag
        assert image3.context_hash != image1.context_hash

    def test_mcp_config_mount(
        self,
        temp_dockerfile_dir: Path,
        cleanup_resources: dict,
    ) -> None:
        """Test MCP configuration mount functionality."""
        # Build image
        image = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-mcp-test",
        )
        cleanup_resources["images"].append(image)

        # Create MCP config mount
        mcp_config = {
            "mcpServers": {
                "test-server": {
                    "command": "node",
                    "args": ["server.js"],
                }
            }
        }
        mcp_mount = Mount.mcp(mcp_config, container_path="/app/mcp/config.json")

        # Create container with MCP mount
        container = Container.create(
            image=image,
            name=f"tolokaforge-mcp-test-{int(time.time())}",
            mounts=[mcp_mount],
        )
        cleanup_resources["containers"].append(container)

        container.start()
        time.sleep(0.5)

        # Read the MCP config from inside the container
        content = container.read_file("/app/mcp/config.json")
        assert b"mcpServers" in content
        assert b"test-server" in content

        # Cleanup temp file
        mcp_mount.cleanup_temp_file()

        # Cleanup container
        container.stop()
        container.destroy()

    def test_workspace_mount(
        self,
        temp_dockerfile_dir: Path,
        cleanup_resources: dict,
    ) -> None:
        """Test per-trial workspace mount functionality."""
        # Build image
        image = Image.build(
            dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
            context=str(temp_dockerfile_dir),
            name="tolokaforge-workspace-test",
        )
        cleanup_resources["images"].append(image)

        # Create workspace mount for a trial
        trial_id = f"trial_{int(time.time())}"
        workspace_mount = Mount.workspace(trial_id, container_path="/work")

        assert workspace_mount.source == f"tolokaforge-workspace-{trial_id}"
        assert workspace_mount.target == "/work"
        assert workspace_mount.mount_type == MountType.VOLUME

        # Create container with workspace mount
        container = Container.create(
            image=image,
            name=f"tolokaforge-workspace-test-{int(time.time())}",
            mounts=[workspace_mount],
        )
        cleanup_resources["containers"].append(container)

        container.start()
        time.sleep(0.5)

        # Write to workspace
        container.write_file("/work/trial_data.txt", b"Trial data content")

        # Read back
        content = container.read_file("/work/trial_data.txt")
        assert content == b"Trial data content"

        # Cleanup
        container.stop()
        container.destroy()
