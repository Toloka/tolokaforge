"""Unit tests for the wheel-based Docker provisioning resolver.

All tests are synthetic — no Docker daemon, no network, no real wheel builds.
"""

from __future__ import annotations

import os
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
    ReinstallWheelProvider,
    WheelArtifact,
    WheelProvider,
    WheelResolver,
    _hash_file,
    _is_engine_pyproject,
    _looks_like_sha,
    _newest_whl_for_version,
    _pip_wheel_cache_bases,
    _read_pyproject_version,
    _run,
    _run_pip,
    _run_uv,
    _stderr_tail,
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


class TestNewestWhlForVersion:
    """The version-matching selector must not return a stale wheel of a
    different version, even if that stale wheel is newer on disk."""

    def test_returns_matching_version(self, tmp_path: Path):
        old = tmp_path / "tolokaforge-0.2.0-py3-none-any.whl"
        old.write_bytes(b"old")
        new = tmp_path / "tolokaforge-0.3.0-py3-none-any.whl"
        new.write_bytes(b"new")
        assert _newest_whl_for_version(tmp_path, "tolokaforge", "0.3.0") == new

    def test_prefers_matching_version_over_newer_mtime(self, tmp_path: Path):
        """A newer stale wheel of the wrong version must NOT win by mtime."""
        target = tmp_path / "tolokaforge-0.3.0-py3-none-any.whl"
        target.write_bytes(b"target")
        # Stale wheel with a newer mtime.
        stale = tmp_path / "tolokaforge-0.5.0-py3-none-any.whl"
        stale.write_bytes(b"stale")
        os.utime(stale, (target.stat().st_mtime + 100, target.stat().st_mtime + 100))
        assert _newest_whl_for_version(tmp_path, "tolokaforge", "0.3.0") == target

    def test_returns_none_when_no_match(self, tmp_path: Path):
        (tmp_path / "tolokaforge-0.5.0-py3-none-any.whl").write_bytes(b"x")
        assert _newest_whl_for_version(tmp_path, "tolokaforge", "0.3.0") is None


class TestLooksLikeSha:
    """Detects hex-SHA commit ids so _materialize_git can skip the
    unsupported `git clone --branch <SHA>` shallow path."""

    @pytest.mark.parametrize(
        "commit",
        ["abc1234", "1234567890abcdef", "a" * 40, "0" * 40, "deadbeef"],
    )
    def test_hex_shas_recognized(self, commit: str):
        assert _looks_like_sha(commit)

    @pytest.mark.parametrize(
        "value",
        ["main", "v0.7.0", "release/2026-Q1", "abc123z", "abc", "a" * 41, ""],
    )
    def test_non_shas_rejected(self, value: str):
        assert not _looks_like_sha(value)


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
        # `_build_wheel` is an instance method; patch.object replaces it
        # with `fake_build`, so the call receives (self, source_root, out_dir).
        def fake_build(self, source_root, out_dir):
            whl = Path(out_dir) / "tolokaforge-0.3.0-py3-none-any.whl"
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

        def tracked_build(self, source_root, out_dir):
            build_calls.append(1)
            whl = Path(out_dir) / "tolokaforge-0.3.0-py3-none-any.whl"
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
        assert "no engine source tree" in provider.last_failure

    def test_build_wheel_calls_uv_with_correct_args(self, engine_root: Path, cache_dir: Path):
        """Direct call to _build_wheel (real signature) — catches staticmethod/
        self-binding regressions the patch-based tests miss."""
        provider = LocalSourceWheelProvider()
        captured: list[list[str]] = []

        def fake_run_uv(args, timeout=300):
            captured.append(args)
            return True, ""

        with patch(
            "tolokaforge.docker.wheel_resolver._run_uv",
            side_effect=fake_run_uv,
        ):
            provider._build_wheel(engine_root, cache_dir)

        assert len(captured) == 1
        assert captured[0][:2] == ["build", "--wheel"]
        assert str(cache_dir) in captured[0]
        assert str(engine_root) in captured[0]

    def test_build_wheel_falls_back_to_pip_when_uv_fails(self, engine_root: Path, cache_dir: Path):
        """uv build failure surfaces via pip wheel fallback."""
        provider = LocalSourceWheelProvider()
        with (
            patch(
                "tolokaforge.docker.wheel_resolver._run_uv",
                return_value=(False, "uv not on PATH"),
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._run_pip",
                return_value=(True, ""),
            ) as mock_pip,
        ):
            provider._build_wheel(engine_root, cache_dir)
        assert mock_pip.call_count == 1
        assert provider.last_failure == ""

    def test_build_wheel_records_both_failures(self, engine_root: Path, cache_dir: Path):
        """When BOTH uv and pip fail, last_failure carries both stderrs."""
        provider = LocalSourceWheelProvider()
        with (
            patch(
                "tolokaforge.docker.wheel_resolver._run_uv",
                return_value=(False, "uv not on PATH"),
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._run_pip",
                return_value=(False, "pip build failed: missing hatchling"),
            ),
        ):
            provider._build_wheel(engine_root, cache_dir)
        assert "uv not on PATH" in provider.last_failure
        assert "missing hatchling" in provider.last_failure

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
    """Precedence: ``UV_CACHE_DIR`` env → ``uv cache dir`` CLI → default (all searched).

    The env var is the most explicit signal a user (or CI action such as
    ``astral-sh/setup-uv``) can send, so it comes first. All three
    candidates are deduplicated into the search list rather than
    short-circuiting on the first — a wheel present in any of them wins.
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

    def test_env_precedes_cli(self, tmp_path, monkeypatch):
        # UV_CACHE_DIR env comes FIRST (most explicit signal); CLI output
        # comes next; both are searched, so a wheel in either wins.
        x = tmp_path / "x"
        y = tmp_path / "y"
        self._set_cli(monkeypatch, x)
        monkeypatch.setenv("UV_CACHE_DIR", str(y))
        bases = _uv_cache_bases()
        assert bases == [y, x, self.default]

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

    def test_all_three_distinct_all_searched(self, tmp_path, monkeypatch):
        # Env, CLI, and default are three different paths — all three must
        # be in the search list so a wheel in any of them can win.
        env = tmp_path / "env"
        cli = tmp_path / "cli"
        self._set_cli(monkeypatch, cli)
        monkeypatch.setenv("UV_CACHE_DIR", str(env))
        assert _uv_cache_bases() == [env, cli, self.default]


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


class TestGitInstallRelocatedCacheResolution:
    """End-to-end resolver behavior for a git install with a relocated uv cache.

    Reproduces issue #27 at the `WheelResolver.resolve()` level: for a git
    install, `local-source` has no tree and `pip-download` skips git installs,
    so `pip-cache` is the only viable provider. If the relocated cache isn't
    discovered, every provider misses -> NoWheelError (the bug). Once
    the cache is discovered, `pip-cache` finds the wheel (the fix).
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
# _run / _run_pip / _run_uv helpers — subprocess wrappers with stderr surfacing
# ===================================================================


class TestStderrTail:
    def test_short_text_unchanged(self):
        assert _stderr_tail("short") == "short"

    def test_empty_text(self):
        assert _stderr_tail("") == ""
        assert _stderr_tail("   \n  ") == ""

    def test_long_text_truncated_with_ellipsis(self):
        long = "x" * 5000
        result = _stderr_tail(long)
        assert result.startswith("…")
        assert len(result) <= 801  # _STDERR_TAIL + the ellipsis char


class TestRunHelper:
    def test_success_returns_ok(self):
        # `python -c "pass"` — a portable no-op that always exists.
        import sys as _sys

        ok, err = _run([_sys.executable, "-c", "pass"])
        assert ok
        assert err == ""

    def test_missing_binary_returns_false_with_path_error(self):
        ok, err = _run(["/nonexistent/binary/xyz"])
        assert not ok
        assert "not on PATH" in err or "No such file" in err or "failed" in err

    def test_nonzero_exit_captures_stderr(self):
        import sys as _sys

        ok, err = _run([_sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
        assert not ok
        assert "boom" in err


class TestRunPipEnsurepipRecovery:
    """The load-bearing fix for issue #13: uv envs omit pip → ensurepip retry."""

    def test_pip_available_first_try(self):
        with patch(
            "tolokaforge.docker.wheel_resolver._run",
            return_value=(True, ""),
        ) as mock_run:
            ok, err = _run_pip(["download", "tolokaforge==0.1.0"])
        assert ok
        assert err == ""
        assert mock_run.call_count == 1  # no ensurepip needed

    def test_missing_pip_triggers_ensurepip_then_retry(self):
        # First call: pip missing. Second: ensurepip succeeds.
        # Third: pip retry succeeds.
        results = iter(
            [
                (False, "No module named pip"),
                (True, ""),  # ensurepip
                (True, ""),  # pip retry
            ]
        )
        with patch(
            "tolokaforge.docker.wheel_resolver._run",
            side_effect=lambda *a, **k: next(results),
        ) as mock_run:
            ok, err = _run_pip(["download", "tolokaforge==0.1.0"])
        assert ok
        assert err == ""
        assert mock_run.call_count == 3
        # Middle call must be ensurepip.
        second_argv = mock_run.call_args_list[1].args[0]
        assert second_argv[-2:] == ["-m", "ensurepip"] or "ensurepip" in second_argv

    def test_ensurepip_failure_surfaces_reason(self):
        results = iter(
            [
                (False, "No module named pip"),
                (False, "ensurepip is disabled in this environment"),
            ]
        )
        with patch(
            "tolokaforge.docker.wheel_resolver._run",
            side_effect=lambda *a, **k: next(results),
        ):
            ok, err = _run_pip(["download", "tolokaforge==0.1.0"])
        assert not ok
        assert "pip missing" in err
        assert "ensurepip failed" in err
        assert "disabled" in err

    def test_non_missing_pip_error_returned_immediately(self):
        # A generic pip error (network 404 etc.) must NOT trigger ensurepip.
        with patch(
            "tolokaforge.docker.wheel_resolver._run",
            return_value=(False, "HTTP 404 for tolokaforge==999"),
        ) as mock_run:
            ok, err = _run_pip(["download", "tolokaforge==999"])
        assert not ok
        assert "HTTP 404" in err
        assert mock_run.call_count == 1  # no ensurepip attempted


class TestRunUv:
    def test_uv_absent_returns_false(self):
        with patch(
            "tolokaforge.docker.wheel_resolver.shutil.which",
            return_value=None,
        ):
            ok, err = _run_uv(["pip", "download", "x"])
        assert not ok
        assert "uv not on PATH" in err

    def test_uv_present_invokes_run(self):
        with (
            patch(
                "tolokaforge.docker.wheel_resolver.shutil.which",
                return_value="/usr/bin/uv",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._run",
                return_value=(True, ""),
            ) as mock_run,
        ):
            ok, err = _run_uv(["pip", "download", "x"])
        assert ok
        assert err == ""
        argv = mock_run.call_args.args[0]
        assert argv[0] == "uv"


# ===================================================================
# ReinstallWheelProvider — the load-bearing addition for #29 + #13
# ===================================================================


class TestReinstallWheelProvider:
    """Reads PEP 610 ``direct_url.json`` and dispatches on install origin.

    Covers all four cases (vcs_info, archive_info, dir_info, and no
    direct_url) plus the by-version fallback when the origin-specific
    path succeeds but produces no matching wheel.
    """

    @staticmethod
    def _drop_wheel(cache_dir: Path, ver: str = "0.3.0") -> Path:
        whl = cache_dir / f"tolokaforge-{ver}-py3-none-any.whl"
        whl.write_bytes(b"PK\x03\x04materialized")
        return whl

    def test_not_installed_yields_none(self, cache_dir: Path):
        provider = ReinstallWheelProvider()
        with patch(
            "tolokaforge.docker.wheel_resolver._installed_version",
            return_value=None,
        ):
            assert provider.provide(cache_dir) is None
        assert "not installed" in provider.last_failure

    def test_git_install_clones_and_builds(self, cache_dir: Path):
        """direct_url.vcs_info present → clone + uv build → wheel."""
        provider = ReinstallWheelProvider()
        direct_url = {
            "url": "https://github.com/Toloka/tolokaforge.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc123def456"},
        }

        def fake_git(url, commit, cache_d):
            self._drop_wheel(cache_d)
            return True, ""

        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=direct_url,
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_git",
                staticmethod(fake_git),
            ),
        ):
            artifact = provider.provide(cache_dir)

        assert artifact is not None
        assert artifact.version == "0.3.0"
        assert artifact.provider_name == "reinstall"

    def test_archive_install_downloads(self, cache_dir: Path):
        """direct_url.archive_info present → download URL → wheel."""
        provider = ReinstallWheelProvider()
        direct_url = {
            "url": "https://example.com/tolokaforge-0.3.0-py3-none-any.whl",
            "archive_info": {"hash": "sha256=abc"},
        }

        def fake_archive(url, cache_d):
            self._drop_wheel(cache_d)
            return True, ""

        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=direct_url,
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_archive",
                staticmethod(fake_archive),
            ),
        ):
            artifact = provider.provide(cache_dir)

        assert artifact is not None
        assert artifact.provider_name == "reinstall"

    def test_no_direct_url_downloads_by_version(self, cache_dir: Path):
        """PyPI install (no direct_url) → uv/pip download by name+version."""
        provider = ReinstallWheelProvider()

        def fake_by_version(ver, cache_d):
            self._drop_wheel(cache_d, ver=ver)
            return True, ""

        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=None,
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_by_version",
                staticmethod(fake_by_version),
            ),
        ):
            artifact = provider.provide(cache_dir)

        assert artifact is not None
        assert artifact.provider_name == "reinstall"

    def test_git_failure_does_not_fall_back_to_pypi(self, cache_dir: Path):
        """A failed git-origin materialize must NOT fall back to a PyPI download.

        A private git commit almost certainly isn't published to PyPI, so
        the by-version fallback would waste a network round-trip and could
        silently substitute a different version's wheel if one happens to
        exist on the configured index.
        """
        provider = ReinstallWheelProvider()
        direct_url = {
            "url": "https://github.com/Toloka/tolokaforge.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc123"},
        }
        by_version_calls: list[int] = []

        def fake_by_version(ver, cache_d):
            by_version_calls.append(1)
            self._drop_wheel(cache_d, ver=ver)
            return True, ""

        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=direct_url,
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_git",
                staticmethod(lambda u, c, d: (False, "clone: fatal: unable to access")),
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_by_version",
                staticmethod(fake_by_version),
            ),
        ):
            assert provider.provide(cache_dir) is None

        assert by_version_calls == []  # never tried the PyPI fallback
        assert "clone: fatal: unable to access" in provider.last_failure

    def test_archive_failure_falls_back_to_by_version(self, cache_dir: Path):
        """archive_info origin failure DOES fall back to by-version.

        Unlike git commits, an archive URL usually names the exact
        version, and the same version may still be on the configured
        index — so falling back is a reasonable recovery.
        """
        provider = ReinstallWheelProvider()
        direct_url = {
            "url": "https://example.com/tolokaforge-0.3.0-py3-none-any.whl",
            "archive_info": {},
        }

        def fake_by_version(ver, cache_d):
            self._drop_wheel(cache_d, ver=ver)
            return True, ""

        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=direct_url,
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_archive",
                staticmethod(lambda u, d: (False, "connection reset")),
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_by_version",
                staticmethod(fake_by_version),
            ),
        ):
            artifact = provider.provide(cache_dir)

        assert artifact is not None
        assert artifact.provider_name == "reinstall"

    def test_all_paths_fail_surfaces_stderr(self, cache_dir: Path):
        """When every path fails, last_failure carries per-attempt stderr."""
        provider = ReinstallWheelProvider()
        direct_url = {
            "url": "https://example.com/tolokaforge-0.3.0-py3-none-any.whl",
            "archive_info": {},
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
            patch.object(
                ReinstallWheelProvider,
                "_materialize_archive",
                staticmethod(lambda u, d: (False, "download: connection reset")),
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_by_version",
                staticmethod(lambda v, d: (False, "index not reachable")),
            ),
        ):
            assert provider.provide(cache_dir) is None

        assert "download: connection reset" in provider.last_failure
        assert "index not reachable" in provider.last_failure

    def test_dir_info_refused_explicitly(self, cache_dir: Path):
        """direct_url with dir_info (local-path install) must be refused,
        not silently substituted with a PyPI download of the same version."""
        provider = ReinstallWheelProvider()
        by_version_calls: list[int] = []

        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value={
                    "url": "file:///home/dev/tolokaforge-fork",
                    "dir_info": {},
                },
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_by_version",
                staticmethod(lambda v, d: by_version_calls.append(1) or (True, "")),
            ),
        ):
            assert provider.provide(cache_dir) is None

        assert by_version_calls == []
        assert "local-path install" in provider.last_failure
        assert "no reachable origin" in provider.last_failure

    def test_vcs_info_missing_fields_recorded(self, cache_dir: Path):
        """A malformed direct_url with no commit_id is surfaced clearly."""
        provider = ReinstallWheelProvider()
        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.3.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value={"vcs_info": {"vcs": "git"}},
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_by_version",
                staticmethod(lambda v, d: (False, "no such version on PyPI")),
            ),
        ):
            assert provider.provide(cache_dir) is None
        assert "missing url or commit_id" in provider.last_failure

    def test_materialize_git_returns_false_when_git_absent(self, cache_dir: Path):
        with patch(
            "tolokaforge.docker.wheel_resolver.shutil.which",
            return_value=None,
        ):
            ok, err = ReinstallWheelProvider._materialize_git(
                "https://example.com/repo.git", "abc123", cache_dir
            )
        assert not ok
        assert "git not on PATH" in err

    def test_materialize_archive_rejects_non_archive_url(self, cache_dir: Path):
        ok, err = ReinstallWheelProvider._materialize_archive(
            "https://example.com/some/dir/", cache_dir
        )
        assert not ok
        assert "does not end in a wheel/sdist suffix" in err

    def test_materialize_archive_accepts_zip_sdist(self, cache_dir: Path, monkeypatch):
        """Archive filter accepts .zip (a legit Python sdist extension)."""
        called: list[str] = []

        def fake_urlretrieve(url, dest):
            called.append(url)
            Path(dest).write_bytes(b"PK\x03\x04sdist-zip")

        monkeypatch.setattr(
            "tolokaforge.docker.wheel_resolver.urllib.request.urlretrieve",
            fake_urlretrieve,
        )
        ok, err = ReinstallWheelProvider._materialize_archive(
            "https://example.com/pkg/tolokaforge-0.3.0.zip", cache_dir
        )
        assert ok
        assert err == ""
        assert (cache_dir / "tolokaforge-0.3.0.zip").is_file()


# ===================================================================
# NoWheelError message enrichment
# ===================================================================


class _FailingProvider(WheelProvider):
    """Provider that always fails with a controlled failure reason."""

    def __init__(self, name: str, priority: int, reason: str) -> None:
        super().__init__()
        self.name = name
        self.priority = priority
        self._reason = reason

    def provide(self, cache_dir: Path):
        self.last_failure = self._reason
        return None


class TestNoWheelErrorMessage:
    """Pin the enriched error format: per-provider failure lines + remediation."""

    def test_lists_every_provider_and_reason(self, cache_dir: Path):
        chain = WheelResolver(
            [
                _FailingProvider("alpha", 10, "no source tree"),
                _FailingProvider("beta", 20, "cache empty"),
                _FailingProvider("gamma", 30, "network error: HTTPError 502"),
            ]
        )
        with pytest.raises(NoWheelError) as exc_info:
            chain.resolve(cache_dir)
        msg = str(exc_info.value)
        assert "Could not resolve" in msg
        assert "alpha: no source tree" in msg
        assert "beta: cache empty" in msg
        assert "gamma: network error: HTTPError 502" in msg
        assert "Remediation" in msg

    def test_records_env_var_state(self, cache_dir: Path, monkeypatch):
        monkeypatch.setenv("UV_CACHE_DIR", "/tmp/uv-relocated")
        monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
        chain = WheelResolver([_FailingProvider("solo", 10, "nothing worked")])
        with pytest.raises(NoWheelError) as exc_info:
            chain.resolve(cache_dir)
        msg = str(exc_info.value)
        assert "UV_CACHE_DIR=/tmp/uv-relocated" in msg
        assert "PIP_CACHE_DIR=unset" in msg

    def test_provider_without_reason_marks_placeholder(self, cache_dir: Path):
        """A provider that forgot to set last_failure still shows up."""
        chain = WheelResolver([_FailingProvider("silent", 10, "")])
        with pytest.raises(NoWheelError) as exc_info:
            chain.resolve(cache_dir)
        msg = str(exc_info.value)
        assert "silent:" in msg
        assert "no reason recorded" in msg


# ===================================================================
# PipDownloadWheelProvider
# ===================================================================


class TestPipDownloadWheelProvider:
    """Delegates to ReinstallWheelProvider._materialize_by_version so both
    providers share one implementation of the pip-download path."""

    @staticmethod
    def _drop_wheel(cache_dir: Path, ver: str = "0.2.0") -> None:
        whl = cache_dir / f"tolokaforge-{ver}-py3-none-any.whl"
        whl.write_bytes(b"PK\x03\x04pypi-wheel")

    def test_delegates_to_by_version_and_succeeds(self, cache_dir: Path):
        provider = PipDownloadWheelProvider()

        def fake_by_version(ver, cache_d):
            self._drop_wheel(cache_d, ver=ver)
            return True, ""

        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.2.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=None,
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_by_version",
                staticmethod(fake_by_version),
            ),
        ):
            artifact = provider.provide(cache_dir)

        assert artifact is not None
        assert artifact.version == "0.2.0"
        assert artifact.provider_name == "pip-download"

    def test_by_version_failure_surfaces_stderr(self, cache_dir: Path):
        provider = PipDownloadWheelProvider()
        with (
            patch(
                "tolokaforge.docker.wheel_resolver._installed_version",
                return_value="0.2.0",
            ),
            patch(
                "tolokaforge.docker.wheel_resolver._read_direct_url",
                return_value=None,
            ),
            patch.object(
                ReinstallWheelProvider,
                "_materialize_by_version",
                staticmethod(
                    lambda v, d: (
                        False,
                        f"pip download tolokaforge=={v}: HTTPError: 404 Not Found",
                    )
                ),
            ),
        ):
            assert provider.provide(cache_dir) is None
        assert "HTTPError: 404 Not Found" in provider.last_failure

    def test_reinstall_owned_origins_are_skipped(self, cache_dir: Path):
        """git, archive, and dir origins are all owned by ReinstallWheelProvider —
        PipDownload short-circuits so it never runs a duplicate download."""
        for origin_key, extra in (
            ("vcs_info", {"vcs": "git", "commit_id": "abc123"}),
            ("archive_info", {}),
            ("dir_info", {}),
        ):
            provider = PipDownloadWheelProvider()
            with (
                patch(
                    "tolokaforge.docker.wheel_resolver._installed_version",
                    return_value="0.3.0",
                ),
                patch(
                    "tolokaforge.docker.wheel_resolver._read_direct_url",
                    return_value={"url": "…", origin_key: extra},
                ),
            ):
                assert provider.provide(cache_dir) is None
            assert origin_key in provider.last_failure
            assert "reinstall provider" in provider.last_failure

    def test_not_installed_yields_none(self, cache_dir: Path):
        provider = PipDownloadWheelProvider()
        with patch(
            "tolokaforge.docker.wheel_resolver._installed_version",
            return_value=None,
        ):
            assert provider.provide(cache_dir) is None
        assert "not installed" in provider.last_failure


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
        with pytest.raises(NoWheelError, match="Could not resolve"):
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
        assert "reinstall" in names
        assert "pip-download" in names
        # Chain order: local-source (10) → pip-cache (20) →
        # reinstall (25) → pip-download (30).
        assert names.index("local-source") < names.index("pip-cache")
        assert names.index("pip-cache") < names.index("reinstall")
        assert names.index("reinstall") < names.index("pip-download")


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
