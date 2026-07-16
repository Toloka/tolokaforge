"""``redis_dump`` seed kind — restore an RDB snapshot into a Redis
service.

Copies the ``.rdb`` into the service's ``/data`` directory as
``dump.rdb`` and restarts the service — Redis loads ``dump.rdb`` at
startup regardless of the ``save`` config. Works against any image
built on the official ``redis`` upstream (data-dir ``/data`` is the
upstream default). ``DEBUG RELOAD`` is not used: it is an internal
RDB-serialisation test that either overwrites the seed with the
running state before reading, or (with ``NOSAVE``) reads from an
implementation-defined snapshot cache that is not guaranteed to be
the file we just wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.models import SeedRef
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from testcontainers.compose import DockerCompose

REDIS_DATA_DIR = "/data"
REDIS_DUMP_NAME = "dump.rdb"


RESTART_PING_MAX_ATTEMPTS = 30
"""Polls of ``redis-cli PING`` after restart before giving up. One
attempt per second."""


class RedisDumpDispatcher:
    """Copy an RDB snapshot into a Redis service and restart the service.

    Uses ``docker compose cp`` to place ``dump.rdb`` into the service's
    data directory, then ``docker compose restart <service>``. Redis
    loads ``dump.rdb`` at startup by default (``dbfilename`` config;
    unrelated to whether the ``save`` config enables persistence). A
    ``PING`` poll then waits for the restarted process to accept
    connections before returning. Each stage surfaces ``RuntimeError``
    on failure rather than proceeding with a half-reset service.
    """

    def apply(
        self,
        seed: SeedRef,
        service_name: str,
        compose: DockerCompose,
    ) -> None:
        import shlex
        import subprocess
        import time

        docker_compose_cmd = list(compose.docker_compose_command())
        cp_cmd = [
            *docker_compose_cmd,
            "cp",
            str(seed.path),
            f"{service_name}:{REDIS_DATA_DIR}/{REDIS_DUMP_NAME}",
        ]
        cp_result = subprocess.run(cp_cmd, capture_output=True, check=False, cwd=compose.context)
        if cp_result.returncode != 0:
            raise RuntimeError(
                f"redis_dump reset (copy stage) failed for service "
                f"{service_name!r} from seed {seed.path!s}: "
                f"rc={cp_result.returncode} cmd={shlex.join(cp_cmd)} "
                f"stderr={cp_result.stderr.decode(errors='replace')!r}"
            )

        restart_cmd = [*docker_compose_cmd, "restart", service_name]
        restart_result = subprocess.run(
            restart_cmd, capture_output=True, check=False, cwd=compose.context
        )
        if restart_result.returncode != 0:
            raise RuntimeError(
                f"redis_dump reset (restart stage) failed for service "
                f"{service_name!r}: rc={restart_result.returncode} "
                f"cmd={shlex.join(restart_cmd)} "
                f"stderr={restart_result.stderr.decode(errors='replace')!r}"
            )

        ping_cmd = [
            *docker_compose_cmd,
            "exec",
            "-T",
            service_name,
            "redis-cli",
            "PING",
        ]
        for _ in range(RESTART_PING_MAX_ATTEMPTS):
            ping_result = subprocess.run(
                ping_cmd, capture_output=True, check=False, cwd=compose.context
            )
            if ping_result.returncode == 0 and b"PONG" in ping_result.stdout:
                return
            time.sleep(1)

        raise RuntimeError(
            f"redis_dump reset (ping stage) failed for service "
            f"{service_name!r}: did not accept PING within "
            f"{RESTART_PING_MAX_ATTEMPTS}s after restart"
        )


DISPATCHER = RedisDumpDispatcher()
RECIPE_REGISTRY["redis_dump"] = DISPATCHER
