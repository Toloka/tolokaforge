"""Unit tests for ``resolve_image_source()``.

Pure-function tests — no Docker daemon, no filesystem, no mocks.
Covers all use-case matrix rows from #1068 (contributor, ad-hoc PyPI
user, arena / task consumer, air-gapped operator, CI, downstream OSS
consumer) that are expressible at this policy layer.
"""

from __future__ import annotations

import pytest

import tolokaforge
from tolokaforge.docker.image_source_policy import (
    UNKNOWN_VERSION_SENTINEL,
    RunnerDockerCliUnavailableError,
    check_runner_docker_cli_available,
    resolve_image_source,
)

pytestmark = pytest.mark.unit


class TestExplicitRequestWins:
    """``request in {"pull", "build"}`` is the escape hatch — never
    influenced by install shape or version."""

    @pytest.mark.parametrize("is_wheel_install", [True, False])
    @pytest.mark.parametrize(
        "engine_version", ["0.18.0", UNKNOWN_VERSION_SENTINEL, "0.18.0.dev5", "1.2.3+dirty"]
    )
    def test_pull_always_yields_pull(self, is_wheel_install: bool, engine_version: str) -> None:
        assert (
            resolve_image_source(
                request="pull",
                is_wheel_install=is_wheel_install,
                engine_version=engine_version,
            )
            == "pull"
        )

    @pytest.mark.parametrize("is_wheel_install", [True, False])
    @pytest.mark.parametrize(
        "engine_version", ["0.18.0", UNKNOWN_VERSION_SENTINEL, "0.18.0.dev5", "1.2.3+dirty"]
    )
    def test_build_always_yields_build(self, is_wheel_install: bool, engine_version: str) -> None:
        assert (
            resolve_image_source(
                request="build",
                is_wheel_install=is_wheel_install,
                engine_version=engine_version,
            )
            == "build"
        )


class TestAutoMode:
    """``request="auto"`` falls through to the shape-based decision."""

    def test_wheel_install_with_known_version_pulls(self) -> None:
        # Case 3 (ad-hoc PyPI user), 4 (arena), 7 (downstream OSS)
        assert (
            resolve_image_source(
                request="auto",
                is_wheel_install=True,
                engine_version="0.18.0",
            )
            == "pull"
        )

    def test_source_checkout_with_known_version_builds(self) -> None:
        # Case 1 (contributor), 6 (CI on a checkout)
        assert (
            resolve_image_source(
                request="auto",
                is_wheel_install=False,
                engine_version="0.18.0",
            )
            == "build"
        )

    def test_source_checkout_without_install_builds(self) -> None:
        # Contributor running from a bare checkout without pip install
        assert (
            resolve_image_source(
                request="auto",
                is_wheel_install=False,
                engine_version=UNKNOWN_VERSION_SENTINEL,
            )
            == "build"
        )

    def test_wheel_install_with_unknown_version_builds(self) -> None:
        # Defensive: an editable install could report the sentinel; auto
        # must not attempt to pull ``tolokasoft1/tolokaforge-runner:0.0.0+unknown``.
        assert (
            resolve_image_source(
                request="auto",
                is_wheel_install=True,
                engine_version=UNKNOWN_VERSION_SENTINEL,
            )
            == "build"
        )

    @pytest.mark.parametrize(
        "engine_version",
        ["0.18.0.dev5", "0.18.0.post1", "0.19.0.rc.1"],
    )
    def test_wheel_install_with_prerelease_version_still_attempts_pull(
        self, engine_version: str
    ) -> None:
        # Prerelease versions are legal Docker tag characters and might
        # be published — let the pull attempt be the authoritative check.
        assert (
            resolve_image_source(
                request="auto",
                is_wheel_install=True,
                engine_version=engine_version,
            )
            == "pull"
        )

    @pytest.mark.parametrize(
        "engine_version",
        ["0.18.0+dirty", "0.18.0+editable", "1.2.3+abcdef.dirty", "0.19.0+local"],
    )
    def test_wheel_install_with_pep440_local_version_falls_back_to_build(
        self, engine_version: str
    ) -> None:
        """PEP 440 local segment (``+something``) is not a legal Docker
        tag character. Route to build in ``auto`` mode so the caller
        never emits a malformed tag that the pull error handler would
        misclassify as ``unreachable``."""
        assert (
            resolve_image_source(
                request="auto",
                is_wheel_install=True,
                engine_version=engine_version,
            )
            == "build"
        )


class TestSentinelStaysInSyncWithPackage:
    """If someone renames the sentinel in ``tolokaforge/__init__.py``
    without updating ``image_source_policy.py``, ``auto`` mode would
    silently start pulling ``:0.0.0+unknown`` from Docker Hub. Pin
    them together so a drift fails CI here first."""

    def test_sentinel_matches_package_version_fallback(self) -> None:
        # We assert the SHAPE, not the exact string — the package sets
        # ``__version__`` to whatever ``importlib.metadata`` returns for
        # an installed distribution, but the sentinel path only runs when
        # metadata is missing. Verify that whenever ``__version__`` looks
        # like the sentinel, the policy considers the version unknown.
        if tolokaforge.__version__ == UNKNOWN_VERSION_SENTINEL:
            assert (
                resolve_image_source(
                    request="auto",
                    is_wheel_install=True,
                    engine_version=tolokaforge.__version__,
                )
                == "build"
            )
        else:
            # The reverse direction: the sentinel constant itself is the
            # only value the policy special-cases in auto+wheel mode. Any
            # other value → pull.
            assert (
                resolve_image_source(
                    request="auto",
                    is_wheel_install=True,
                    engine_version=tolokaforge.__version__,
                )
                == "pull"
            )


class TestRunnerDockerCliAvailabilityGate:
    """`check_runner_docker_cli_available` fails a run before any container
    work when the resolved image source would ship a runner without the
    docker CLI to a run that needs it."""

    def test_noop_when_docker_cli_not_needed(self) -> None:
        check_runner_docker_cli_available(
            needs_docker_cli=False,
            request="pull",
            is_wheel_install=True,
            engine_version="0.19.1",
        )

    def test_pull_wheel_install_raises(self) -> None:
        with pytest.raises(RunnerDockerCliUnavailableError) as exc:
            check_runner_docker_cli_available(
                needs_docker_cli=True,
                request="pull",
                is_wheel_install=True,
                engine_version="0.19.1",
            )
        msg = str(exc.value)
        assert "docker CLI" in msg
        assert "--image-source build" in msg
        assert "docker.image_source: build" in msg
        assert "TOLOKAFORGE_IMAGE_SOURCE=build" in msg

    def test_auto_wheel_install_pinned_version_raises(self) -> None:
        # This is the exact operator-install shape that misfires by default:
        # pip-installed engine + auto policy + real version → resolves to
        # 'pull' → runner ships without docker CLI.
        with pytest.raises(RunnerDockerCliUnavailableError):
            check_runner_docker_cli_available(
                needs_docker_cli=True,
                request="auto",
                is_wheel_install=True,
                engine_version="0.19.1",
            )

    @pytest.mark.parametrize(
        "request_, is_wheel_install, engine_version",
        [
            # Explicit build always yields build.
            ("build", True, "0.19.1"),
            # Source-checkout contributor: auto resolves to build.
            ("auto", False, "0.19.1"),
            # No valid pull tag → auto falls through to build.
            ("auto", True, UNKNOWN_VERSION_SENTINEL),
            # PEP 440 local segment (`+dirty`) → auto falls through to build.
            ("auto", True, "0.19.1+dirty"),
        ],
    )
    def test_gate_passes_when_resolved_source_is_build(
        self,
        request_: str,
        is_wheel_install: bool,
        engine_version: str,
    ) -> None:
        check_runner_docker_cli_available(
            needs_docker_cli=True,
            request=request_,
            is_wheel_install=is_wheel_install,
            engine_version=engine_version,
        )
