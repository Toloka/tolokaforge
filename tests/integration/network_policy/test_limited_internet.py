"""``limited_internet`` permits egress only to the declared allowlist.

Materialises the two-service stack through the real backend under
``limited_internet`` with ``example.com`` allowlisted, and asserts the
enforced contract against a real squid sidecar: an allowlisted host is
reachable through the proxy (HTTP 200), a non-allowlisted host and a raw-IP
target are denied by the proxy (HTTP 403 — the connection to the proxy
succeeds, so this is *not* a transport failure), inter-service DNS bypasses
the proxy, and the runner keeps its host-published port and direct egress.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.network_policy import _harness
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.trial import NetworkPolicy

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def _allowlisted_host_reachable() -> bool:
    """True when the allowlisted target answers from the test host — proxy
    enforcement can only be asserted against a host that is actually up. A
    negative result is a network/host flake, not a policy failure, so the
    test skips rather than reds CI."""
    try:
        return httpx.get(_harness.ALLOWLISTED_URL, timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial backend needs it)",
)
def test_limited_internet_allows_only_allowlisted_egress(tmp_path) -> None:
    if not _allowlisted_host_reachable():
        pytest.skip(
            f"{_harness.ALLOWLISTED_URL} unreachable from the test host — "
            "network/host flake, not a policy failure"
        )

    manifest = _harness.write_manifest(
        tmp_path / "stack",
        NetworkPolicy.LIMITED_INTERNET,
        allowlist=[_harness.ALLOWLISTED_HOST],
    )
    backend = PerTrialRuntimeBackend()
    handle = backend.provision(_harness.make_spec(manifest, "netpolicy-limited-internet:0"))

    try:
        allowed = _harness.run_curl_status(handle.compose, _harness.ALLOWLISTED_URL)
        assert allowed == "200", (
            f"allowlisted {_harness.ALLOWLISTED_URL} must be reachable through the proxy; "
            f"got HTTP status {allowed!r}"
        )

        denied = _harness.run_curl_status(handle.compose, _harness.DENIED_URL)
        assert denied == "403", (
            f"non-allowlisted {_harness.DENIED_URL} must be refused by the proxy with HTTP 403; "
            f"got HTTP status {denied!r}"
        )

        raw_ip = _harness.run_curl_status(handle.compose, _harness.PUBLIC_IP_URL)
        assert raw_ip == "403", (
            f"raw-IP {_harness.PUBLIC_IP_URL} must be refused by the proxy (allowlist is "
            f"DNS-only, app has no direct egress); got HTTP status {raw_ip!r}"
        )

        inter = _harness.run_curl(handle.compose, _harness.INTER_SERVICE_URL)
        assert inter.returncode == 0, (
            "inter-service DNS + connectivity must survive limited_internet "
            "(NO_PROXY bypasses the proxy for compose services); "
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
