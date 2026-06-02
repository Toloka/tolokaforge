"""Unit tests for the wheel-based Docker provisioning resolver.

All tests are synthetic — no Docker daemon, no network, no real wheel builds.
"""

from __future__ import annotations

import subprocess
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
    _pip_wheel_cache_bases,
    _read_pyproject_version,
    _uv_cache_bases,
    _uv_cache_dir_from_cli,
    _walk_pip_wheel_caches,
    _wheel_matches_version,
    resolve_wheel,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_uv_cache_dir_cli_cache():
    """`_uv_cache_dir_from_cli` is lru_cached — keep tests independent."""
    _uv_cache_dir_from_cli.cache_clear()
    yield
    _uv_cache_dir_from_cli.cache_clear()


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
# Cache discovery — _uv_cache_dir_from_cli + _walk_pip_wheel_caches
# ===================================================================


def _write_uv_wheel(uv_root: Path, *, version: str = "0.3.0", editable: bool = False) -> Path:
    """Create a wheel under a uv-style ``sdists-v0`` layout inside *uv_root*."""
    sub = uv_root / "sdists-v0" / "tolokaforge"
    if editable:
        sub = sub / "editable"
    sub.mkdir(parents=True, exist_ok=True)
    whl = sub / f"tolokaforge-{version}-py3-none-any.whl"
    whl.write_bytes(b"PK\x03\x04test")
    return whl


def _write_pip_wheel(pip_root: Path, *, version: str = "0.3.0") -> Path:
    """Create a wheel under ``<pip_root>/wheels/...``."""
    sub = pip_root / "wheels" / "ab" / "cd"
    sub.mkdir(parents=True, exist_ok=True)
    whl = sub / f"tolokaforge-{version}-py3-none-any.whl"
    whl.write_bytes(b"PK\x03\x04test")
    return whl


class TestUvCacheDirFromCli:
    def test_returns_path_from_uv(self, tmp_path: Path):
        root = tmp_path / "uvcache"
        with (
            patch("tolokaforge.docker.wheel_resolver.shutil.which", return_value="/usr/bin/uv"),
            patch(
                "tolokaforge.docker.wheel_resolver.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["uv", "cache", "dir"], returncode=0, stdout=f"{root}\n", stderr=""
                ),
            ),
        ):
            assert _uv_cache_dir_from_cli() == root

    def test_uv_absent_returns_none(self):
        with patch("tolokaforge.docker.wheel_resolver.shutil.which", return_value=None):
            assert _uv_cache_dir_from_cli() is None

    def test_nonzero_exit_returns_none(self):
        with (
            patch("tolokaforge.docker.wheel_resolver.shutil.which", return_value="/usr/bin/uv"),
            patch(
                "tolokaforge.docker.wheel_resolver.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["uv", "cache", "dir"], returncode=1, stdout="", stderr="boom"
                ),
            ),
        ):
            assert _uv_cache_dir_from_cli() is None

    def test_subprocess_error_returns_none(self):
        with (
            patch("tolokaforge.docker.wheel_resolver.shutil.which", return_value="/usr/bin/uv"),
            patch(
                "tolokaforge.docker.wheel_resolver.subprocess.run",
                side_effect=OSError("nope"),
            ),
        ):
            assert _uv_cache_dir_from_cli() is None


class TestUvCacheBasesPrecedence:
    """Lock the precedence: `uv cache dir` (authoritative) > UV_CACHE_DIR env > default.

    The full matrix of {uv present / uv unavailable} x {UV_CACHE_DIR set / unset}.
    Behavior must be equivalent to the pre-tweak version in every cell (env and
    `uv cache dir` never disagree when both are available, because `uv cache dir`
    itself honors UV_CACHE_DIR).
    """

    @pytest.fixture(autouse=True)
    def _home(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        self.default = home / ".cache" / "uv"
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)

    def _set_cli(self, monkeypatch, value):
        monkeypatch.setattr(
            "tolokaforge.docker.wheel_resolver._uv_cache_dir_from_cli", lambda: value
        )

    def test_cli_present_env_unset(self, tmp_path, monkeypatch):
        x = tmp_path / "x"
        self._set_cli(monkeypatch, x)
        assert _uv_cache_bases() == [x, self.default]

    def test_cli_wins_over_env(self, tmp_path, monkeypatch):
        # uv is authoritative; when present, the UV_CACHE_DIR env value is NOT
        # added as a separate root (uv cache dir already reflects it).
        x = tmp_path / "x"
        y = tmp_path / "y"
        self._set_cli(monkeypatch, x)
        monkeypatch.setenv("UV_CACHE_DIR", str(y))
        bases = _uv_cache_bases()
        assert bases == [x, self.default]
        assert y not in bases

    def test_env_fallback_when_cli_unavailable(self, tmp_path, monkeypatch):
        y = tmp_path / "y"
        self._set_cli(monkeypatch, None)
        monkeypatch.setenv("UV_CACHE_DIR", str(y))
        assert _uv_cache_bases() == [y, self.default]

    def test_default_only_when_nothing_available(self, monkeypatch):
        self._set_cli(monkeypatch, None)
        assert _uv_cache_bases() == [self.default]

    def test_cli_equals_env_dedups(self, tmp_path, monkeypatch):
        x = tmp_path / "x"
        self._set_cli(monkeypatch, x)
        monkeypatch.setenv("UV_CACHE_DIR", str(x))
        assert _uv_cache_bases() == [x, self.default]


class TestPipWheelCacheBases:
    @pytest.fixture(autouse=True)
    def _home(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        self.default = home / ".cache" / "pip" / "wheels"
        monkeypatch.delenv("PIP_CACHE_DIR", raising=False)

    def test_default_only(self):
        assert _pip_wheel_cache_bases() == [self.default]

    def test_env_then_default(self, tmp_path, monkeypatch):
        p = tmp_path / "pipcache"
        monkeypatch.setenv("PIP_CACHE_DIR", str(p))
        assert _pip_wheel_cache_bases() == [p / "wheels", self.default]


class TestWalkPipWheelCaches:
    """The walk must find the wheel wherever the cache actually lives."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        # Baseline: no env overrides, no uv CLI, empty HOME. Each test opts in.
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
        monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
        monkeypatch.setattr(
            "tolokaforge.docker.wheel_resolver._uv_cache_dir_from_cli",
            lambda: None,
        )
        empty_home = tmp_path / "home"
        empty_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: empty_home)

    def test_uv_cache_dir_env_honored(self, tmp_path, monkeypatch):
        root = tmp_path / "relocated-uv"
        whl = _write_uv_wheel(root)
        monkeypatch.setenv("UV_CACHE_DIR", str(root))
        assert whl in _walk_pip_wheel_caches()

    def test_uv_cache_dir_from_cli_honored(self, tmp_path, monkeypatch):
        root = tmp_path / "cli-uv"
        whl = _write_uv_wheel(root)
        monkeypatch.setattr(
            "tolokaforge.docker.wheel_resolver._uv_cache_dir_from_cli",
            lambda: root,
        )
        assert whl in _walk_pip_wheel_caches()

    def test_pip_cache_dir_env_honored(self, tmp_path, monkeypatch):
        root = tmp_path / "relocated-pip"
        whl = _write_pip_wheel(root)
        monkeypatch.setenv("PIP_CACHE_DIR", str(root))
        assert whl in _walk_pip_wheel_caches()

    def test_default_home_uv_cache(self, tmp_path, monkeypatch):
        home = tmp_path / "home2"
        whl = _write_uv_wheel(home / ".cache" / "uv")
        monkeypatch.setattr(Path, "home", lambda: home)
        assert whl in _walk_pip_wheel_caches()

    def test_editable_wheels_skipped(self, tmp_path, monkeypatch):
        root = tmp_path / "uv-editable"
        whl = _write_uv_wheel(root, editable=True)
        monkeypatch.setenv("UV_CACHE_DIR", str(root))
        assert whl not in _walk_pip_wheel_caches()

    def test_dedup_when_env_and_cli_overlap(self, tmp_path, monkeypatch):
        root = tmp_path / "shared-uv"
        whl = _write_uv_wheel(root)
        monkeypatch.setenv("UV_CACHE_DIR", str(root))
        monkeypatch.setattr(
            "tolokaforge.docker.wheel_resolver._uv_cache_dir_from_cli",
            lambda: root,
        )
        results = _walk_pip_wheel_caches()
        assert results.count(whl) == 1

    def test_cli_location_searched_even_when_env_points_elsewhere(self, tmp_path, monkeypatch):
        # Wheel lives only in the authoritative (uv cache dir) location; the
        # UV_CACHE_DIR env points at a different, empty dir. The walk must still
        # find it because uv-cache-dir wins.
        cli_root = tmp_path / "cli"
        env_root = tmp_path / "env"
        whl = _write_uv_wheel(cli_root)
        env_root.mkdir()
        monkeypatch.setenv("UV_CACHE_DIR", str(env_root))
        monkeypatch.setattr(
            "tolokaforge.docker.wheel_resolver._uv_cache_dir_from_cli", lambda: cli_root
        )
        assert whl in _walk_pip_wheel_caches()


class TestScenarioCResolution:
    """End-to-end resolver behavior for a git install with a relocated uv cache.

    Reproduces issue #27 at the `WheelResolver.resolve()` level: for a git
    install, `local-source` has no tree and `pip-download` skips git installs,
    so `pip-cache` is the only viable provider. If the relocated cache isn't
    discovered, every provider misses -> NoWheelError (the bug). Once
    `UV_CACHE_DIR` advertises the cache, `pip-cache` finds the wheel (the fix).
    """

    def _git_install(self):
        """Patches that make the engine look git-installed with no source tree."""
        return (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._find_engine_source_root",
                return_value=None,
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value={"vcs_info": {"vcs": "git"}},
            ),
        )

    def test_relocated_cache_advertised_resolves_via_pip_cache(
        self, tmp_path, monkeypatch, cache_dir
    ):
        uv_root = tmp_path / "setup-uv-cache"
        _write_uv_wheel(uv_root, version="0.3.0")
        monkeypatch.setenv("UV_CACHE_DIR", str(uv_root))
        monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
        monkeypatch.setattr(
            "tolokaforge.docker.wheel_resolver._uv_cache_dir_from_cli", lambda: None
        )
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: empty_home)

        p1, p2, p3 = self._git_install()
        with p1, p2, p3:
            artifact = WheelResolver().resolve(cache_dir)

        assert artifact.version == "0.3.0"
        assert artifact.provider_name == "pip-cache"

    def test_relocated_cache_not_advertised_raises(self, tmp_path, monkeypatch, cache_dir):
        # The wheel exists only in a cache we never advertise (env unset, no CLI,
        # empty HOME) — this is the pre-fix failure mode.
        hidden = tmp_path / "hidden-cache"
        _write_uv_wheel(hidden, version="0.3.0")
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
        monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
        monkeypatch.setattr(
            "tolokaforge.docker.wheel_resolver._uv_cache_dir_from_cli", lambda: None
        )
        empty_home = tmp_path / "empty-home2"
        empty_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: empty_home)

        p1, p2, p3 = self._git_install()
        with p1, p2, p3, pytest.raises(NoWheelError, match="UV_CACHE_DIR"):
            WheelResolver().resolve(cache_dir)


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
