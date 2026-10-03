"""Real-git behaviour lock for the reinstall provider's LFS-free clone.

The unit test in ``test_wheel_resolver.py`` captures the git ``argv``/``env`` at
the ``_run`` boundary; this one drives real ``git`` + ``git-lfs`` against a local
repo whose LFS object cannot be fetched, proving the provider resolves a wheel
where a plain (smudging) clone fails. No Docker, no network to a real server —
a ``file://`` origin plus a dead LFS endpoint.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tolokaforge.docker.wheel_resolver import ReinstallWheelProvider

pytestmark = pytest.mark.integration

_HAVE_GIT_LFS = shutil.which("git") is not None and shutil.which("git-lfs") is not None
_HAVE_UV = shutil.which("uv") is not None


def _make_lfs_repo(root: Path) -> tuple[str, str]:
    """A buildable package whose one LFS-tracked file cannot be smudged.

    Returns ``(file_url, sha)``. The committed ``lfs.url`` points at a dead
    endpoint, so any checkout that runs the smudge filter fails fetching the
    object — the exact shape of the issue's credential-denied LFS fetch.
    """
    src = root / "src_repo"
    src.mkdir()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(src), *args], check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.email", "t@t.io")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    subprocess.run(
        ["git", "-C", str(src), "lfs", "install", "--local"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(src), "lfs", "track", "*.bin"], check=True, capture_output=True
    )

    (src / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "[project]\n"
        'name = "lfs-repro-pkg"\n'
        'version = "0.0.1"\n'
    )
    pkg = src / "src" / "lfs_repro_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("__version__ = '0.0.1'\n")
    (src / "data.bin").write_bytes(b"\x00" * 2048)  # LFS-tracked payload

    git("add", ".gitattributes", "pyproject.toml", "src", "data.bin")
    git("commit", "-q", "-m", "repo with an LFS file")
    # Point LFS at a dead endpoint and drop the local object, so a checkout that
    # smudges must hit that endpoint and fail.
    git("config", "-f", ".lfsconfig", "lfs.url", "http://127.0.0.1:1/lfs")
    git("add", ".lfsconfig")
    git("commit", "-q", "--amend", "--no-edit")
    sha = git("rev-parse", "HEAD").stdout.strip()
    shutil.rmtree(src / ".git" / "lfs" / "objects", ignore_errors=True)
    return f"file://{src}", sha


@pytest.mark.skipif(not _HAVE_GIT_LFS, reason="git / git-lfs not installed")
@pytest.mark.skipif(not _HAVE_UV, reason="uv not on PATH for the wheel build")
def test_reinstall_resolves_wheel_despite_unfetchable_lfs(tmp_path: Path, monkeypatch) -> None:
    url, sha = _make_lfs_repo(tmp_path)
    # Never hang on a credential prompt if the environment has one configured.
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    # Control: a plain clone+checkout that smudges MUST fail, proving the repo
    # genuinely triggers the LFS failure the provider has to dodge.
    plain = tmp_path / "plain"
    clone = subprocess.run(
        ["git", "clone", "--filter=blob:none", url, str(plain)],
        capture_output=True,
        text=True,
    )
    if clone.returncode == 0:
        checkout = subprocess.run(
            ["git", "-C", str(plain), "checkout", sha], capture_output=True, text=True
        )
        assert checkout.returncode != 0, (
            "plain checkout unexpectedly succeeded — the repo is not triggering "
            "an LFS smudge failure, so this test would not prove the fix"
        )

    # The provider adds GIT_LFS_SKIP_SMUDGE=1, so the clone+checkout succeed and
    # it goes on to build the wheel from the checked-out source.
    ok, err = ReinstallWheelProvider()._materialize_git(url, sha, tmp_path / "cache")
    assert ok, f"_materialize_git failed despite skip-smudge: {err}"
