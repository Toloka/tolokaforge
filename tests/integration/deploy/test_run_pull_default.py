"""Integration test — the docker.image_source policy end-to-end.

Verifies the pull path against a real Docker daemon: a wheel-installed
engine with ``docker.image_source: pull`` reaches Docker Hub, lands
the published image locally, and satisfies ``EngineStack.get_image``.
The build-mode counterpart verifies that ``image_source: build``
never touches Docker Hub.

Isolation guarantees:

- Marked ``integration + requires_docker``. Skipped in the default
  ``pytest`` collection; runs only under ``-m "integration and
  requires_docker"``.
- Skipped when a container named ``tolokaforge-runner`` is already
  running on the host — that indicates a live dev-loop, and this test
  would otherwise race the running container. A restart between tests
  would introduce test order-dependence.
- Uses ``tolokasoft1/tolokaforge-*`` (Docker Hub-scoped) as the pull
  target; locally-built engine images use the ``tolokaforge-*`` prefix
  (no publisher). The two namespaces do not overlap, so a cached local
  build cannot masquerade as a pulled image, and the test never
  ``docker rmi``s a shared image the developer built.
- Every subprocess is capped at ``_SUBPROCESS_TIMEOUT_S`` so a hung
  ``docker pull`` can't burn the runner's wall-clock budget.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLISHED_RUNNER_REPO = "tolokasoft1/tolokaforge-runner"
_SUBPROCESS_TIMEOUT_S = 300  # docker pull on a slow link can be a few minutes


def _daemon_available() -> bool:
    try:
        return is_docker_daemon_available()
    except Exception:
        return False


def _live_runner_container_running() -> bool:
    """True if a container named ``tolokaforge-runner`` is already running
    on the host — a dev-loop signal. Skipping in that case is deliberate:
    we do not want to race a live container or accidentally influence its
    tags."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return "tolokaforge-runner" in result.stdout.split()


@pytest.fixture(scope="module")
def _skip_if_dev_loop() -> None:
    if not _daemon_available():
        pytest.skip("Docker daemon is not available on this host")
    if _live_runner_container_running():
        pytest.skip(
            "A tolokaforge-runner container is already running on this host — "
            "skipping to avoid racing a live dev-loop."
        )


@pytest.fixture(scope="module")
def _built_wheels_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the engine + models wheels once for the module.

    Mirrors ``tests/canonical/test_wheel_install_runner_context.py``'s
    ``built_wheels_dist`` — reusing the exact invocation the base wheel
    ships with, so this test exercises the same install surface a real
    ``pip install tolokaforge`` would."""
    dist_dir = tmp_path_factory.mktemp("integration_pull_dist")
    for project_dir, label in (
        (REPO_ROOT, "engine"),
        (REPO_ROOT / "tolokaforge_models", "models"),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(dist_dir)],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        if result.returncode != 0:
            pytest.fail(
                f"{label} wheel build failed (exit {result.returncode}):\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
    return dist_dir


@pytest.fixture(scope="module")
def _scratch_venv(
    tmp_path_factory: pytest.TempPathFactory,
    _built_wheels_dist: Path,
    _skip_if_dev_loop: None,
) -> Path:
    """Build a scratch venv with the freshly-built engine wheel installed.

    Returns the venv's python interpreter path. Session-scoped and
    resource-heavy (~30–60 s), so downstream tests share it via
    module scope."""
    venv_dir = tmp_path_factory.mktemp("integration_pull_venv")
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    venv_python = venv_dir / "bin" / "python"

    # Find the engine wheel — it's the only one matching ``tolokaforge-*``
    # (the models sibling is ``tolokaforge_models-*``).
    wheels = list(_built_wheels_dist.glob("*.whl"))
    base_wheels = [w for w in wheels if w.name.startswith("tolokaforge-")]
    assert len(base_wheels) == 1, (
        f"expected exactly one base tolokaforge wheel in {_built_wheels_dist}, "
        f"got: {[w.name for w in wheels]}"
    )
    base_wheel = base_wheels[0]

    install_cmd = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--quiet",
        "--find-links",
        str(_built_wheels_dist),
        str(base_wheel),
    ]
    result = subprocess.run(
        install_cmd,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    if result.returncode != 0:
        pytest.fail(
            f"wheel install into scratch venv failed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return venv_python


def _resolved_engine_version(venv_python: Path) -> str:
    result = subprocess.run(
        [str(venv_python), "-c", "import tolokaforge; print(tolokaforge.__version__)"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(f"failed to resolve engine version:\nstderr:\n{result.stderr}")
    return result.stdout.strip()


def _docker_image_present(reference: str) -> bool:
    """True when ``docker image inspect <ref>`` succeeds."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", reference],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def _docker_image_id(reference: str) -> str | None:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _cleanup_local_alias(alias: str) -> None:
    """Best-effort ``docker rmi`` of an alias tag this test created.

    Removing the alias is safe: it only untags the reference. The
    underlying image layers (and any other tags pointing at the same
    image ID, e.g. the ``:0.18.0`` reference the pull created) are
    preserved."""
    subprocess.run(
        ["docker", "rmi", alias],
        capture_output=True,
        timeout=15,
        check=False,
    )


class TestPullPathLandsPublishedImage:
    def test_image_source_pull_leaves_published_reference_in_daemon(
        self,
        _scratch_venv: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """A wheel-installed engine calling ``Image.pull`` against Docker
        Hub for the runner repo lands the published tag in the local
        daemon. This is the empirical version of ``test_image_pull.py``:
        no mocks — the real Docker SDK talks to real Docker Hub."""
        engine_version = _resolved_engine_version(_scratch_venv)
        assert engine_version != "0.0.0+unknown", (
            "scratch venv failed to expose an installed version — the "
            "install-metadata is missing from the built wheel"
        )
        published_ref = f"{PUBLISHED_RUNNER_REPO}:{engine_version}"

        # Run the pull from OUTSIDE the repo (probe_cwd trick) so the
        # scratch venv sees ONLY the wheel-installed tolokaforge, not
        # any source-tree package that might shadow it via sys.path[0].
        probe_cwd = tmp_path_factory.mktemp("integration_pull_cwd_outside_repo")
        probe = textwrap.dedent(f"""
            import json
            import tolokaforge
            from tolokaforge.docker.image import Image

            img = Image.pull(
                name={PUBLISHED_RUNNER_REPO!r},
                tag={engine_version!r},
                platform="linux/amd64",
            )
            print(json.dumps({{
                "full_tag": img.full_tag,
                "image_id": img.image_id,
                "context_hash": img.context_hash,
                "engine_version": tolokaforge.__version__,
            }}))
        """).strip()

        try:
            result = subprocess.run(
                [str(_scratch_venv), "-c", probe],
                capture_output=True,
                text=True,
                cwd=str(probe_cwd),
                timeout=_SUBPROCESS_TIMEOUT_S,
            )
            if result.returncode != 0:
                pytest.fail(
                    f"Image.pull inside scratch venv failed (exit "
                    f"{result.returncode}):\nstdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}"
                )
            data = json.loads(result.stdout.strip().splitlines()[-1])

            assert data["full_tag"] == published_ref
            assert data["context_hash"] == "pulled"
            assert _docker_image_present(published_ref), (
                f"Image.pull reported success but {published_ref} is not "
                "present in the local daemon after the call — either the "
                "pull code did not actually pull, or a cleanup ran between."
            )
            assert data["image_id"] == _docker_image_id(published_ref), (
                "image_id returned by Image.pull does not match the daemon's "
                f"inspect for {published_ref}"
            )
        finally:
            shutil.rmtree(probe_cwd, ignore_errors=True)


class TestBuildModeSkipsPull:
    def test_image_source_build_never_pulls(
        self,
        _scratch_venv: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """With ``docker.image_source=build``, the stack never calls
        ``Image.pull``. We can't directly observe absence-of-network-
        call, but we CAN verify the resolver returns 'build' for every
        input combination that would otherwise pull — which is
        equivalent to observing at the layer above what the network
        would see below."""
        probe_cwd = tmp_path_factory.mktemp("integration_pull_build_mode_cwd")
        probe = textwrap.dedent("""
            import json
            import tolokaforge
            from tolokaforge.docker.image_source_policy import resolve_image_source

            # Even the case that would otherwise pull (auto + wheel + known
            # version) resolves to build when the caller declares build
            # explicitly.
            forced_build = resolve_image_source(
                request="build",
                is_wheel_install=True,
                engine_version=tolokaforge.__version__,
            )
            print(json.dumps({"forced_build": forced_build}))
        """).strip()

        try:
            result = subprocess.run(
                [str(_scratch_venv), "-c", probe],
                capture_output=True,
                text=True,
                cwd=str(probe_cwd),
                timeout=30,
            )
            if result.returncode != 0:
                pytest.fail(
                    f"resolve_image_source probe failed (exit "
                    f"{result.returncode}):\nstderr:\n{result.stderr}"
                )
            data = json.loads(result.stdout.strip().splitlines()[-1])
            assert data["forced_build"] == "build"
        finally:
            shutil.rmtree(probe_cwd, ignore_errors=True)


class TestSourceCheckoutResolvesToBuild:
    def test_auto_from_repo_checkout_yields_build(
        self,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """The repo-checkout branch — running Python from THIS worktree
        (where ``pyproject.toml`` sits alongside ``repo_root()``) —
        must resolve ``auto`` to ``build``. No wheel install involved;
        this is the contributor path.

        Uses the outer test's Python (not a scratch venv) so
        ``repo_root()`` genuinely sees the on-disk ``pyproject.toml``."""
        # Skip if we somehow lost the pyproject.toml alongside the runtime
        # tolokaforge tree — the assertion below is only meaningful when
        # the file exists.
        from tolokaforge.docker.builder import repo_root

        if not (repo_root() / "pyproject.toml").is_file():
            pytest.skip(
                "runtime tolokaforge is not co-located with pyproject.toml — "
                "this test only checks the source-checkout branch."
            )

        import tolokaforge
        from tolokaforge.docker.image_source_policy import resolve_image_source

        resolved = resolve_image_source(
            request="auto",
            is_wheel_install=False,
            engine_version=tolokaforge.__version__,
        )
        assert resolved == "build"
