"""Real-docker acceptance gate for building first-party images from a wheel install.

#1738: on a wheel install ``repo_root()`` is ``site-packages``; rag-service and
grader emitted repo-relative sibling paths (``tolokaforge_models/``,
``tolokaforge_coding_harnesses/``) that do not exist there, so their
``sibling-wheel-builder`` stage ran ``hatchling build`` in a directory with no
``pyproject.toml`` and the build died before the first trial.

The unit and canonical tiers assert the assembled build *context*. This tier runs
an actual ``docker build`` end-to-end from a scratch wheel-install venv — the only
tier that proves the Dockerfile's in-container ``hatchling build`` stage actually
compiles against the packaged ``_subset_build/`` sources. For rag-service it also
exercises ``resolve_wheel`` on a wheel install (#866).

Needs a Docker daemon; builds three images that compile wheels in-container, so it
is marked ``slow`` and runs in the push/schedule integration lane.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tests.utils.wheel_builds import (
    base_wheel_in,
    build_workspace_wheels,
    make_wheel_install_venv,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker, pytest.mark.slow]

_BUILD_TIMEOUT_S = 1800


@pytest.fixture(scope="module")
def wheel_install_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch venv with the base tolokaforge wheel installed — a faithful
    ``pip install tolokaforge``, where ``repo_root()`` is ``site-packages``."""
    dist = build_workspace_wheels(tmp_path_factory.mktemp("dist"))
    venv = tmp_path_factory.mktemp("wheel_venv")
    return make_wheel_install_venv(venv, dist, base_wheel_in(dist))


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
@pytest.mark.parametrize("service", ["runner", "rag-service", "grader"])
def test_image_builds_from_wheel_install(
    service: str,
    wheel_install_python: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """``docker build`` of *service* must succeed when driven from a wheel-install
    venv — the exact repro of #1738 for rag-service / grader.

    The build runs from a CWD outside the repo so a ``sys.path[0]`` prepend cannot
    shadow the wheel-installed package with the source tree; the probe asserts it
    really took the wheel-install branch before building."""
    probe_cwd = tmp_path_factory.mktemp("build_cwd_outside_repo")
    out_file = probe_cwd / "result.json"
    probe = textwrap.dedent(f"""
        import json
        import sys
        from pathlib import Path

        from tolokaforge.docker.builder import build_image, repo_root

        root = repo_root()
        assert not (root / "pyproject.toml").is_file(), (
            f"probe is not running as a wheel install (repo_root={{root}} has a "
            "pyproject.toml); the test would not exercise the wheel-install branch"
        )
        image = build_image({service!r}, force=True)
        Path(sys.argv[1]).write_text(json.dumps({{"tag": image.full_tag, "repo_root": str(root)}}))
        """).strip()

    result = subprocess.run(
        [str(wheel_install_python), "-c", probe, str(out_file)],
        capture_output=True,
        text=True,
        cwd=str(probe_cwd),
        timeout=_BUILD_TIMEOUT_S,
    )
    if result.returncode != 0:
        pytest.fail(
            f"docker build of '{service}' from a wheel install failed "
            f"(exit {result.returncode}) — this is the #1738 failure mode:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    data = json.loads(out_file.read_text())
    tag = data["tag"]
    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", tag], capture_output=True, text=True
        )
        assert inspect.returncode == 0, (
            f"'{service}' reported a successful build as {tag!r} but the image is "
            f"absent from the daemon:\n{inspect.stderr}"
        )
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, text=True)
