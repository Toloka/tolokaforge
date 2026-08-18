"""A container that never becomes ready says which service, and shows why.

testcontainers' ``HttpWaitStrategy`` reports only the URL it gave up on, so a
fixture timeout used to read as a bare "waited too long for
http://localhost:32943/health" — no service name, no reason, and the reason (the
service answering 503, or dying on a bad volume) sitting unread in the
container's own log. ``start_container`` attaches both.

The failure is provoked the honest way: a real rag-service container behind a
wait for a path it does not serve. Nothing about the container is faked, so the
log tail under assertion is a real service's real output.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from testcontainers.core.generic import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

from tests.utils.containers import ContainerStartupError, start_container
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.docker import builder

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker, pytest.mark.slow]

_UNSERVED_PATH = "/a-path-the-rag-service-does-not-serve"
_WAIT_TIMEOUT_S = 15


def test_wait_failure_names_the_service_and_carries_its_logs() -> None:
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")

    container = DockerContainer(builder.build_image("rag-service").full_tag)
    container.with_exposed_ports(8001)
    container.waiting_for(
        HttpWaitStrategy(8001, path=_UNSERVED_PATH)
        .for_status_code(200)
        .with_startup_timeout(timedelta(seconds=_WAIT_TIMEOUT_S))
    )

    try:
        with pytest.raises(ContainerStartupError) as raised:
            start_container(container, "rag-service")
    finally:
        container.stop()

    message = str(raised.value)
    assert "rag-service never became ready" in message, message
    assert "Uvicorn running on http://0.0.0.0:8001" in message, (
        "the container's own log must ride along with the timeout — without it the reader has "
        f"only the URL the wait strategy gave up on:\n{message}"
    )
