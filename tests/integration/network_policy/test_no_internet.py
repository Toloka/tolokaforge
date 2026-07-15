"""``no_internet`` blocks application egress while preserving the runner's
host-published port and inter-service DNS.

Materialises the two-service stack through the real backend under the
default ``no_internet`` policy and asserts the honest contract: the ``app``
service cannot reach the public internet (raw IP *or* DNS name), yet the
runner's published port stays host-reachable and services still resolve each
other by name.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.network_policy import _harness
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.trial import NetworkPolicy

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial backend needs it)",
)
def test_no_internet_blocks_egress_but_keeps_runner_reachable(tmp_path) -> None:
    manifest = _harness.write_manifest(tmp_path / "stack", NetworkPolicy.NO_INTERNET)
    backend = PerTrialRuntimeBackend()
    handle = backend.provision(_harness.make_spec(manifest, "netpolicy-no-internet:0"))

    try:
        raw_ip = _harness.run_curl(handle.compose, _harness.PUBLIC_IP_URL)
        assert raw_ip.returncode != 0, (
            f"raw-IP egress to {_harness.PUBLIC_IP_URL} should be blocked under no_internet; "
            f"curl exited 0 (stdout: {raw_ip.stdout!r})"
        )

        dns = _harness.run_curl(handle.compose, _harness.PUBLIC_DNS_URL)
        assert dns.returncode != 0, (
            f"DNS-name egress to {_harness.PUBLIC_DNS_URL} should be blocked under no_internet; "
            f"curl exited 0 (stdout: {dns.stdout!r})"
        )

        inter = _harness.run_curl(handle.compose, _harness.INTER_SERVICE_URL)
        assert inter.returncode == 0, (
            "inter-service DNS + connectivity must survive no_internet; "
            f"curl to {_harness.INTER_SERVICE_URL} exited {inter.returncode} "
            f"(stderr: {inter.stderr!r})"
        )
        assert _harness.RUNNER_OK_BODY in inter.stdout

        runner_url = backend.endpoints(handle).runner_url
        response = httpx.get(runner_url, timeout=10)
        assert response.status_code == 200, f"runner {runner_url} not host-reachable"
        assert _harness.RUNNER_OK_BODY in response.text
    finally:
        backend.teardown(handle)
