"""Keyless rc-smoke for the all-in-one ``tolokaforge-standalone`` image.

Runs only inside the publish workflow's ``smoke`` gate (and in a pre-publish
local exercise): skipped unless ``TOLOKAFORGE_SMOKE_IMAGE_TAG`` names the tag the
``publish`` job just pushed. For a local run, build the image, tag it
``tolokasoft1/tolokaforge-standalone:local`` and set
``TOLOKAFORGE_SMOKE_IMAGE_TAG=local`` + ``TOLOKAFORGE_SMOKE_IMAGE_LOCAL=1``.

The all-in-one image collapses the five-container stack into one container, so a
single ``docker run`` must bring up all four services on loopback. The gate
asserts, keyless:

* Docker health reaches ``healthy`` (the runner's gRPC channel-ready probe).
* The three env services answer ``/health`` 200 on localhost INSIDE the
  container (8000 db-service, 8001 rag-service, 8080 mock-web) — proving the
  supervisor brought them all up co-located.
* The baked-in ``tolokaforge`` CLI reports the tagged version (the image ships
  the full ``[dx]`` front-end, unlike the per-component runner image).
* ``tolokaforge run-trial`` speaks the ``v:1`` wire on garbage stdin — the same
  keyless runner-surface check the per-image smoke runs, here proving the runner
  process inside the all-in-one image is wired.

A red assertion fails the workflow's ``smoke`` job and blocks the ``:latest`` /
``:X.Y`` promotion for every image.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator

import pytest

from tests.integration.deploy.conftest import (
    docker_exec,
    obtain_image,
    published_image_ref,
    run_standalone,
    smoke_image_tag,
)
from tests.utils.docker_helpers import wait_for_health

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
    pytest.mark.slow,
    pytest.mark.skipif(
        smoke_image_tag() is None,
        reason="TOLOKAFORGE_SMOKE_IMAGE_TAG unset — rc-smoke runs only against pushed images",
    ),
]

# The all-in-one image installs the rag search stack and bakes an embedding
# model; its start-period is 40s and cold model load adds more, so allow headroom
# over the per-component default.
_HEALTHY_TIMEOUT_S = 300.0

# (service, in-container health URL) for each co-located env service.
_SERVICE_HEALTH: tuple[tuple[str, str], ...] = (
    ("db-service", "http://localhost:8000/health"),
    ("rag-service", "http://localhost:8001/health"),
    ("mock-web", "http://localhost:8080/health"),
)

_GARBAGE_ENVELOPE = "not a valid start envelope\n"


def _tag() -> str:
    tag = smoke_image_tag()
    assert tag is not None
    return tag


@pytest.fixture(scope="module")
def standalone_container(docker_daemon: None) -> Iterator[str]:
    """A pulled, running, healthy all-in-one container shared by the checks."""
    ref = published_image_ref("standalone", _tag())
    obtained = obtain_image(ref)
    if obtained.returncode != 0:
        pytest.fail(f"could not obtain {ref}: {obtained.stderr.strip()}")
    container_id = run_standalone(ref)
    try:
        status = wait_for_health(container_id, timeout_s=_HEALTHY_TIMEOUT_S)
        if status != "healthy":
            logs = subprocess.run(
                ["docker", "logs", "--tail", "40", container_id],
                capture_output=True,
                text=True,
            )
            pytest.fail(
                f"all-in-one never became healthy (last status: {status!r})\n"
                f"logs:\n{logs.stdout}\n{logs.stderr}"
            )
        yield container_id
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


def test_standalone_reports_healthy(standalone_container: str) -> None:
    """The single container's entrypoint starts and health reaches healthy."""
    # The fixture already asserted healthy; this names the property explicitly.
    assert standalone_container


@pytest.mark.parametrize(("service", "url"), _SERVICE_HEALTH)
def test_colocated_service_answers_on_loopback(
    standalone_container: str, service: str, url: str
) -> None:
    """Each env service answers /health 200 on localhost inside the container."""
    probe = docker_exec(
        standalone_container,
        [
            "python",
            "-c",
            f"import urllib.request,sys; "
            f"sys.exit(0 if urllib.request.urlopen('{url}', timeout=5).status==200 else 1)",
        ],
    )
    assert probe.returncode == 0, (
        f"{service} did not answer 200 at {url} inside the all-in-one container "
        f"(rc={probe.returncode}): {probe.stderr.strip()}"
    )


def test_standalone_cli_version_matches_tag(standalone_container: str) -> None:
    """``tolokaforge --version`` reports the tagged base version (ships [dx])."""
    base_version = _tag().split("-rc.")[0]
    proc = docker_exec(standalone_container, ["tolokaforge", "--version"])
    assert proc.returncode == 0, f"--version failed (rc={proc.returncode}): {proc.stderr.strip()}"
    # `local` tag has no semantic version to match; just require a working CLI.
    if base_version != "local":
        missing = f"--version {proc.stdout.strip()!r} lacks base version {base_version!r}"
        assert base_version in proc.stdout, missing


def test_standalone_run_trial_speaks_wire(standalone_container: str) -> None:
    """``tolokaforge run-trial`` emits one well-formed ``v:1`` wire line keylessly."""
    proc = docker_exec(standalone_container, ["tolokaforge", "run-trial"], stdin=_GARBAGE_ENVELOPE)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (
        f"expected exactly one wire line, got {len(lines)}: "
        f"stdout={proc.stdout!r} stderr={proc.stderr.strip()!r} rc={proc.returncode}"
    )
    try:
        envelope = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        pytest.fail(f"run-trial stdout is not JSON: {lines[0]!r} ({exc})")
    assert envelope.get("v") == 1, f"wire line missing v:1: {envelope!r}"
    assert envelope.get("type") in {"result", "error"}, f"unexpected wire type: {envelope!r}"
