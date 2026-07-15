"""``full_internet`` leaves application egress intact.

The over-enforcement guard: the same stack that is egress-blocked under
``no_internet`` must reach the public internet when the task declares
``full_internet`` (the transform is identity for this policy).
"""

from __future__ import annotations

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
def test_full_internet_allows_public_egress(tmp_path) -> None:
    manifest = _harness.write_manifest(tmp_path / "stack", NetworkPolicy.FULL_INTERNET)
    backend = PerTrialRuntimeBackend()
    handle = backend.provision(_harness.make_spec(manifest, "netpolicy-full-internet:0"))

    try:
        public = _harness.run_curl(handle.compose, _harness.PUBLIC_DNS_URL, max_time=15)
        assert public.returncode == 0, (
            f"public egress to {_harness.PUBLIC_DNS_URL} must succeed under full_internet; "
            f"curl exited {public.returncode} (stderr: {public.stderr!r})"
        )
    finally:
        backend.teardown(handle)
