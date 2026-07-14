"""``redis_dump`` seed kind — restore an RDB snapshot into a Redis
service.

Copies the ``.rdb`` into the service's ``/data`` directory as
``dump.rdb`` and asks Redis to reload it via ``DEBUG RELOAD``. Works
against any image built on the official ``redis`` upstream (data-dir
``/data`` is the upstream default).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.models import SeedRef
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from testcontainers.compose import DockerCompose

REDIS_DATA_DIR = "/data"
REDIS_DUMP_NAME = "dump.rdb"


class RedisDumpDispatcher:
    """Copy an RDB snapshot into a Redis service and reload it.

    Uses ``docker cp`` to place the dump, then ``docker exec`` +
    ``redis-cli DEBUG RELOAD`` to swap the in-memory dataset. Both stages
    surface ``RuntimeError`` on failure rather than proceeding with a
    half-reset service.
    """

    def apply(
        self,
        seed: SeedRef,
        service_name: str,
        compose: DockerCompose,
    ) -> None:
        import shlex
        import subprocess

        docker_compose_cmd = list(compose.docker_compose_command())
        cp_cmd = [
            *docker_compose_cmd,
            "cp",
            str(seed.path),
            f"{service_name}:{REDIS_DATA_DIR}/{REDIS_DUMP_NAME}",
        ]
        cp_result = subprocess.run(cp_cmd, capture_output=True, check=False)
        if cp_result.returncode != 0:
            raise RuntimeError(
                f"redis_dump reset (copy stage) failed for service "
                f"{service_name!r} from seed {seed.path!s}: "
                f"rc={cp_result.returncode} cmd={shlex.join(cp_cmd)} "
                f"stderr={cp_result.stderr.decode(errors='replace')!r}"
            )

        reload_cmd = [
            *docker_compose_cmd,
            "exec",
            "-T",
            service_name,
            "redis-cli",
            "DEBUG",
            "RELOAD",
        ]
        reload_result = subprocess.run(reload_cmd, capture_output=True, check=False)
        if reload_result.returncode != 0:
            raise RuntimeError(
                f"redis_dump reset (reload stage) failed for service "
                f"{service_name!r}: rc={reload_result.returncode} "
                f"cmd={shlex.join(reload_cmd)} "
                f"stdout={reload_result.stdout.decode(errors='replace')!r} "
                f"stderr={reload_result.stderr.decode(errors='replace')!r}"
            )


DISPATCHER = RedisDumpDispatcher()
RECIPE_REGISTRY["redis_dump"] = DISPATCHER
