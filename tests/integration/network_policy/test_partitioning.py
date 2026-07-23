"""``ServiceSpec.network_access="restricted"`` partitions a sibling off the
harness-injected internal network so it cannot reach the runner or other
default-topology siblings, while keeping the ``default`` control siblings
fully connected.

Materialises a four-service stack through the real
:class:`~tolokaforge.core.per_trial_runtime.PerTrialRuntimeBackend`:

* ``runner`` — nginx returning ``runner-ok``, host-published and health-gated.
* ``tool_bridge_service`` — nginx returning ``bridge-ok`` on a task-declared
  ``tool_bridge`` network; the sibling the restricted service IS allowed to
  reach.
* ``restricted_sibling`` — curl container attached only to ``tool_bridge``.
  Marked ``network_access: restricted`` — must NOT reach ``runner``.
* ``unrestricted_sibling`` — curl container with default topology. Control
  case: must reach ``runner`` under both policies.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.integration.network_policy import _harness
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.trial import NetworkPolicy

pytestmark = [pytest.mark.integration, pytest.mark.docker]


TOOL_BRIDGE_URL = f"http://{_harness.TOOL_BRIDGE_SERVICE}/"
"""HTTP target served by ``tool_bridge_service`` on its task-declared
``tool_bridge`` network — reachable by the restricted sibling."""


def _assert_blocked(result: subprocess.CompletedProcess, *, from_service: str, target: str) -> None:
    """A blocked egress surfaces as a non-zero curl exit (6 = DNS failure,
    7 = connection refused, 28 = timeout). A zero exit is a security
    violation of the partitioning primitive."""
    assert result.returncode != 0, (
        f"network_access='restricted' security violation: {from_service!r} reached "
        f"{target!r} — expected blocked (curl exit 6/7/28), got exit 0 "
        f"(stdout: {result.stdout!r})"
    )


def _assert_reached(
    result: subprocess.CompletedProcess, *, from_service: str, target: str, body: str
) -> None:
    assert result.returncode == 0, (
        f"{from_service!r} could not reach {target!r} — expected 0, got "
        f"{result.returncode} (stderr: {result.stderr!r})"
    )
    assert body in result.stdout, (
        f"{from_service!r} reached {target!r} but body {body!r} missing "
        f"(stdout: {result.stdout!r})"
    )


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial backend needs it)",
)
def test_restricted_sibling_cannot_reach_runner_under_no_internet(tmp_path) -> None:
    manifest = _harness.write_partitioning_manifest(tmp_path / "stack", NetworkPolicy.NO_INTERNET)
    backend = PerTrialRuntimeBackend()
    handle = backend.provision(_harness.make_spec(manifest, "netpolicy-partitioning-no-internet:0"))

    try:
        blocked = _harness.run_curl_from(
            handle.compose, _harness.RESTRICTED_SIBLING_SERVICE, _harness.INTER_SERVICE_URL
        )
        _assert_blocked(
            blocked,
            from_service=_harness.RESTRICTED_SIBLING_SERVICE,
            target=_harness.INTER_SERVICE_URL,
        )

        allowed_sibling = _harness.run_curl_from(
            handle.compose, _harness.RESTRICTED_SIBLING_SERVICE, TOOL_BRIDGE_URL
        )
        _assert_reached(
            allowed_sibling,
            from_service=_harness.RESTRICTED_SIBLING_SERVICE,
            target=TOOL_BRIDGE_URL,
            body=_harness.BRIDGE_OK_BODY,
        )

        control = _harness.run_curl_from(
            handle.compose, _harness.UNRESTRICTED_SIBLING_SERVICE, _harness.INTER_SERVICE_URL
        )
        _assert_reached(
            control,
            from_service=_harness.UNRESTRICTED_SIBLING_SERVICE,
            target=_harness.INTER_SERVICE_URL,
            body=_harness.RUNNER_OK_BODY,
        )
    finally:
        backend.teardown(handle)


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial backend needs it)",
)
def test_restricted_sibling_cannot_reach_runner_under_limited_internet(tmp_path) -> None:
    manifest = _harness.write_partitioning_manifest(
        tmp_path / "stack",
        NetworkPolicy.LIMITED_INTERNET,
        allowlist=[_harness.ALLOWLISTED_HOST],
    )
    backend = PerTrialRuntimeBackend()
    handle = backend.provision(
        _harness.make_spec(manifest, "netpolicy-partitioning-limited-internet:0")
    )

    try:
        blocked = _harness.run_curl_from(
            handle.compose, _harness.RESTRICTED_SIBLING_SERVICE, _harness.INTER_SERVICE_URL
        )
        _assert_blocked(
            blocked,
            from_service=_harness.RESTRICTED_SIBLING_SERVICE,
            target=_harness.INTER_SERVICE_URL,
        )

        allowed_sibling = _harness.run_curl_from(
            handle.compose, _harness.RESTRICTED_SIBLING_SERVICE, TOOL_BRIDGE_URL
        )
        _assert_reached(
            allowed_sibling,
            from_service=_harness.RESTRICTED_SIBLING_SERVICE,
            target=TOOL_BRIDGE_URL,
            body=_harness.BRIDGE_OK_BODY,
        )

        control = _harness.run_curl_from(
            handle.compose, _harness.UNRESTRICTED_SIBLING_SERVICE, _harness.INTER_SERVICE_URL
        )
        _assert_reached(
            control,
            from_service=_harness.UNRESTRICTED_SIBLING_SERVICE,
            target=_harness.INTER_SERVICE_URL,
            body=_harness.RUNNER_OK_BODY,
        )

        restricted_no_proxy = subprocess.run(
            [
                *handle.compose.docker_compose_command(),
                "exec",
                "-T",
                _harness.RESTRICTED_SIBLING_SERVICE,
                "sh",
                "-c",
                'test -z "$HTTP_PROXY"',
            ],
            capture_output=True,
            text=True,
            cwd=handle.compose.context,
            check=False,
        )
        assert restricted_no_proxy.returncode == 0, (
            "restricted sibling must NOT carry HTTP_PROXY env under "
            f"limited_internet (proxy injection must be skipped); shell exited "
            f"{restricted_no_proxy.returncode} (stderr: {restricted_no_proxy.stderr!r})"
        )

        unrestricted_has_proxy = subprocess.run(
            [
                *handle.compose.docker_compose_command(),
                "exec",
                "-T",
                _harness.UNRESTRICTED_SIBLING_SERVICE,
                "sh",
                "-c",
                'test -n "$HTTP_PROXY"',
            ],
            capture_output=True,
            text=True,
            cwd=handle.compose.context,
            check=False,
        )
        assert unrestricted_has_proxy.returncode == 0, (
            "unrestricted sibling must carry HTTP_PROXY env under "
            f"limited_internet (control case); shell exited "
            f"{unrestricted_has_proxy.returncode} (stderr: {unrestricted_has_proxy.stderr!r})"
        )
    finally:
        backend.teardown(handle)
