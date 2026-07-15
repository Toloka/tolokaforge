"""``limited_internet`` refuses to materialise.

Docker's ``internal`` flag is binary — a real allowlist needs an
egress-proxy sidecar (#323). Until then the backend must fail loud *before*
any container starts, rather than silently granting full or no internet.
"""

from __future__ import annotations

import pytest

from tests.integration.network_policy import _harness
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.compose_materialisation import NetworkPolicyError
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.trial import NetworkPolicy

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial backend needs it)",
)
def test_limited_internet_refuses_before_any_container_starts(tmp_path) -> None:
    manifest = _harness.write_manifest(tmp_path / "stack", NetworkPolicy.LIMITED_INTERNET)
    backend = PerTrialRuntimeBackend()
    spec = _harness.make_spec(manifest, "netpolicy-limited-internet:0")

    before = _harness.docker_container_ids()
    with pytest.raises(NetworkPolicyError) as exc:
        backend.provision(spec)

    message = str(exc.value)
    assert "limited_internet" in message
    assert "#323" in message
    no_containers = "limited_internet must be refused before any container is created"
    assert _harness.docker_container_ids() <= before, no_containers
