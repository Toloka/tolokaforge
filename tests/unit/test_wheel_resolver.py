"""Unit tests for the wheel-based Docker provisioning resolver.

All tests are synthetic — no Docker daemon, no network, no real wheel builds.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from tolokaforge.docker.wheel_resolver import (
    LocalSourceWheelProvider,
    NoWheelError,
    PipCacheWheelProvider,
    PipDownloadWheelProvider,
    WheelArtifact,
    WheelProvider,
    WheelResolver,
    _hash_file,
    _is_engine_pyproject,
    _read_pyproject_version,
    _wheel_matches_version,
    resolve_wheel,
)

pytestmark = pytest.mark.unit


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture()
def engine_root(tmp_path: Path) -> Path:
    """A minimal engine checkout with pyproject.toml + a Python file."""
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"

            [project]
            name = "tolokaforge"
            version = "0.3.0"
        """
        ),
    )
    pkg = tmp_path / "tolokaforge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '0.3.0'\n")
    (pkg / "core.py").write_text("# core module\n")
    return tmp_path


@pytest.fixture()
def tasks_root(tmp_path: Path) -> Path:
    """A checkout that looks like tolokaforge-tasks (not the engine)."""
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "tolokaforge-tasks"
            version = "0.1.0"
        """
        ),
    )
    return tmp_path


@pytest.fixture()
def fake_wheel(tmp_path: Path) -> Path:
    """A dummy wheel file for testing cache operations."""
    whl = tmp_path / "tolokaforge-0.3.0-py3-none-any.whl"
    whl.write_bytes(b"PK\x03\x04fake-wheel-content-for-test")
    return whl


@pytest.fixture()
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "wheel-cache"
    d.mkdir()
    return d


# ===================================================================
# pyproject.toml helpers
# ===================================================================


class TestPyprojectHelpers:
    def test_engine_pyproject_detected(self, engine_root: Path):
        assert _is_engine_pyproject(engine_root / "pyproject.toml")

    def test_tasks_pyproject_rejected(self, tasks_root: Path):
        assert not _is_engine_pyproject(tasks_root / "pyproject.toml")

    def test_missing_file(self, tmp_path: Path):
        assert not _is_engine_pyproject(tmp_path / "no.toml")

    def test_version_extraction(self, engine_root: Path):
        assert _read_pyproject_version(engine_root) == "0.3.0"

    def test_version_missing(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert _read_pyproject_version(tmp_path) is None


# ===================================================================
# Wheel filename matching
# ===================================================================


class TestWheelMatching:
    def test_exact_version_match(self, fake_wheel: Path):
        assert _wheel_matches_version(fake_wheel, "0.3.0")

    def test_version_mismatch(self, fake_wheel: Path):
        assert not _wheel_matches_version(fake_wheel, "0.2.0")

    def test_malformed_name(self, tmp_path: Path):
        bad = tmp_path / "not-a-wheel.whl"
        bad.touch()
        assert not _wheel_matches_version(bad, "0.3.0")


# ===================================================================
# LocalSourceWheelProvider
# ===================================================================


class TestLocalSourceWheelProvider:
    def test_builds_wheel_from_source(
        self,
        engine_root: Path,
        cache_dir: Path,
    ):
        """Detects a source checkout and 'builds' a wheel."""
        provider = LocalSourceWheelProvider()

        # Patch __file__ so the provider finds our fake engine root.
        fake_module = engine_root / "tolokaforge" / "docker" / "wh.py"
        fake_module.parent.mkdir(parents=True, exist_ok=True)
        fake_module.touch()

        # Patch the build step to just create a dummy wheel.
        # _build_wheel is a @staticmethod; patch.object replaces it
        # with a regular function, so accept *args to be safe.
        def fake_build(*args):
            # Last arg is out_dir, second-to-last is source_root.
            cache_d = args[-1]
            whl = Path(cache_d) / "tolokaforge-0.3.0-py3-none-any.whl"
            whl.write_bytes(b"PK\x03\x04dummy")

        with (
            patch(
                "tolokaforge.docker.wheel_resolver.__file__",
                str(fake_module),
            ),
            patch.object(
                LocalSourceWheelProvider,
                "_build_wheel",
                fake_build,
            ),
        ):
            artifact = provider.provide(cache_dir)

        assert artifact is not None
        assert artifact.version == "0.3.0"
        assert artifact.provider_name == "local-source"
        assert artifact.path.exists()
        assert artifact.path.suffix == ".whl"

    def test_cache_hit_skips_build(
        self,
        engine_root: Path,
        cache_dir: Path,
    ):
        """When a cached wheel with matching hash exists, skip the build."""
        provider = LocalSourceWheelProvider()

        fake_module = engine_root / "tolokaforge" / "docker" / "wh.py"
        fake_module.parent.mkdir(parents=True, exist_ok=True)
        fake_module.touch()

        build_calls = []

        def tracked_build(*args):
            build_calls.append(1)
            cache_d = args[-1]
            whl = Path(cache_d) / "tolokaforge-0.3.0-py3-none-any.whl"
            whl.write_bytes(b"PK\x03\x04dummy")

        with (
            patch(
                "tolokaforge.docker.wheel_resolver.__file__",
                str(fake_module),
            ),
            patch.object(
                LocalSourceWheelProvider,
                "_build_wheel",
                tracked_build,
            ),
        ):
            art1 = provider.provide(cache_dir)
            art2 = provider.provide(cache_dir)

        assert len(build_calls) == 1  # second call is a cache hit
        assert art1 is not None
        assert art2 is not None
        assert art1.content_hash == art2.content_hash

    def test_no_source_yields_none(self, cache_dir: Path):
        """When no source checkout is found, return None."""
        provider = LocalSourceWheelProvider()
        with patch(
            "tolokaforge.docker.wheel_resolver.__file__",
            "/nonexistent/tolokaforge/docker/wh.py",
        ):
            assert provider.provide(cache_dir) is None

    def test_tasks_checkout_yields_none(
        self,
        tasks_root: Path,
        cache_dir: Path,
    ):
        """When the checkout is tolokaforge-tasks, return None."""
        provider = LocalSourceWheelProvider()
        fake_module = tasks_root / "tolokaforge" / "docker" / "wh.py"
        fake_module.parent.mkdir(parents=True, exist_ok=True)
        fake_module.touch()
        with patch(
            "tolokaforge.docker.wheel_resolver.__file__",
            str(fake_module),
        ):
            assert provider.provide(cache_dir) is None


# ===================================================================
# PipCacheWheelProvider
# ===================================================================


class TestPipCacheWheelProvider:
    def test_finds_cached_wheel(
        self,
        fake_wheel: Path,
        cache_dir: Path,
    ):
        """Locates a matching wheel in the pip/uv cache."""
        provider = PipCacheWheelProvider()
        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._walk_pip_wheel_caches",
                return_value=[fake_wheel],
            ),
        ):
            artifact = provider.provide(cache_dir)

        assert artifact is not None
        assert artifact.version == "0.3.0"
        assert artifact.provider_name == "pip-cache"
        # The wheel should be copied into cache_dir.
        assert artifact.path.parent == cache_dir

    def test_version_mismatch_skipped(
        self,
        fake_wheel: Path,
        cache_dir: Path,
    ):
        """Wheel with wrong version is skipped."""
        provider = PipCacheWheelProvider()
        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.2.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._walk_pip_wheel_caches",
                return_value=[fake_wheel],
            ),
        ):
            assert provider.provide(cache_dir) is None

    def test_empty_cache_yields_none(self, cache_dir: Path):
        provider = PipCacheWheelProvider()
        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._walk_pip_wheel_caches",
                return_value=[],
            ),
        ):
            assert provider.provide(cache_dir) is None

    def test_not_installed_yields_none(self, cache_dir: Path):
        provider = PipCacheWheelProvider()
        with patch(
            "tolokaforge.docker.wheel_resolver._installed_version",
            return_value=None,
        ):
            assert provider.provide(cache_dir) is None


# ===================================================================
# PipDownloadWheelProvider
# ===================================================================


class TestPipDownloadWheelProvider:
    def test_downloads_for_pypi_install(self, cache_dir: Path):
        """For a PyPI install, downloads the matching wheel."""
        provider = PipDownloadWheelProvider()

        # Simulate: pip download creates a wheel file.
        def fake_check_call(cmd, **kwargs):
            dest = cmd[cmd.index("--dest") + 1]
            whl = Path(dest) / "tolokaforge-0.2.0-py3-none-any.whl"
            whl.write_bytes(b"PK\x03\x04pypi-wheel")

        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.2.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=None,
            ),
            patch(
                "tolokaforge.docker.wheel_resolver.subprocess.check_call",
                side_effect=fake_check_call,
            ),
        ):
            artifact = provider.provide(cache_dir)

        assert artifact is not None
        assert artifact.version == "0.2.0"
        assert artifact.provider_name == "pip-download"

    def test_git_install_skipped(self, cache_dir: Path):
        """Git installs don't have a matching PyPI release; should skip."""
        provider = PipDownloadWheelProvider()
        direct_url = {
            "url": "https://github.com/Toloka/tolokaforge.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc123"},
        }
        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=direct_url,
            ),
        ):
            assert provider.provide(cache_dir) is None

    def test_not_installed_yields_none(self, cache_dir: Path):
        provider = PipDownloadWheelProvider()
        with patch(
            "tolokaforge.docker.wheel_resolver._installed_version",
            return_value=None,
        ):
            assert provider.provide(cache_dir) is None


# ===================================================================
# WheelResolver chain
# ===================================================================


class _AlwaysNoneProvider(WheelProvider):
    name = "always-none"
    priority = 50

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        return None


class _AlwaysSucceedProvider(WheelProvider):
    name = "always-succeed"
    priority = 25

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        whl = cache_dir / "tolokaforge-test-py3-none-any.whl"
        whl.write_bytes(b"PK\x03\x04test")
        return WheelArtifact(
            path=whl,
            version="test",
            content_hash="testhash",
            provider_name=self.name,
        )


class _HighPriProvider(WheelProvider):
    name = "high-pri"
    priority = 1

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        whl = cache_dir / "tolokaforge-hi-py3-none-any.whl"
        whl.write_bytes(b"PK\x03\x04hi")
        return WheelArtifact(
            path=whl,
            version="hi",
            content_hash="hihash",
            provider_name=self.name,
        )


class TestWheelResolver:
    def test_priority_ordering(self, cache_dir: Path):
        """Lower priority value wins."""
        chain = WheelResolver(
            [
                _AlwaysSucceedProvider(),
                _HighPriProvider(),
            ]
        )
        art = chain.resolve(cache_dir)
        assert art.provider_name == "high-pri"

    def test_falls_through_none(self, cache_dir: Path):
        chain = WheelResolver(
            [
                _AlwaysNoneProvider(),
                _AlwaysSucceedProvider(),
            ]
        )
        art = chain.resolve(cache_dir)
        assert art.provider_name == "always-succeed"

    def test_all_none_raises(self, cache_dir: Path):
        chain = WheelResolver([_AlwaysNoneProvider()])
        with pytest.raises(NoWheelError, match="No provider"):
            chain.resolve(cache_dir)

    def test_empty_chain_raises(self, cache_dir: Path):
        chain = WheelResolver([])
        with pytest.raises(NoWheelError):
            chain.resolve(cache_dir)

    def test_register_inserts_and_resorts(self, cache_dir: Path):
        chain = WheelResolver([_AlwaysNoneProvider()])
        chain.register(_HighPriProvider())
        art = chain.resolve(cache_dir)
        assert art.provider_name == "high-pri"

    def test_register_returns_self(self):
        chain = WheelResolver([])
        assert chain.register(_AlwaysSucceedProvider()) is chain

    def test_default_chain_providers(self):
        chain = WheelResolver()
        names = [p.name for p in chain._providers]
        assert "local-source" in names
        assert "pip-cache" in names
        assert "pip-download" in names
        # local-source should come first.
        assert names.index("local-source") < names.index("pip-cache")
        assert names.index("pip-cache") < names.index("pip-download")


# ===================================================================
# WheelArtifact
# ===================================================================


class TestWheelArtifact:
    def test_frozen(self, fake_wheel: Path):
        art = WheelArtifact(
            path=fake_wheel,
            version="0.3.0",
            content_hash="abc",
            provider_name="test",
        )
        with pytest.raises(AttributeError):
            art.version = "0.4.0"  # type: ignore[misc]

    def test_hash_file(self, fake_wheel: Path):
        h = _hash_file(fake_wheel)
        assert len(h) == 64  # SHA-256 hex digest


# ===================================================================
# Module-level resolve_wheel() with caching
# ===================================================================


class TestModuleResolve:
    def test_caches_result(self, cache_dir: Path):
        import tolokaforge.docker.wheel_resolver as mod

        mod._cached_artifact = None
        fake = WheelArtifact(
            path=cache_dir / "test.whl",
            version="1.0",
            content_hash="abc",
            provider_name="test",
        )
        with patch(
            "tolokaforge.docker.wheel_resolver.WheelResolver.resolve",
            return_value=fake,
        ) as mock_resolve:
            first = resolve_wheel(cache_dir)
            second = resolve_wheel(cache_dir)
        assert first is second
        assert mock_resolve.call_count == 1
        mod._cached_artifact = None  # cleanup

    def test_force_refresh(self, cache_dir: Path):
        import tolokaforge.docker.wheel_resolver as mod

        fake1 = WheelArtifact(
            path=cache_dir / "a.whl",
            version="1",
            content_hash="a",
            provider_name="first",
        )
        fake2 = WheelArtifact(
            path=cache_dir / "b.whl",
            version="2",
            content_hash="b",
            provider_name="second",
        )
        mod._cached_artifact = fake1
        with patch(
            "tolokaforge.docker.wheel_resolver.WheelResolver.resolve",
            return_value=fake2,
        ):
            result = resolve_wheel(cache_dir, force_refresh=True)
        assert result.provider_name == "second"
        mod._cached_artifact = None  # cleanup
