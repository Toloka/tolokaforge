"""``filesystem_dir`` seed kind — copy a directory tree into a service's
workspace.

The seed path points at a directory on the host. The dispatcher wipes
the service's target workspace and copies the seed tree into it. Target
path defaults to ``/workspace`` inside the container; task packs that
mount a different workspace override by declaring the seed against a
service whose compose entry mounts the matching path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.models import SeedRef
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from testcontainers.compose import DockerCompose

WORKSPACE_TARGET = "/workspace"
"""Default in-container path a ``filesystem_dir`` seed writes into."""


class FilesystemDirDispatcher:
    """Copy the seed directory tree into a service container's workspace.

    Wipes ``WORKSPACE_TARGET`` first so a partial-copy mid-trial does
    not leave stale files behind. Uses ``docker cp`` under the compose
    project's namespace so the underlying container id does not have to
    be resolved by the caller.
    """

    def apply(
        self,
        seed: SeedRef,
        service_name: str,
        compose: DockerCompose,
    ) -> None:
        import shlex
        import subprocess

        if not seed.path.is_dir():
            raise RuntimeError(
                f"filesystem_dir seed at {seed.path!s} is not a directory; "
                "the recipe requires a directory tree."
            )

        docker_compose_cmd = list(compose.docker_compose_command())
        wipe_cmd = [
            *docker_compose_cmd,
            "exec",
            "-T",
            service_name,
            "sh",
            "-c",
            f'rm -rf "{WORKSPACE_TARGET}"/* "{WORKSPACE_TARGET}"/.[!.]* 2>/dev/null || true',
        ]
        wipe_result = subprocess.run(
            wipe_cmd, capture_output=True, check=False, cwd=compose.context
        )
        if wipe_result.returncode != 0:
            raise RuntimeError(
                f"filesystem_dir reset (wipe stage) failed for service "
                f"{service_name!r}: rc={wipe_result.returncode} "
                f"cmd={shlex.join(wipe_cmd)} "
                f"stderr={wipe_result.stderr.decode(errors='replace')!r}"
            )

        cp_cmd = [
            *docker_compose_cmd,
            "cp",
            f"{seed.path!s}/.",
            f"{service_name}:{WORKSPACE_TARGET}",
        ]
        cp_result = subprocess.run(cp_cmd, capture_output=True, check=False, cwd=compose.context)
        if cp_result.returncode != 0:
            raise RuntimeError(
                f"filesystem_dir reset (copy stage) failed for service "
                f"{service_name!r} from seed {seed.path!s}: "
                f"rc={cp_result.returncode} cmd={shlex.join(cp_cmd)} "
                f"stderr={cp_result.stderr.decode(errors='replace')!r}"
            )


DISPATCHER = FilesystemDirDispatcher()
RECIPE_REGISTRY["filesystem_dir"] = DISPATCHER
