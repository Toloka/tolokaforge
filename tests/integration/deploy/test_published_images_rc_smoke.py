"""Keyless image-level rc-smoke for the four published first-party images.

Runs only inside the publish workflow's ``smoke`` gate: skipped unless
``TOLOKAFORGE_SMOKE_IMAGE_TAG`` names the tag the ``publish`` job just pushed. So
it is a no-op in the normal PR/local lane and exercises freshly-published images
in-workflow. For every ``tolokasoft1/tolokaforge-{runner,db-service,rag-service,
mock-web}`` image it asserts a fresh ``docker pull`` succeeds and the standalone
entrypoint reaches Docker health ``healthy`` (uniform, thanks to the healthchecks
on all four images).

For the runner it additionally exercises the two documented ``docker exec``
subcommands, keyless: ``tolokaforge --version`` prints the tagged version, and
``tolokaforge run-trial`` emits exactly one well-formed ``{"v":1,"type":...}``
wire line. The run-trial check is deliberately keyless — an ``error`` line
(``ProtocolError`` on garbage stdin, ``cancelled`` on empty stdin, or a missing
LLM key) is an ACCEPTED outcome: it proves the subcommand is present, reads
stdin, and speaks the ``"v":1`` wire. What fails is a click "No such command"
(exit 2, no JSON), a crash (non-JSON stdout), or a wrong/absent ``v``. This is
why the gate needs no provider key and costs zero tokens; a red assertion here
fails the workflow's ``smoke`` job and blocks the ``:latest`` / ``:X.Y``
promotion.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator

import pytest

from tests.integration.deploy.conftest import (
    IMAGE_COMPONENTS,
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

# Garbage stdin the run-trial wire must reject with a well-formed error line,
# proving the subcommand reads stdin and speaks the "v":1 wire without a key.
_GARBAGE_ENVELOPE = "not a valid start envelope\n"


def _tag() -> str:
    """The tag under test; the module ``skipif`` guarantees it is set here."""
    tag = smoke_image_tag()
    assert tag is not None
    return tag


@pytest.fixture(params=IMAGE_COMPONENTS)
def healthy_image(request: pytest.FixtureRequest, docker_daemon: None) -> Iterator[tuple[str, str]]:
    """Yield ``(component, container_id)`` for a pulled, running, healthy image.

    The pull outcome is asserted here so a failed publish surfaces as this
    fixture's error; health is asserted by the test body.
    """
    component: str = request.param
    ref = published_image_ref(component, _tag())
    obtained = obtain_image(ref)
    assert obtained.returncode == 0, f"could not obtain {ref}: {obtained.stderr.strip()}"
    container_id = run_standalone(ref)
    try:
        yield component, container_id
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


@pytest.fixture(scope="module")
def runner_container(docker_daemon: None) -> Iterator[str]:
    """A pulled, running, healthy runner container shared by the exec checks."""
    ref = published_image_ref("runner", _tag())
    obtained = obtain_image(ref)
    if obtained.returncode != 0:
        pytest.fail(f"could not obtain {ref}: {obtained.stderr.strip()}")
    container_id = run_standalone(ref)
    try:
        status = wait_for_health(container_id)
        if status != "healthy":
            pytest.fail(f"runner never became healthy (last status: {status!r})")
        yield container_id
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


def test_published_image_reports_healthy(healthy_image: tuple[str, str]) -> None:
    """Each published image pulls, its entrypoint starts, and health reaches healthy."""
    component, container_id = healthy_image
    status = wait_for_health(container_id)
    assert status == "healthy", f"{component} never became healthy (last status: {status!r})"


def test_runner_version_matches_tag(runner_container: str) -> None:
    """``tolokaforge --version`` on the runner prints the tagged base version."""
    base_version = _tag().split("-rc.")[0]
    proc = docker_exec(runner_container, ["tolokaforge", "--version"])
    assert proc.returncode == 0, f"--version failed (rc={proc.returncode}): {proc.stderr.strip()}"
    stdout = proc.stdout.strip()
    assert base_version in proc.stdout, f"--version {stdout!r} lacks base version {base_version!r}"


def test_runner_run_trial_speaks_wire(runner_container: str) -> None:
    """``tolokaforge run-trial`` emits exactly one well-formed ``v:1`` wire line.

    Garbage stdin is fed on purpose: a well-formed ``error`` line is a pass — it
    proves the documented exec subcommand is present, reads stdin, and speaks the
    wire keylessly. A click "No such command", a crash, or an absent/wrong ``v``
    fails the assertion.
    """
    proc = docker_exec(runner_container, ["tolokaforge", "run-trial"], stdin=_GARBAGE_ENVELOPE)
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
