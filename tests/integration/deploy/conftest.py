"""Shared primitives for the deploy integration suite.

Both the keyless image-level rc-smoke and the local-vs-published parity
runbook drive first-party images through the same
``docker`` CLI operations: resolve an image reference, make it available, run it
standalone, wait for Docker health, and exec documented subcommands. Those
primitives live here so every deploy module shares one implementation.

Image references are always ``tolokasoft1/tolokaforge-<component>:<tag>``. In the
publish workflow the tag is the freshly-pushed ``:{version}`` and the images pull
from Docker Hub. For a pre-publish local exercise, tag the locally-built images
``tolokasoft1/tolokaforge-<component>:local`` and set
``TOLOKAFORGE_SMOKE_IMAGE_LOCAL=1`` so the primitives confirm the image is
present locally instead of pulling from the registry.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available

IMAGE_COMPONENTS: tuple[str, ...] = (
    "runner",
    "db-service",
    "rag-service",
    "mock-web",
    "grader",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "standalone" / "docker-compose.yaml"

HEALTHY_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 2.0
_TRUTHY = frozenset({"1", "true", "yes"})

# Locally-built images are tagged ``:local`` and carry the build host's native
# architecture, unlike the published amd64-only images the compose default
# targets.
_LOCAL_TAG = "local"
_MACHINE_TO_PLATFORM: dict[str, str] = {
    "arm64": "linux/arm64",
    "aarch64": "linux/arm64",
    "x86_64": "linux/amd64",
    "amd64": "linux/amd64",
}

# The runner image's own HEALTHCHECK treats the gRPC server as serving once a
# client channel becomes ready; reused verbatim so the debugging-runbook health
# step probes exactly what the image self-reports.
_GRPC_READY_PROBE = (
    "import grpc; ch = grpc.insecure_channel('localhost:50051'); "
    "grpc.channel_ready_future(ch).result(timeout=2)"
)
_VERSION_RE = re.compile(r"\b\d+\.\d+")


def smoke_image_tag() -> str | None:
    """The tag under test, or ``None`` when the rc-smoke run-guard is not set."""
    return os.environ.get("TOLOKAFORGE_SMOKE_IMAGE_TAG") or None


def published_image_ref(component: str, tag: str) -> str:
    """Full Docker Hub reference for a first-party image component and tag."""
    return f"tolokasoft1/tolokaforge-{component}:{tag}"


def _use_local_images() -> bool:
    return os.environ.get("TOLOKAFORGE_SMOKE_IMAGE_LOCAL", "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class StackHandle:
    """A brought-up standalone stack: its compose project and image-tag mode."""

    mode: str
    project: str
    tag: str


def _host_platform() -> str:
    """Docker platform string matching the host CPU (via ``platform.machine()``)."""
    machine = platform.machine().lower()
    try:
        return _MACHINE_TO_PLATFORM[machine]
    except KeyError:
        raise RuntimeError(f"unsupported host architecture: {machine!r}") from None


def _compose_env(tag: str) -> dict[str, str]:
    """Subprocess env for ``docker compose`` at ``tag``.

    ``TOLOKAFORGE_IMAGE_TAG`` rides the env (Compose interpolation reads it with
    precedence over any sibling ``.env``), so the keyless lane needs no ``.env``
    file — provider keys, when present, are inherited from ``os.environ`` the same
    way. The ``:local`` tag holds native-arch images, so its platform is pinned to
    the host arch to match; an explicit ``TOLOKAFORGE_PLATFORM`` is never
    overridden, and other tags keep the recipe's amd64 default.
    """
    env = {**os.environ, "TOLOKAFORGE_IMAGE_TAG": tag}
    if tag == _LOCAL_TAG and "TOLOKAFORGE_PLATFORM" not in os.environ:
        env["TOLOKAFORGE_PLATFORM"] = _host_platform()
    return env


def compose(
    project: str,
    args: list[str],
    tag: str,
    *,
    input_text: str | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose`` against the standalone recipe for one project."""
    env = _compose_env(tag)
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
        check=check,
    )


def build_and_tag_local() -> None:
    """Build the four images from the tree and tag each ``tolokasoft1/…:local``."""
    from tolokaforge.docker.builder import build_image

    for component in IMAGE_COMPONENTS:
        image = build_image(component)
        ref = published_image_ref(component, "local")
        subprocess.run(["docker", "tag", image.full_tag, ref], check=True, capture_output=True)


def pull_published(tag: str) -> bool:
    """Whether all four ``tolokasoft1/…:<tag>`` images pull successfully."""
    for component in IMAGE_COMPONENTS:
        ref = published_image_ref(component, tag)
        if subprocess.run(["docker", "pull", ref], capture_output=True, text=True).returncode != 0:
            return False
    return True


def obtain_image(ref: str) -> subprocess.CompletedProcess[str]:
    """Make ``ref`` available locally and return the docker process for asserting.

    Pulls ``ref`` from the registry (the workflow path), or — in local-exercise
    mode — inspects it to confirm a locally-built, locally-tagged image is
    present. The caller asserts ``returncode == 0``.
    """
    op = ["image", "inspect", ref] if _use_local_images() else ["pull", ref]
    return subprocess.run(["docker", *op], capture_output=True, text=True)


def docker_exec(
    container_id: str, args: list[str], *, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``docker exec`` (``-i`` when feeding stdin) and capture its output."""
    flags = ["-i"] if stdin is not None else []
    return subprocess.run(
        ["docker", "exec", *flags, container_id, *args],
        capture_output=True,
        text=True,
        input=stdin,
    )


def run_standalone(ref: str) -> str:
    """Start ``ref`` detached with ``--rm`` and return the container id."""
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", ref], capture_output=True, text=True, check=True
    )
    return started.stdout.strip()


@dataclass(frozen=True)
class RunbookOutcome:
    """Source-independent result of the operator debugging runbook.

    Each step is reduced to a comparable observation: the volatile version string
    becomes a well-formedness flag so a locally-built image and a published
    release — whose version numbers legitimately differ — still compare equal
    when the runbook behaves identically against both.
    """

    version_ok: bool
    grpc_serving: bool
    log_tail_nonempty: bool


def _await_grpc_serving(container_id: str, timeout_s: float = HEALTHY_TIMEOUT_S) -> bool:
    """Whether the runner's gRPC channel becomes ready within ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        probe = docker_exec(container_id, ["python", "-c", _GRPC_READY_PROBE])
        if probe.returncode == 0:
            return True
        time.sleep(_POLL_INTERVAL_S)
    return False


def run_debugging_runbook(container_id: str) -> RunbookOutcome:
    """Run the fixed operator debugging runbook against a running runner container.

    The three probes an operator runs regardless of image source: print the CLI
    version, confirm the gRPC server is serving (the channel-ready check the
    image's own HEALTHCHECK performs), and tail the container log. Two sources
    that behave identically produce an equal :class:`RunbookOutcome`.
    """
    version = docker_exec(container_id, ["tolokaforge", "--version"])
    version_ok = version.returncode == 0 and bool(_VERSION_RE.search(version.stdout))
    grpc_serving = _await_grpc_serving(container_id)
    logs = subprocess.run(
        ["docker", "logs", "--tail", "20", container_id], capture_output=True, text=True
    )
    log_tail_nonempty = bool((logs.stdout + logs.stderr).strip())
    return RunbookOutcome(
        version_ok=version_ok,
        grpc_serving=grpc_serving,
        log_tail_nonempty=log_tail_nonempty,
    )


@pytest.fixture(scope="session")
def docker_daemon() -> None:
    """Skip the whole deploy test if no Docker daemon is reachable."""
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
