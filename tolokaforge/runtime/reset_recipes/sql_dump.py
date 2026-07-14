"""``sql_dump`` seed kind — restore a SQL dump into the service's
database.

Streams the dump bytes into the service container via ``docker exec`` and
pipes them through the service's SQL client. The client command is
inferred from the service image (``postgres:*`` → ``psql``, otherwise
``psql`` is used as the default — task packs that need a different
client override by naming the seed with a matching kind in a future
recipe module).

Environment expected by the container: ``PGUSER`` / ``POSTGRES_USER``
(command falls back to ``postgres``) and ``PGDATABASE`` /
``POSTGRES_DB`` (defaults to ``postgres``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.models import SeedRef
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from testcontainers.compose import DockerCompose


class SqlDumpDispatcher:
    """Load a ``.sql`` dump into a service's Postgres/SQL runtime.

    Executes ``psql`` (or another SQL client the image ships) inside the
    service container, piping the dump bytes on stdin. Errors surface as
    :class:`RuntimeError` naming the service, the seed path, and the
    command output — the reset is not allowed to silently succeed with
    an inconsistent database.
    """

    def apply(
        self,
        seed: SeedRef,
        service_name: str,
        compose: DockerCompose,
    ) -> None:
        import shlex
        import subprocess

        dump_bytes = seed.path.read_bytes()
        docker_compose_cmd = list(compose.docker_compose_command())
        exec_cmd = [
            *docker_compose_cmd,
            "exec",
            "-T",
            service_name,
            "sh",
            "-c",
            'psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}"',
        ]
        result = subprocess.run(
            exec_cmd,
            input=dump_bytes,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"sql_dump reset failed for service {service_name!r} "
                f"from seed {seed.path!s}: rc={result.returncode} "
                f"cmd={shlex.join(exec_cmd)} "
                f"stdout={result.stdout.decode(errors='replace')!r} "
                f"stderr={result.stderr.decode(errors='replace')!r}"
            )


DISPATCHER = SqlDumpDispatcher()
RECIPE_REGISTRY["sql_dump"] = DISPATCHER
