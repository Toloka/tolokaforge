"""The credential payload reaches the runner container intact.

Docker Compose interpolates ``$`` in ``environment`` values, so a credential
containing one is silently truncated unless the injected value doubles it.
``docker compose config`` re-escapes on output and cannot serve as the oracle,
so the round trip is only closed by a container reading its own environment:
this test drives ``docker compose`` directly and compares what the process
inside the container sees against the payload the host serialised.

``docker compose`` is driven outside ``provision()`` on purpose — provisioning
runs ``compose.start()`` with ``wait=True`` and then resolves endpoints, so a
print-and-exit service fails provisioning before anything can be read.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.compose_materialisation import (
    apply_network_policy_to_compose_file,
    copy_compose_context,
    inject_runner_credentials,
    make_project_temp_dir,
)
from tolokaforge.core.trial import NetworkPolicy
from tolokaforge.secrets import CONTAINER_SECRETS_ENV_VAR, container_secrets_env

pytestmark = [pytest.mark.integration, pytest.mark.docker]

RUNNER_SERVICE = "runner"

COMPOSE = f"""\
services:
  {RUNNER_SERVICE}:
    image: "alpine:3.20"
"""


def _compose_command(compose_file: Path, project: str, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file), "-p", project, *args]


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (credential delivery needs a real container)",
)
def test_the_runner_container_reads_back_the_exact_payload(
    tmp_path: Path, installed_fake_secrets: dict[str, str]
) -> None:
    source_dir = tmp_path / "stack"
    source_dir.mkdir()
    (source_dir / "docker-compose.yml").write_text(COMPOSE)

    project_dir = make_project_temp_dir("credential-delivery")
    compose_file = project_dir / "docker-compose.yml"
    try:
        copy_compose_context(source_dir / "docker-compose.yml", project_dir)
        apply_network_policy_to_compose_file(
            compose_file, NetworkPolicy.NO_INTERNET, RUNNER_SERVICE, []
        )
        inject_runner_credentials(compose_file, RUNNER_SERVICE)

        expected = container_secrets_env()[CONTAINER_SECRETS_ENV_VAR]
        assert "$" in expected, "the fake payload lost its $-bearing value; this test stops biting"

        result = subprocess.run(
            _compose_command(
                compose_file,
                project_dir.name,
                "run",
                "--rm",
                RUNNER_SERVICE,
                "printenv",
                CONTAINER_SECRETS_ENV_VAR,
            ),
            capture_output=True,
            text=True,
            check=False,
            cwd=project_dir,
            timeout=300,
        )
    finally:
        subprocess.run(
            _compose_command(compose_file, project_dir.name, "down", "--volumes"),
            capture_output=True,
            check=False,
            timeout=180,
        )
        shutil.rmtree(project_dir, ignore_errors=True)

    assert result.returncode == 0, f"printenv failed: {result.stderr}"
    assert result.stdout.rstrip("\n") == expected
