"""The synthesised compose healthchecks must run binaries their images carry.

A compose-level ``healthcheck`` overrides the image's own ``HEALTHCHECK``, so a
probe naming a binary the image does not ship cannot ever pass: the service
stays unhealthy, ``docker compose up --wait`` fails, and the whole run dies in
provisioning with no sign that the service itself is fine.

The db-service image is a ``python:slim`` base with no curl layer, so its probe
has to be the stdlib one its Dockerfile uses. Two adapters synthesise that
service independently — the native one and the terminal-bench one — and both
are checked here, because fixing either alone leaves the other's runs dead.
"""

from __future__ import annotations

import pytest
from tolokaforge_adapter_terminal_bench.compose_synthesis import (
    _db_service_body as tbench_db_service_body,
)

from tolokaforge.adapters.native_harness_synthesis import (
    _db_service_body as native_db_service_body,
)

pytestmark = pytest.mark.unit

_BODIES = {
    "native": native_db_service_body,
    "terminal_bench": tbench_db_service_body,
}


@pytest.mark.parametrize("name", sorted(_BODIES))
def test_db_service_probe_does_not_shell_out_to_curl(name):
    test = _BODIES[name]("tolokaforge-db-service:local")["healthcheck"]["test"]

    assert not any("curl" in part for part in test), (
        f"{name}: the db-service image ships no curl; a curl probe pins the "
        "service unhealthy forever and every run dies in provisioning"
    )


@pytest.mark.parametrize("name", sorted(_BODIES))
def test_db_service_probe_runs_python_against_its_own_health_endpoint(name):
    test = _BODIES[name]("tolokaforge-db-service:local")["healthcheck"]["test"]

    assert test[0] == "CMD"
    assert test[1] == "python"
    assert "urllib.request.urlopen" in test[-1]
    assert "http://localhost:8000/health" in test[-1]
