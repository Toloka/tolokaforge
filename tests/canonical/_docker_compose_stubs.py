"""Non-docker :class:`testcontainers.compose.DockerCompose` stand-in.

Shared by canonical tests that need to exercise the materialise /
teardown code paths without a live docker daemon. Records every
driver-side call (``__init__``, ``start``, ``stop``, ``get_containers``)
in insertion order so parity tests can assert two paths drove the same
lifecycle. ``get_service_host_and_port`` returns a deterministic host +
host-port so endpoint-resolving callers see identical values regardless
of which materialise path drove the stub.
"""

from __future__ import annotations

from typing import Any


class InertDockerCompose:
    """Non-docker :class:`DockerCompose` stand-in for canonical tests.

    Records every driver-side call in :attr:`calls` in the order the
    materialise path drove it. ``get_service_host_and_port`` maps any
    ``(service, port)`` to ``("127.0.0.1", 60000 + port)`` — deterministic
    across paths, port-preserving so ``resolve_env_endpoints`` builds
    identical URLs on both sides.
    """

    def __init__(
        self,
        *,
        context: str,
        compose_file_name: str,
        pull: bool,
        build: bool,
        wait: bool,
    ) -> None:
        self.context = context
        self.compose_file_name = compose_file_name
        self.pull = pull
        self.build = build
        self.wait = wait
        self.calls: list[tuple[str, tuple[Any, ...]]] = [
            ("__init__", (context, compose_file_name, pull, build, wait))
        ]

    def start(self) -> None:
        self.calls.append(("start", ()))

    def stop(self, down: bool = True) -> None:
        self.calls.append(("stop", (down,)))

    def get_containers(self) -> list[Any]:
        self.calls.append(("get_containers", ()))
        return []

    def get_service_host_and_port(self, service_name: str, port: int) -> tuple[str, int]:
        del service_name
        return ("127.0.0.1", 60000 + port)


def driver_state(stub: InertDockerCompose) -> tuple[Any, ...]:
    """Normalise stub state for cross-path equality.

    Compare the compose-file identity, the four constructor flags, and
    the post-``__init__`` call sequence. The ``context`` (temp-dir path)
    differs between paths — mktemp names are process-unique — so it is
    excluded from the comparison; the basename shape is asserted
    separately by the parity tests.
    """
    return (
        stub.compose_file_name,
        stub.pull,
        stub.build,
        stub.wait,
        tuple(stub.calls[1:]),
    )
