"""Wheel-based Docker provisioning for tolokaforge.

Determines how to provision a ``tolokaforge`` wheel on the host so the
Docker builder can ``COPY`` it into the runner image.  The container
always installs from a wheel — no conditional Dockerfile logic needed.

Four providers are tried in priority order:

1. **LocalSourceWheelProvider** (priority 10) — builds a wheel from a
   local source checkout (cached by source-tree SHA-256). Fires when
   the code is running from a git clone with the engine's
   ``pyproject.toml`` reachable via ``Path(__file__).parents``.
2. **PipCacheWheelProvider** (priority 20) — locates a wheel that pip
   or uv already built in their local caches. Fast-path when the same
   host has previously installed the engine; no-op on a cold runner.
3. **ReinstallWheelProvider** (priority 25) — materializes a wheel by
   re-fetching from the installed distribution's origin using PEP 610
   ``direct_url.json`` metadata: a ``git clone`` at the pinned commit
   for git installs; a direct download for archive-URL installs; a
   ``pip download --only-binary`` for plain PyPI installs. Refuses
   ``dir_info`` (local-path) origins so a PyPI wheel is never silently
   substituted for the user's local checkout.
4. **PipDownloadWheelProvider** (priority 30) — final fallback:
   ``python -m pip download`` for the installed name+version, with
   ``ensurepip`` auto-recovery when pip is not installed. Delegates to
   :meth:`ReinstallWheelProvider._materialize_by_version` so both
   providers share one implementation of the download path.

The resolver stops at the first successful provider. When every
provider returns ``None``, :class:`NoWheelError` is raised with per-
provider failure reasons included so the root cause is visible.

Public API
----------
- :func:`resolve_wheel` — cached, module-level convenience.
- :class:`WheelResolver` — explicit chain construction + resolution.
- :class:`WheelArtifact` — frozen description of the located wheel.
- :class:`WheelProvider` — abstract base for new providers.

Extension point
~~~~~~~~~~~~~~~
Subclass :class:`WheelProvider`, set ``name`` and ``priority``, implement
:meth:`provide` (and optionally set ``self.last_failure`` before
returning ``None`` so failures show up in :class:`NoWheelError`), and
register via ``WheelResolver.register()``.

Install-scenario matrix (which provider wins in which scenario):

* Source checkout (``git clone`` + ``uv sync``) → LocalSource.
* Git-tag install (``uv sync`` with ``tolokaforge = { git = "…", tag =
  "v0.7.0" }``) → PipCache if the uv cache is warm and discoverable,
  else Reinstall clones the same git ref and builds a wheel.
* PyPI wheel install (``pip install tolokaforge`` or ``uv pip install
  tolokaforge``) → PipCache if warm, else Reinstall calls
  ``pip download --only-binary=:all:`` for the pinned version.
* Fresh CI runner with relocated ``UV_CACHE_DIR`` → PipCache picks it
  up via env-var-first resolution, or Reinstall re-fetches from origin.
* Local-path install (``pip install /path/to/checkout``) → Reinstall
  refuses ``dir_info`` so a PyPI wheel is never silently substituted
  for the user's local checkout.
* uv env without pip (bare ``uv venv``) → Reinstall / PipDownload
  invoke ``ensurepip`` to bootstrap pip, then run ``pip download``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "LocalSourceWheelProvider",
    "NoWheelError",
    "PipCacheWheelProvider",
    "PipDownloadWheelProvider",
    "ReinstallWheelProvider",
    "WheelArtifact",
    "WheelProvider",
    "WheelResolver",
    "resolve_wheel",
]

_ENGINE_PKG = "tolokaforge"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WheelResolverError(RuntimeError):
    """Base class for wheel resolver errors."""


class NoWheelError(WheelResolverError):
    """Raised when no provider can produce a wheel."""


# ---------------------------------------------------------------------------
# WheelArtifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WheelArtifact:
    """A located or built wheel ready to be COPY'd into a Docker context."""

    path: Path
    """Absolute path to the ``.whl`` on the host."""

    version: str
    """tolokaforge version string (e.g. ``"0.3.0"``)."""

    content_hash: str
    """SHA-256 of the wheel file content (for Docker cache busting)."""

    provider_name: str
    """Which provider produced this artifact (for logging)."""


# ---------------------------------------------------------------------------
# WheelProvider ABC
# ---------------------------------------------------------------------------


class WheelProvider(ABC):
    """Produces a tolokaforge wheel on the host.

    Subclasses set ``name`` and ``priority``, and implement :meth:`provide`.
    Optionally, ``self.last_failure`` should be set to a short human-
    readable string before returning ``None`` from :meth:`provide` so
    :class:`NoWheelError` can surface the root cause to callers.
    """

    name: str = ""
    """Human-readable identifier (for logging and error messages)."""

    priority: int = 100
    """Lower values are tried first."""

    last_failure: str = ""
    """Reason recorded when :meth:`provide` returned ``None``.

    Class-level default is an empty string so subclasses that override
    ``__init__`` without calling ``super().__init__()`` still expose the
    attribute — the resolver's failure-message assembly reads it
    unconditionally."""

    @abstractmethod
    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        """Locate or build a wheel, placing it under *cache_dir*.

        Return a :class:`WheelArtifact` on success, or ``None`` to
        yield to the next provider in the chain. When returning
        ``None``, set ``self.last_failure`` to the reason string so
        the resolver can include it in :class:`NoWheelError` on the
        end-of-chain path.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} priority={self.priority}>"


# ---------------------------------------------------------------------------
# Source-tree hashing
# ---------------------------------------------------------------------------

# Extensions to include when hashing the source tree.  Covers all
# files that affect the built wheel's behaviour.
_HASH_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyx",
        ".pxd",
        ".toml",
        ".cfg",
        ".txt",
        ".md",
        ".yaml",
        ".yml",
        ".json",
        ".j2",
        ".jinja",
        ".jinja2",
        ".proto",
        ".sql",
        ".sh",
    }
)


def _hash_source_tree(root: Path) -> str:
    """Compute a stable SHA-256 over the engine source tree.

    Walks ``root/tolokaforge/`` plus ``root/pyproject.toml`` and hashes
    every file matching ``_HASH_EXTENSIONS``.  Files are sorted by
    relative path for determinism.
    """
    h = hashlib.sha256()
    files: list[tuple[str, Path]] = []

    pyproj = root / "pyproject.toml"
    if pyproj.is_file():
        files.append(("pyproject.toml", pyproj))

    pkg_dir = root / _ENGINE_PKG
    if pkg_dir.is_dir():
        for p in sorted(pkg_dir.rglob("*")):
            if p.is_file() and p.suffix in _HASH_EXTENSIONS:
                files.append((str(p.relative_to(root)), p))

    for rel, path in files:
        h.update(rel.encode())
        h.update(path.read_bytes())

    return h.hexdigest()


def _hash_file(path: Path) -> str:
    """SHA-256 of a single file's content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# pyproject.toml helpers (stdlib-only, no tomli)
# ---------------------------------------------------------------------------


def _is_engine_pyproject(path: Path) -> bool:
    """``True`` if *path* is a ``pyproject.toml`` for the tolokaforge engine."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("["):
            break
        if in_project:
            m = re.match(r"""name\s*=\s*["']([^"']+)["']""", stripped)
            if m and m.group(1) == _ENGINE_PKG:
                return True
    return False


def _read_pyproject_version(root: Path) -> str | None:
    """Extract ``project.version`` from a ``pyproject.toml``."""
    path = root / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("["):
            break
        if in_project:
            m = re.match(r"""version\s*=\s*["']([^"']+)["']""", stripped)
            if m:
                return m.group(1)
    return None


def _find_engine_source_root() -> Path | None:
    """Walk up from this module's file looking for the engine checkout."""
    candidate = Path(__file__).resolve()
    for parent in candidate.parents:
        pyproj = parent / "pyproject.toml"
        if pyproj.is_file() and _is_engine_pyproject(pyproj):
            return parent
    return None


# ---------------------------------------------------------------------------
# Package metadata helpers
# ---------------------------------------------------------------------------


def _installed_version(pkg: str = _ENGINE_PKG) -> str | None:
    try:
        return version(pkg)
    except PackageNotFoundError:
        return None


def _read_direct_url(pkg: str = _ENGINE_PKG) -> dict | None:
    """Read PEP 610 ``direct_url.json`` for an installed distribution."""
    try:
        dist = distribution(pkg)
    except PackageNotFoundError:
        return None
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Subprocess helpers (uv / pip / git)
# ---------------------------------------------------------------------------


_STDERR_TAIL = 800
"""Max chars of stderr to preserve in per-provider failure messages.

Keeps :class:`NoWheelError` output diagnostic without turning it into a
multi-KB wall of text — long pip / uv error traces get truncated to the
tail (which is where the actual cause typically sits)."""


def _stderr_tail(text: str) -> str:
    """Return the last ``_STDERR_TAIL`` chars of *text*, right-stripped."""
    if not text:
        return ""
    trimmed = text.rstrip()
    if len(trimmed) <= _STDERR_TAIL:
        return trimmed
    return "…" + trimmed[-_STDERR_TAIL:]


def _run(argv: list[str], *, timeout: int = 300) -> tuple[bool, str]:
    """Run *argv* to completion. Return ``(ok, stderr_tail)``.

    ``ok`` is ``True`` on exit code 0. On any subprocess error
    (``FileNotFoundError`` for a missing binary, ``TimeoutExpired``,
    non-zero exit, etc.) ``ok`` is ``False`` and ``stderr_tail`` carries
    a short, human-readable reason (with the tail of the child's stderr
    when applicable). Never raises.
    """
    try:
        result = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, f"{argv[0]!r} not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]!r} timed out after {timeout}s"
    except OSError as exc:
        return False, f"{argv[0]!r} failed: {exc}"
    if result.returncode == 0:
        return True, ""
    return False, _stderr_tail(result.stderr or result.stdout)


def _run_pip(args: list[str], *, timeout: int = 300) -> tuple[bool, str]:
    """Run ``python -m pip <args>``.

    If pip is missing (as in ``uv``-created virtualenvs that omit pip by
    default), the first attempt fails with ``ModuleNotFoundError: No
    module named pip``. We detect that specific error, bootstrap pip via
    ``ensurepip --upgrade``, and retry the invocation once. Child stderr
    is preserved on failure.
    """
    for attempt in (1, 2):
        ok, err = _run([sys.executable, "-m", "pip", *args], timeout=timeout)
        if ok:
            return True, ""
        # Detect missing-pip and bootstrap once via ensurepip.
        if attempt == 1 and "No module named pip" in err:
            logger.info(
                "pip not installed in %s; bootstrapping via ensurepip",
                sys.executable,
            )
            boot_ok, boot_err = _run(
                [sys.executable, "-m", "ensurepip", "--upgrade"],
                timeout=60,
            )
            if not boot_ok:
                return False, f"pip missing; ensurepip failed: {boot_err}"
            continue
        return False, err
    return False, "unreachable"


def _run_uv(args: list[str], *, timeout: int = 300) -> tuple[bool, str]:
    """Run ``uv <args>``. Returns ``(False, 'uv not on PATH')`` when uv
    is unavailable. Never raises."""
    if shutil.which("uv") is None:
        return False, "uv not on PATH"
    return _run(["uv", *args], timeout=timeout)


# ---------------------------------------------------------------------------
# Wheel filename helpers
# ---------------------------------------------------------------------------


def _newest_whl(directory: Path, prefix: str) -> Path | None:
    """Return the newest ``.whl`` in *directory* whose name starts with *prefix*."""
    candidates = sorted(
        (p for p in directory.glob(f"{prefix}*.whl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _newest_whl_for_version(directory: Path, prefix: str, ver: str) -> Path | None:
    """Return the newest ``.whl`` in *directory* whose name matches *prefix* AND *ver*.

    Uses :func:`_wheel_matches_version` to filter by the PEP 427 version
    field so a stale wheel of a different version left in *directory* by a
    prior provider run is never returned tagged with the current version.
    """
    candidates = [
        p
        for p in directory.glob(f"{prefix}*.whl")
        if p.is_file() and _wheel_matches_version(p, ver)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Pip / uv cache walking
# ---------------------------------------------------------------------------


def _dedup_paths(paths: list[Path]) -> list[Path]:
    """De-duplicate paths (after ``expanduser``), preserving first-seen order."""
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        ep = p.expanduser()
        key = str(ep)
        if key not in seen:
            seen.add(key)
            out.append(ep)
    return out


@lru_cache(maxsize=1)
def _uv_cache_dir_from_cli() -> Path | None:
    """Ask ``uv`` for its cache directory (authoritative location).

    ``uv cache dir`` honors ``UV_CACHE_DIR``, ``XDG_CACHE_HOME``, and uv config,
    so it covers relocations (e.g. ``astral-sh/setup-uv`` in CI) that the
    hard-coded default would miss.  Returns ``None`` if ``uv`` is unavailable or
    the call fails — callers fall back to the default location.
    """
    if shutil.which("uv") is None:
        return None
    try:
        result = subprocess.run(
            ["uv", "cache", "dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out) if out else None


def _uv_cache_bases() -> list[Path]:
    """uv cache roots to search, de-duplicated, in precedence order.

    Order:

    1. ``UV_CACHE_DIR`` from the process environment — the most explicit
       signal a user can send. CI tools such as ``astral-sh/setup-uv``
       relocate the cache by setting this variable; the resolver honors
       it directly without needing a working ``uv`` CLI subprocess.
    2. ``uv cache dir`` CLI output — authoritative when the binary is
       reachable; already honors ``UV_CACHE_DIR``, ``XDG_CACHE_HOME``,
       and uv config.
    3. ``~/.cache/uv`` — the default, kept as a final fallback.

    All three candidates are searched (deduplicated) rather than
    short-circuiting on the first, so a stale CLI result never masks a
    cache-hit on the env-var-relocated path.

    This is *location* discovery — the on-disk *layout* within a root is
    uv-internal and handled by :func:`_walk_pip_wheel_caches`.
    """
    candidates: list[Path] = []
    env = os.environ.get("UV_CACHE_DIR")
    if env:
        candidates.append(Path(env))
    cli = _uv_cache_dir_from_cli()
    if cli is not None:
        candidates.append(cli)
    candidates.append(Path.home() / ".cache" / "uv")
    return _dedup_paths(candidates)


def _pip_wheel_cache_bases() -> list[Path]:
    """pip wheel-cache roots to search, de-duplicated.

    Honors ``PIP_CACHE_DIR`` (pip stores built wheels under ``<cache>/wheels``)
    then the default ``~/.cache/pip/wheels``.
    """
    candidates: list[Path] = []
    env = os.environ.get("PIP_CACHE_DIR")
    if env:
        candidates.append(Path(env) / "wheels")
    candidates.append(Path.home() / ".cache" / "pip" / "wheels")
    return _dedup_paths(candidates)


def _walk_pip_wheel_caches() -> list[Path]:
    """Find all tolokaforge wheels in the pip and uv caches.

    *Where* to look is discovered dynamically — ``UV_CACHE_DIR`` / ``uv cache dir``
    and ``PIP_CACHE_DIR``, plus the ``~/.cache`` defaults (see
    :func:`_uv_cache_bases` / :func:`_pip_wheel_cache_bases`) — because CI tools
    such as ``astral-sh/setup-uv`` relocate the cache.

    *How* uv lays wheels out within a cache root is uv-internal:
    - ``<uv-cache>/sdists-v*/``       — wheels built from source/git installs
    - ``<uv-cache>/wheels-v*/``       — pre-built wheels from indices
    - ``<uv-cache>/built-wheels-v*/`` — (older uv versions)
    pip uses ``<pip-cache>/wheels/``.
    """
    results: list[Path] = []
    whl_glob = f"{_ENGINE_PKG}-*.whl"

    # pip caches
    for pip_cache in _pip_wheel_cache_bases():
        if pip_cache.is_dir():
            results.extend(pip_cache.rglob(whl_glob))

    # uv caches — search the versioned layout dirs within each cache root
    for uv_base in _uv_cache_bases():
        if not uv_base.is_dir():
            continue
        for pattern in ("sdists-v*", "wheels-v*", "built-wheels-v*"):
            for versioned_dir in uv_base.glob(pattern):
                for whl in versioned_dir.rglob(whl_glob):
                    # Skip editable wheels — they contain .pth pointers
                    # to local dirs, not actual package code.
                    if "/editable/" not in str(whl):
                        results.append(whl)

    return _dedup_paths(results)


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------


class LocalSourceWheelProvider(WheelProvider):
    """Builds a wheel from a local source checkout.

    Detects the engine source root by walking up from ``__file__``.
    Caches wheels under ``cache_dir`` keyed by ``{version}-{source_hash}``.
    Only rebuilds when the source tree actually changes.
    """

    name = "local-source"
    priority = 10

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        source_root = _find_engine_source_root()
        if source_root is None:
            self.last_failure = (
                f"no engine source tree walking up from {Path(__file__).parent} "
                "(wheel-only install: source tree not present in site-packages)"
            )
            return None

        ver = _read_pyproject_version(source_root) or "0.0.0"
        src_hash = _hash_source_tree(source_root)

        # Check cache: a sidecar .sha256 file stores the source hash
        # next to the standard-named wheel.
        existing = _newest_whl(cache_dir, _ENGINE_PKG)
        if existing is not None:
            sha_file = existing.with_suffix(".whl.sha256")
            if sha_file.is_file() and sha_file.read_text().strip() == src_hash:
                logger.info(
                    "%s: cache hit for %s (hash=%s)",
                    self.name,
                    ver,
                    src_hash[:12],
                )
                return WheelArtifact(
                    path=existing,
                    version=ver,
                    content_hash=src_hash,
                    provider_name=self.name,
                )
            # Stale cache — remove old wheel + hash.
            existing.unlink(missing_ok=True)
            sha_file.unlink(missing_ok=True)

        # Cache miss — build the wheel.
        logger.info(
            "%s: building wheel for %s (hash=%s)",
            self.name,
            ver,
            src_hash[:12],
        )
        self._build_wheel(source_root, cache_dir)
        produced = _newest_whl(cache_dir, _ENGINE_PKG)
        if produced is None:
            logger.warning("%s: wheel build produced no output", self.name)
            return None

        # Write sidecar hash file for next cache lookup.
        produced.with_suffix(".whl.sha256").write_text(src_hash)
        return WheelArtifact(
            path=produced,
            version=ver,
            content_hash=src_hash,
            provider_name=self.name,
        )

    def _build_wheel(self, source_root: Path, out_dir: Path) -> None:
        """Build a wheel from *source_root* into *out_dir*.

        Tries ``uv build`` first (handles build isolation internally),
        then falls back to ``pip wheel`` (with ``ensurepip`` recovery
        when pip is missing). Records the reason on ``self.last_failure``
        when both paths fail.
        """
        ok, uv_err = _run_uv(
            ["build", "--wheel", "--out-dir", str(out_dir), str(source_root)],
            timeout=300,
        )
        if ok:
            return
        ok, pip_err = _run_pip(
            ["wheel", "--no-deps", "--wheel-dir", str(out_dir), str(source_root)],
            timeout=300,
        )
        if ok:
            return
        self.last_failure = f"uv build: {uv_err}; pip wheel: {pip_err}"


class PipCacheWheelProvider(WheelProvider):
    """Locates a tolokaforge wheel from the pip/uv wheel cache.

    Fast-path when tolokaforge was previously installed via
    ``pip install`` or ``uv sync`` on the same host — the installer
    already built/downloaded a wheel and cached it locally. When the
    cache is cold (fresh CI runner) or relocated to a directory the
    resolver hasn't discovered, this provider returns ``None`` and the
    chain falls through to :class:`ReinstallWheelProvider`.
    """

    name = "pip-cache"
    priority = 20

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        ver = _installed_version()
        if ver is None:
            self.last_failure = f"{_ENGINE_PKG!r} is not installed in this environment"
            return None

        cache_roots = [*_uv_cache_bases(), *_pip_wheel_cache_bases()]
        candidates = _walk_pip_wheel_caches()
        for whl in candidates:
            # Wheel filename: tolokaforge-{version}-{python}-{abi}-{platform}.whl
            # or tolokaforge-{version}-py3-none-any.whl
            if _wheel_matches_version(whl, ver):
                dest = cache_dir / whl.name
                if not dest.is_file():
                    shutil.copy2(whl, dest)
                content_hash = _hash_file(dest)
                logger.info(
                    "%s: found cached wheel %s",
                    self.name,
                    whl.name,
                )
                return WheelArtifact(
                    path=dest,
                    version=ver,
                    content_hash=content_hash,
                    provider_name=self.name,
                )

        self.last_failure = (
            f"no wheel for {_ENGINE_PKG}=={ver} in {len(candidates)} "
            f"candidate(s) across {len(cache_roots)} cache root(s): "
            f"{', '.join(str(p) for p in cache_roots) or '(none)'}"
        )
        return None


# ---------------------------------------------------------------------------
# ReinstallWheelProvider — materialize a wheel from the installed origin
# ---------------------------------------------------------------------------


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _looks_like_sha(commit: str) -> bool:
    """True when *commit* looks like a hex-encoded git object id.

    ``git clone --branch <ref>`` accepts tags and branches, not commit
    SHAs. Callers use this to skip the shallow-clone fast path when the
    recorded ``commit_id`` is a raw SHA (the common PEP 610 shape).
    """
    return bool(_SHA_RE.fullmatch(commit))


_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tgz", ".zip", ".tar.bz2", ".tar.xz")


class ReinstallWheelProvider(WheelProvider):
    """Materialize a wheel from the installed distribution's origin.

    Reads PEP 610 ``direct_url.json`` from the installed distribution
    and dispatches on install origin:

    * ``vcs_info`` present (git-installed): clone the same git URL at
      the pinned commit and build a wheel via ``uv build --wheel`` or
      ``pip wheel`` (with ``ensurepip`` fallback if pip is missing).
    * ``archive_info`` present (URL-installed wheel/sdist): download
      the same URL directly.
    * No direct_url (plain PyPI install): ``pip download`` for the
      pinned name+version, with ``ensurepip`` auto-recovery.

    ``dir_info`` (local-path install) is intentionally rejected — a
    local checkout has no reachable origin the container can materialize
    the same artifact from, and silently substituting a PyPI wheel of
    the same version would swap the user's local code for public code.
    """

    name = "reinstall"
    priority = 25

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        ver = _installed_version()
        if ver is None:
            self.last_failure = f"{_ENGINE_PKG!r} is not installed in this environment"
            return None

        direct_url = _read_direct_url()
        errors: list[str] = []

        # Case A — git install.
        if direct_url and "vcs_info" in direct_url:
            url = direct_url.get("url", "")
            vcs_info = direct_url["vcs_info"]
            commit = vcs_info.get("commit_id") or vcs_info.get("requested_revision")
            if url and commit:
                ok, err = self._materialize_git(url, commit, cache_dir)
                if ok:
                    art = self._finalize(cache_dir, ver)
                    if art is not None:
                        return art
                    errors.append(f"git@{commit[:12]}: {self.last_failure}")
                else:
                    errors.append(f"git@{commit[:12]}: {err}")
            else:
                errors.append(f"direct_url.vcs_info missing url or commit_id: {vcs_info!r}")
        # Case B — archive URL install.
        elif direct_url and "archive_info" in direct_url:
            url = direct_url.get("url", "")
            if url:
                ok, err = self._materialize_archive(url, cache_dir)
                if ok:
                    art = self._finalize(cache_dir, ver)
                    if art is not None:
                        return art
                    errors.append(f"archive({url}): {self.last_failure}")
                else:
                    errors.append(f"archive({url}): {err}")
            else:
                errors.append("direct_url.archive_info missing url")
        # Case C — dir_info: refuse silently substituting a PyPI wheel.
        elif direct_url and "dir_info" in direct_url:
            path = direct_url.get("url", "<unknown>")
            self.last_failure = (
                f"local-path install ({path}) has no reachable origin — "
                "install tolokaforge from a git ref, an archive URL, or "
                "PyPI so the runner can materialize the same artifact"
            )
            return None
        # Case D — no direct_url or PyPI install → download by name+version.
        else:
            ok, err = self._materialize_by_version(ver, cache_dir)
            if ok:
                art = self._finalize(cache_dir, ver)
                if art is not None:
                    return art
                errors.append(f"by-version: {self.last_failure}")
            else:
                errors.append(err)

        # Only fall back to by-version when the origin isn't already
        # the index (avoids duplicate work + duplicate error text) and
        # isn't a git origin (a private commit won't be on the index).
        origin_is_index_or_vcs = direct_url is None or "vcs_info" in (direct_url or {})
        if errors and not origin_is_index_or_vcs:
            ok, err = self._materialize_by_version(ver, cache_dir)
            if ok:
                art = self._finalize(cache_dir, ver)
                if art is not None:
                    return art
                errors.append(f"fallback-by-version: {self.last_failure}")
            else:
                errors.append(f"fallback-by-version: {err}")

        self.last_failure = "; ".join(errors) if errors else "no origin metadata"
        return None

    def _finalize(self, cache_dir: Path, ver: str) -> WheelArtifact | None:
        """Return the wheel matching *ver* in *cache_dir*, or ``None``.

        Filters by version match rather than mtime alone so a stale wheel
        of a different version left in the shared cache directory by a
        prior provider run cannot be returned tagged with the current
        version.
        """
        whl = _newest_whl_for_version(cache_dir, _ENGINE_PKG, ver)
        if whl is None:
            self.last_failure = (
                f"materialize completed but no {_ENGINE_PKG}-{ver}-*.whl landed in {cache_dir}"
            )
            return None
        return WheelArtifact(
            path=whl,
            version=ver,
            content_hash=_hash_file(whl),
            provider_name=self.name,
        )

    @staticmethod
    def _materialize_git(url: str, commit: str, cache_dir: Path) -> tuple[bool, str]:
        """Clone *url* at *commit* into a temp dir + build a wheel into
        *cache_dir*. Returns ``(ok, stderr_tail)``.

        Uses a shallow ``--branch <ref>`` clone only when *commit* looks
        like a tag/branch name; for hex-SHA commit ids (the common PEP
        610 shape) ``--branch`` would be rejected by git, so we go
        straight to full clone + explicit checkout.
        """
        if shutil.which("git") is None:
            return False, "git not on PATH"

        with tempfile.TemporaryDirectory(prefix="tolokaforge-git-") as tmp:
            src = Path(tmp) / "src"
            if _looks_like_sha(commit):
                # Full clone then explicit checkout — git clone --branch
                # does not accept commit SHAs.
                ok, err = _run(["git", "clone", url, str(src)], timeout=180)
                if not ok:
                    return False, f"clone: {err}"
                ok, err = _run(["git", "-C", str(src), "checkout", commit], timeout=60)
                if not ok:
                    return False, f"checkout {commit[:12]}: {err}"
            else:
                # Tag/branch — shallow clone is safe and fast.
                ok, err = _run(
                    ["git", "clone", "--depth=1", "--branch", commit, url, str(src)],
                    timeout=180,
                )
                if not ok:
                    return False, f"clone --branch {commit}: {err}"

            # Build the wheel — uv first, pip fallback.
            ok, uv_err = _run_uv(
                ["build", "--wheel", "--out-dir", str(cache_dir), str(src)],
                timeout=300,
            )
            if ok:
                return True, ""
            ok, pip_err = _run_pip(
                ["wheel", str(src), "--no-deps", "-w", str(cache_dir)],
                timeout=300,
            )
            if ok:
                return True, ""
            return False, f"uv build: {uv_err}; pip wheel: {pip_err}"

    @staticmethod
    def _materialize_archive(url: str, cache_dir: Path) -> tuple[bool, str]:
        """Download *url* into *cache_dir*. Returns ``(ok, stderr_tail)``.

        Accepts any URL whose path ends in a recognized wheel/sdist
        extension (``.whl``, ``.tar.gz``, ``.tgz``, ``.zip``,
        ``.tar.bz2``, ``.tar.xz``) — the same set pip and uv accept as
        a direct-URL install.
        """
        parsed = urllib.parse.urlparse(url)
        filename = Path(parsed.path).name
        if not filename or not filename.endswith(_ARCHIVE_SUFFIXES):
            return False, f"URL path does not end in a wheel/sdist suffix: {url}"
        dest = cache_dir / filename
        try:
            urllib.request.urlretrieve(url, dest)  # noqa: S310
        except urllib.error.URLError as exc:
            return False, f"urlretrieve {url}: {exc}"
        except OSError as exc:
            return False, f"urlretrieve {url}: {exc}"
        if not dest.is_file() or dest.stat().st_size == 0:
            return False, "download produced empty file"
        return True, ""

    @staticmethod
    def _materialize_by_version(ver: str, cache_dir: Path) -> tuple[bool, str]:
        """Download the wheel from the configured index by name+version.

        Invokes ``python -m pip download --only-binary=:all:`` via
        :func:`_run_pip`, which auto-recovers a missing pip via
        ``ensurepip`` (the fix for uv envs that omit pip). Forces
        ``--only-binary`` so an sdist can never masquerade as a
        successful wheel download.

        ``uv pip download`` is intentionally not attempted: uv 0.9+ has
        no ``pip download`` subcommand — the pip path already handles
        the ``no pip installed`` case via ``ensurepip`` recovery.

        Returns ``(ok, stderr_tail)``.
        """
        spec = f"{_ENGINE_PKG}=={ver}"
        ok, err = _run_pip(
            [
                "download",
                spec,
                "--no-deps",
                "--only-binary=:all:",
                "-d",
                str(cache_dir),
            ],
            timeout=180,
        )
        if ok:
            return True, ""
        return False, f"pip download {spec}: {err}"


class PipDownloadWheelProvider(WheelProvider):
    """Downloads a wheel from PyPI as a last-resort fallback.

    Runs only when the installed distribution has no ``direct_url.json``
    (a plain PyPI install) and earlier providers all missed. Reuses
    :meth:`ReinstallWheelProvider._materialize_by_version` — a single
    implementation of the ``pip download --only-binary=:all:`` path
    (with ``ensurepip`` auto-recovery) so the two providers can't drift
    on flags, timeout, or error format.

    For git and archive-URL installs the earlier
    :class:`ReinstallWheelProvider` is authoritative, so this provider
    short-circuits with a reason on ``self.last_failure``.
    """

    name = "pip-download"
    priority = 30

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        ver = _installed_version()
        if ver is None:
            self.last_failure = f"{_ENGINE_PKG!r} is not installed in this environment"
            return None

        direct_url = _read_direct_url()
        if direct_url is not None and (
            "vcs_info" in direct_url or "archive_info" in direct_url or "dir_info" in direct_url
        ):
            self.last_failure = (
                f"skipped: install origin ({next(iter(direct_url.keys() - {'url'}))}) "
                "is owned by the reinstall provider"
            )
            logger.debug("%s: %s", self.name, self.last_failure)
            return None

        logger.info("%s: downloading wheel for %s==%s", self.name, _ENGINE_PKG, ver)
        ok, err = ReinstallWheelProvider._materialize_by_version(ver, cache_dir)
        if not ok:
            self.last_failure = err
            logger.warning("%s: %s", self.name, err)
            return None

        whl = _newest_whl_for_version(cache_dir, _ENGINE_PKG, ver)
        if whl is None:
            self.last_failure = (
                f"pip download reported success but no {_ENGINE_PKG}-{ver}-*.whl "
                f"landed in {cache_dir}"
            )
            return None

        return WheelArtifact(
            path=whl,
            version=ver,
            content_hash=_hash_file(whl),
            provider_name=self.name,
        )


# ---------------------------------------------------------------------------
# Wheel filename version matching
# ---------------------------------------------------------------------------


def _wheel_matches_version(whl: Path, ver: str) -> bool:
    """Check if a wheel filename matches the expected version.

    Wheel filenames use ``-`` as separators and normalize ``_`` for
    the distribution name.  E.g.:
    ``tolokaforge-0.2.0-py3-none-any.whl``
    """
    # Normalise: PEP 427 uses hyphens as field separators.
    parts = whl.name.split("-")
    if len(parts) < 2:
        return False
    # parts[0] is the distribution name, parts[1] is the version.
    return parts[1] == ver


# ---------------------------------------------------------------------------
# WheelResolver
# ---------------------------------------------------------------------------


_DEFAULT_PROVIDERS: tuple[type[WheelProvider], ...] = (
    LocalSourceWheelProvider,
    PipCacheWheelProvider,
    ReinstallWheelProvider,
    PipDownloadWheelProvider,
)


class WheelResolver:
    """Tries :class:`WheelProvider` instances in priority order.

    The default chain contains all four built-in providers.
    Use :meth:`register` to add custom providers after construction.
    """

    def __init__(
        self,
        providers: Sequence[WheelProvider] | None = None,
    ) -> None:
        if providers is not None:
            self._providers: list[WheelProvider] = list(providers)
        else:
            self._providers = [cls() for cls in _DEFAULT_PROVIDERS]
        self._sort()

    def register(self, provider: WheelProvider) -> WheelResolver:
        """Append *provider* and re-sort.  Returns ``self`` for chaining."""
        self._providers.append(provider)
        self._sort()
        return self

    def resolve(self, cache_dir: Path) -> WheelArtifact:
        """Walk the chain and return the first wheel found.

        Raises:
            NoWheelError: If every provider returns ``None``. The
                exception message includes each provider's recorded
                ``last_failure`` so the root cause is visible without
                re-running with DEBUG logging.
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        failures: list[tuple[str, str]] = []
        for p in self._providers:
            # Reset before each attempt so a stale reason from a prior
            # resolve() on a reused provider instance cannot leak into
            # this call's NoWheelError message.
            p.last_failure = ""
            artifact = p.provide(cache_dir)
            if artifact is not None:
                logger.info(
                    "Wheel resolver: '%s' provided %s (v%s, hash=%s)",
                    p.name,
                    artifact.path.name,
                    artifact.version,
                    artifact.content_hash[:12],
                )
                return artifact
            failures.append((p.name, p.last_failure or "no reason recorded"))

        uv_env = os.environ.get("UV_CACHE_DIR") or "unset"
        pip_env = os.environ.get("PIP_CACHE_DIR") or "unset"
        lines = [
            f"Could not resolve a {_ENGINE_PKG} wheel via any provider.",
            f"UV_CACHE_DIR={uv_env}, PIP_CACHE_DIR={pip_env}.",
            "Per-provider failure:",
        ]
        for name, reason in failures:
            lines.append(f"  {name}: {reason}")
        lines.append(
            "Remediation: install tolokaforge via `uv sync` / `pip install`, "
            "set UV_CACHE_DIR to a warm cache, or ensure the install origin "
            "(git URL / archive URL / PyPI index) is reachable."
        )
        raise NoWheelError("\n".join(lines))

    def _sort(self) -> None:
        self._providers.sort(key=lambda p: p.priority)

    def __repr__(self) -> str:  # pragma: no cover
        names = [p.name for p in self._providers]
        return f"<WheelResolver providers={names}>"


# ---------------------------------------------------------------------------
# Module-level convenience (cached)
# ---------------------------------------------------------------------------

_cached_artifact: WheelArtifact | None = None


def resolve_wheel(
    cache_dir: Path | None = None,
    *,
    force_refresh: bool = False,
) -> WheelArtifact:
    """Resolve the tolokaforge wheel, using a per-process cache.

    Args:
        cache_dir: Directory for cached wheels.  Defaults to
            ``.workbench/wheel-cache`` relative to CWD.
        force_refresh: Discard the cached result and re-resolve.

    Returns:
        A :class:`WheelArtifact` pointing at the wheel on disk.

    Raises:
        NoWheelError: When no provider can produce a wheel.
    """
    global _cached_artifact  # noqa: PLW0603
    if _cached_artifact is not None and not force_refresh:
        return _cached_artifact

    if cache_dir is None:
        cache_dir = Path(".workbench") / "wheel-cache"
    cache_dir = cache_dir.resolve()

    _cached_artifact = WheelResolver().resolve(cache_dir)
    return _cached_artifact
