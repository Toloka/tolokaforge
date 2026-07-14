"""Cross-mode isolation — one manifest, two services with different
labels, one shared stack across two trials.

The stack pairs a stateful ``postgres`` (labelled ``reset``, seeded
back to baseline between trials) with a stateless ``kv-shared`` alpine
(labelled ``shared`` — writes must persist across trials). Trial 1
mutates both; trial 2 sees:

* ``postgres`` restored to the baseline row (recipe fired), and
* ``kv-shared`` still carrying trial 1's mutation (no recipe).

Exercises the ``SharedStackRuntimeBackend.reset_services_for_next_trial``
seam in the shared-stack + task-declared-stack configuration (Case B
under ADR-0018).
"""

from __future__ import annotations

import hashlib
import subprocess
import textwrap
from pathlib import Path

import pytest

from tolokaforge.core.models import ResetSpec, SeedRef, ServiceSpec
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvironmentManifest

pytestmark = [pytest.mark.integration, pytest.mark.docker]


COMPOSE = textwrap.dedent(
    """
    services:
      runner:
        image: "alpine:3.20"
        command: ["sleep", "3600"]
        ports:
          - "50051"
        healthcheck:
          test: ["CMD", "true"]
          interval: 2s
          timeout: 3s
          retries: 30
      postgres:
        image: "postgres:16-alpine"
        environment:
          POSTGRES_USER: "postgres"
          POSTGRES_PASSWORD: "example"
          POSTGRES_DB: "app"
        healthcheck:
          test: ["CMD-SHELL", "pg_isready -U postgres -d app"]
          interval: 2s
          timeout: 3s
          retries: 30
      kv-shared:
        image: "alpine:3.20"
        command: ["sh", "-c", "mkdir -p /state && sleep 3600"]
        healthcheck:
          test: ["CMD", "test", "-d", "/state"]
          interval: 2s
          timeout: 3s
          retries: 30
    """
).strip()


BASELINE_SQL = textwrap.dedent(
    """
    DROP TABLE IF EXISTS widgets;
    CREATE TABLE widgets (id INT PRIMARY KEY, name TEXT);
    INSERT INTO widgets (id, name) VALUES (1, 'baseline');
    """
).strip()


def _psql(backend: SharedStackRuntimeBackend, sql: str) -> str:
    """Run ``sql`` inside the postgres service on the backend's live
    compose stack; return stripped stdout."""
    assert backend._compose is not None  # narrowed by caller
    cmd = [
        *backend._compose.docker_compose_command(),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-t",
        "-A",
        "-U",
        "postgres",
        "-d",
        "app",
        "-c",
        sql,
    ]
    result = subprocess.run(
        cmd, capture_output=True, check=True, text=True, cwd=backend._compose.context
    )
    return result.stdout.strip()


def _kv_write(backend: SharedStackRuntimeBackend, contents: str) -> None:
    assert backend._compose is not None
    subprocess.run(
        [
            *backend._compose.docker_compose_command(),
            "exec",
            "-T",
            "kv-shared",
            "sh",
            "-c",
            f"printf %s '{contents}' > /state/entry",
        ],
        check=True,
        capture_output=True,
        cwd=backend._compose.context,
    )


def _kv_read(backend: SharedStackRuntimeBackend) -> str:
    assert backend._compose is not None
    result = subprocess.run(
        [
            *backend._compose.docker_compose_command(),
            "exec",
            "-T",
            "kv-shared",
            "sh",
            "-c",
            "cat /state/entry 2>/dev/null || printf ''",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=backend._compose.context,
    )
    return result.stdout


def test_cross_mode_isolation_between_trials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared runner + reset postgres + shared kv-shared; two trials;
    postgres reverts, kv-shared persists."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(COMPOSE + "\n")

    seed_file = tmp_path / "seed.sql"
    seed_file.write_text(BASELINE_SQL + "\n")
    seed = SeedRef(
        path=seed_file,
        kind="sql_dump",
        digest="sha256:" + hashlib.sha256(seed_file.read_bytes()).hexdigest(),
    )

    manifest = EnvironmentManifest(
        compose_file=compose_file,
        runner_service="runner",
        services={
            "runner": ServiceSpec(isolation="shared"),
            "postgres": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
            "kv-shared": ServiceSpec(isolation="shared"),
        },
    )

    backend = SharedStackRuntimeBackend(
        env_manifest=manifest,
        run_id="cross-mode-isolation",
        seeds={"baseline": seed},
    )

    # The runner in this test isn't a real tolokaforge runner — bypass
    # the gRPC health-check that ``connect`` normally runs. The reset
    # seam is what we care about here; the RPC surface is exercised
    # elsewhere.
    def _skip_grpc_connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        del timeout, retry_interval

    monkeypatch.setattr(
        "tolokaforge.core.shared_stack_runtime.GrpcRunnerClient.connect",
        _skip_grpc_connect,
    )

    try:
        backend.connect()

        # Trial 1 — establish baseline on both services, then mutate.
        backend.reset_services_for_next_trial(manifest)
        assert _psql(backend, "SELECT name FROM widgets WHERE id = 1;") == "baseline"
        assert _kv_read(backend) == ""

        _psql(backend, "UPDATE widgets SET name = 'trial1' WHERE id = 1;")
        _kv_write(backend, "trial1")

        assert _psql(backend, "SELECT name FROM widgets WHERE id = 1;") == "trial1"
        assert _kv_read(backend) == "trial1"

        # Trial 2 — reset seam fires; postgres reverts, kv-shared persists.
        backend.reset_services_for_next_trial(manifest)
        assert _psql(backend, "SELECT name FROM widgets WHERE id = 1;") == "baseline"
        assert _kv_read(backend) == "trial1"
    finally:
        backend.close()
