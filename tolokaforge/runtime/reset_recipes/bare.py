"""``bare`` seed kind — no automatic overlay.

The seed file is available at the resolved path on the host; the task's
compose file consumes it verbatim (as a bind mount, a copy step in the
service's own entrypoint, or however the task author wires it). No
container-side action runs at reset time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.models import SeedRef
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from testcontainers.compose import DockerCompose


class BareDispatcher:
    """No-op recipe: the file already sits where the task's compose file
    expects it, and the reset has nothing to do inside the container."""

    def apply(
        self,
        seed: SeedRef,
        service_name: str,
        compose: DockerCompose,
    ) -> None:
        del seed, service_name, compose


DISPATCHER = BareDispatcher()
RECIPE_REGISTRY["bare"] = DISPATCHER
