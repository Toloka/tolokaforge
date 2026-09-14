"""Guard the four invariants of the two-lockfile pattern.

The two-lockfile pattern (see [`docs/DEV_SETUP.md`](../../docs/DEV_SETUP.md))
commits both `uv.lock.public` (resolved against `pypi.org`, used by CI /
arena runners / external contributors) and `uv.lock.jfrog` (resolved
through Toloka's JFrog mirror, used by internal Toloka Macs whose SecOps
policy blocks direct PyPI access). The working `uv.lock` is generated
locally from one of them via `make use-public` / `make use-jfrog` and is
gitignored.

The failure mode this test exists to prevent: someone regenerates
`uv.lock.public` from a Toloka Mac (where the personal `~/.pip/pip.conf`
points at JFrog), inadvertently embedding `toloka.jfrog.io` URLs into the
"public" flavour. That lock then ships to arena runners / external
containers that can't reach `toloka.jfrog.io`, and their `uv sync` fails.

This test is a mechanical guard for the shape; the regeneration ritual
lives in `docs/RELEASING.md`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import tomllib

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_uv_lock_is_not_tracked() -> None:
    """`uv.lock` at the repo root is generated locally and must not be
    committed. Only `uv.lock.jfrog` and `uv.lock.public` are tracked."""
    result = subprocess.run(
        ["git", "ls-files", "--", "uv.lock"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        "uv.lock is tracked; the two-lockfile pattern requires it to be "
        "gitignored so a `uv lock` on any machine can't silently clobber the "
        "committed public/jfrog variants. Run `git rm --cached uv.lock` and "
        "confirm `.gitignore` still lists `/uv.lock`."
    )


def test_public_lock_carries_no_jfrog_urls() -> None:
    """`uv.lock.public` is the CI-canonical flavour; every URL in it must
    resolve against `pypi.org`. A `toloka.jfrog.io` URL leaking in means
    someone regenerated the public lock from a JFrog-pointed environment,
    and CI / arena runners / external contributors will fail to fetch."""
    text = (_REPO_ROOT / "uv.lock.public").read_text()
    assert "toloka.jfrog.io" not in text, (
        "uv.lock.public contains `toloka.jfrog.io` URLs. Regenerate it from "
        "a shell whose pip/uv defaults do not point at JFrog, e.g. "
        "`uv lock --config-file uv-public.toml && cp uv.lock uv.lock.public` "
        "(the `--config-file` flag overrides personal config). See "
        "docs/DEV_SETUP.md § 'Regenerating both lockfiles'."
    )


def test_jfrog_and_public_locks_pin_the_same_set() -> None:
    """Both lockfiles must name the same package set at the same versions;
    only the per-package `url` / `sha256` fields may differ. A version drift
    between them means one was regenerated but not the other, and internal
    vs external contributors will end up on different resolved sets."""
    jfrog = tomllib.loads((_REPO_ROOT / "uv.lock.jfrog").read_text())
    public = tomllib.loads((_REPO_ROOT / "uv.lock.public").read_text())

    def pinned_set(lock: dict) -> set[tuple[str, str]]:
        return {(pkg["name"], pkg["version"]) for pkg in lock.get("package", [])}

    jfrog_set = pinned_set(jfrog)
    public_set = pinned_set(public)
    only_jfrog = sorted(jfrog_set - public_set)
    only_public = sorted(public_set - jfrog_set)
    assert not only_jfrog and not only_public, (
        "uv.lock.jfrog and uv.lock.public pin different package sets:\n"
        f"  only in jfrog: {only_jfrog}\n  only in public: {only_public}\n"
        "Regenerate both together via `make refresh-locks`."
    )


def test_index_configs_declare_the_expected_indexes() -> None:
    """`uv-jfrog.toml` and `uv-public.toml` each carry exactly one
    `[[index]]` block, pointing at the sanctioned mirror for their audience.
    This test locks the shape so a rename or a silently-added second index
    fails visibly rather than confusing a downstream `uv lock`."""
    jfrog_cfg = tomllib.loads((_REPO_ROOT / "uv-jfrog.toml").read_text())
    public_cfg = tomllib.loads((_REPO_ROOT / "uv-public.toml").read_text())

    for name, cfg, expected_url in (
        (
            "uv-jfrog.toml",
            jfrog_cfg,
            "https://toloka.jfrog.io/artifactory/api/pypi/pypi-virtual/simple/",
        ),
        ("uv-public.toml", public_cfg, "https://pypi.org/simple/"),
    ):
        indexes = cfg.get("index", [])
        assert len(indexes) == 1, (
            f"{name} declares {len(indexes)} indexes; the two-lockfile "
            "pattern requires exactly one so regeneration is deterministic."
        )
        assert indexes[0].get("url") == expected_url, (
            f"{name} points at {indexes[0].get('url')!r}; expected " f"{expected_url!r}."
        )
        assert indexes[0].get("default") is True, (
            f"{name}'s index is not marked `default = true`; without it, "
            "`uv lock --config-file` would treat the URL as a fallback and "
            "produce non-deterministic resolutions."
        )
