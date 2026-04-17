"""Integration tests for ServiceStack lifecycle.

These tests verify that ServiceStack can create, start, health-check,
stop, and destroy containers using the foundation layer.

Requires Docker to be running.
"""

import textwrap
import uuid

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available

# Skip all tests if Docker is not available
pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
]


@pytest.fixture
def simple_dockerfile(tmp_path):
    """Create a simple HTTP server Dockerfile for testing."""
    context_dir = tmp_path / "context"
    context_dir.mkdir()

    dockerfile = context_dir / "Dockerfile"
    dockerfile.write_text(
        textwrap.dedent(
            """\
        FROM python:3.12-alpine
        RUN echo "healthy" > /health.txt
        CMD ["python", "-m", "http.server", "8080"]
        """
        )
    )

    return context_dir, dockerfile


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestDockerStack:
    """Integration tests for ServiceStack lifecycle."""

    def test_start_stop_single_service(self, simple_dockerfile):
        """Start and stop a single service."""
        from tolokaforge.docker.ports import PortConfig
        from tolokaforge.docker.stack import ServiceDefinition, ServiceStack

        context_dir, dockerfile = simple_dockerfile

        stack = ServiceStack(prefix=f"test-single-{uuid.uuid4().hex[:6]}")
        svc = ServiceDefinition(
            name="web",
            image_name="test-stack-web",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port="auto")],
        )
        stack.add_service(svc)

        try:
            stack.start_all(wait=False)
            statuses = stack.get_status()
            assert "web" in statuses
        finally:
            stack.destroy()

    def test_dependency_ordering(self, simple_dockerfile):
        """Services start in dependency order."""
        from tolokaforge.docker.ports import PortConfig
        from tolokaforge.docker.stack import ServiceDefinition, ServiceStack

        context_dir, dockerfile = simple_dockerfile

        stack = ServiceStack(prefix=f"test-deps-{uuid.uuid4().hex[:6]}")

        svc_a = ServiceDefinition(
            name="base",
            image_name="test-deps-base",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port="auto")],
        )
        svc_b = ServiceDefinition(
            name="dependent",
            image_name="test-deps-dep",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port="auto")],
            depends_on=["base"],
        )

        stack.add_services([svc_a, svc_b])

        try:
            # Verify topological sort
            order = stack._topological_sort(stack.services)
            assert order.index("base") < order.index("dependent")

            stack.start_all(wait=False)
            statuses = stack.get_status()
            assert len(statuses) == 2
        finally:
            stack.destroy()

    def test_profile_filtering(self, simple_dockerfile):
        """Profile filtering includes/excludes services correctly."""
        from tolokaforge.docker.ports import PortConfig
        from tolokaforge.docker.stack import ServiceDefinition, ServiceStack

        context_dir, dockerfile = simple_dockerfile

        stack = ServiceStack(prefix=f"test-profiles-{uuid.uuid4().hex[:6]}")

        core_svc = ServiceDefinition(
            name="core",
            image_name="test-profiles-core",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port="auto")],
        )
        optional_svc = ServiceDefinition(
            name="optional",
            image_name="test-profiles-opt",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port="auto")],
            profiles=["extended"],
        )

        stack.add_services([core_svc, optional_svc])

        try:
            # Start with no profiles — only core (no-profile) should run
            stack.start_all(profiles=["core"], wait=False)
            assert "core" in stack._containers
            # optional should not be started since profile doesn't match
            assert "optional" not in stack._containers
        finally:
            stack.destroy()

    def test_auto_port_allocation(self, simple_dockerfile):
        """Auto port allocation assigns unique host ports."""
        from tolokaforge.docker.ports import PortConfig
        from tolokaforge.docker.stack import ServiceDefinition, ServiceStack

        context_dir, dockerfile = simple_dockerfile

        stack = ServiceStack(prefix=f"test-autoport-{uuid.uuid4().hex[:6]}")
        svc = ServiceDefinition(
            name="auto",
            image_name="test-autoport-svc",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port="auto")],
        )
        stack.add_service(svc)

        try:
            stack.start_all(wait=False)
            # Port should have been resolved
            assert "auto" in stack._containers
        finally:
            stack.destroy()

    def test_destroy_cleanup(self, simple_dockerfile):
        """Destroy removes all containers and networks."""
        from tolokaforge.docker.ports import PortConfig
        from tolokaforge.docker.stack import ServiceDefinition, ServiceStack

        context_dir, dockerfile = simple_dockerfile

        stack = ServiceStack(prefix=f"test-cleanup-{uuid.uuid4().hex[:6]}")
        svc = ServiceDefinition(
            name="cleanup",
            image_name="test-cleanup-svc",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port="auto")],
        )
        stack.add_service(svc)

        try:
            stack.start_all(wait=False)
            assert len(stack._containers) == 1
        finally:
            stack.destroy()
        assert len(stack._containers) == 0
        assert len(stack._networks) == 0
        assert len(stack._images) == 0

    def test_context_manager(self, simple_dockerfile):
        """ServiceStack works as a context manager."""
        from tolokaforge.docker.ports import PortConfig
        from tolokaforge.docker.stack import ServiceDefinition, ServiceStack

        context_dir, dockerfile = simple_dockerfile

        stack = ServiceStack(prefix=f"test-ctxmgr-{uuid.uuid4().hex[:6]}")
        svc = ServiceDefinition(
            name="ctxmgr",
            image_name="test-ctxmgr-svc",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port="auto")],
        )
        stack.add_service(svc)

        with stack:
            statuses = stack.get_status()
            assert "ctxmgr" in statuses

        # After exiting context, containers should be cleaned up
        assert len(stack._containers) == 0

    def test_health_check_all(self, tmp_path):
        """health_check_all returns status for all services."""
        from tolokaforge.docker.health import HealthProbe
        from tolokaforge.docker.ports import PortConfig
        from tolokaforge.docker.stack import ServiceDefinition, ServiceStack

        context_dir = tmp_path / "context"
        context_dir.mkdir()
        dockerfile = context_dir / "Dockerfile"
        # Write the server script as a separate file to COPY into the image
        server_py = context_dir / "server.py"
        server_py.write_text(
            textwrap.dedent(
                """\
            from http.server import HTTPServer, BaseHTTPRequestHandler
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                def log_message(self, *a): pass
            HTTPServer(('', 8080), H).serve_forever()
            """
            )
        )
        dockerfile.write_text(
            textwrap.dedent(
                """\
            FROM python:3.12-alpine
            COPY server.py /server.py
            CMD ["python", "/server.py"]
            """
            )
        )

        _prefix = f"test-health-{uuid.uuid4().hex[:6]}"
        # Use fixed host port so health probe URL can reference it directly
        _host_port = 28080
        stack = ServiceStack(prefix=_prefix)
        svc = ServiceDefinition(
            name="healthsvc",
            image_name="test-health-svc",
            dockerfile=str(dockerfile),
            context=str(context_dir),
            ports=[PortConfig(container_port=8080, host_port=_host_port)],
            health_probe=HealthProbe.http(
                url=f"http://localhost:{_host_port}/",
                timeout_s=15.0,
                interval_s=0.5,
            ),
        )
        stack.add_service(svc)

        try:
            stack.start_all(wait=True)
            statuses = stack.health_check_all()
            assert "healthsvc" in statuses
            assert statuses["healthsvc"].health in ("healthy", "no_probe")
        finally:
            stack.destroy()
