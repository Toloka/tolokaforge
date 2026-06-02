"""Wheel-based Docker provisioning for tolokaforge.

Determines how to provision a ``tolokaforge`` wheel on the host so the
Docker builder can ``COPY`` it into the runner image.  The container
always installs from a wheel — no conditional Dockerfile logic needed.

Three providers are tried in priority order:

1. **LocalSourceWheelProvider** — builds a wheel from a local source
   checkout (cached by source-tree SHA-256).
2. **PipCacheWheelProvider** — locates a wheel that pip or uv already
   built in their local caches.
3. **PipDownloadWheelProvider** — downloads a wheel from PyPI as a
   last-resort fallback.

Public API
----------
- :func:`resolve_wheel` — cached, module-level convenience.
- :class:`WheelResolver` — explicit chain construction + resolution.
- :class:`WheelArtifact` — frozen description of the located wheel.
- :class:`WheelProvider` — abstract base for new providers.

Extension point
~~~~~~~~~~~~~~~
Subclass :class:`WheelProvider`, set ``name`` and ``priority``, implement
:meth:`provide`, and register via ``WheelResolver.register()``.
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
    """

    name: str = ""
    """Human-readable identifier (for logging and error messages)."""

    priority: int = 100
    """Lower values are tried first."""

    @abstractmethod
    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        """Locate or build a wheel, placing it under *cache_dir*.

        Return a :class:`WheelArtifact` on success, or ``None`` to
        yield to the next provider in the chain.
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
    """uv cache roots to search, most-specific first, de-duplicated.

    This is *location* discovery, not layout: it honors ``UV_CACHE_DIR`` and
    ``uv cache dir`` (which also covers ``XDG_CACHE_HOME`` / uv config), falling
    back to the default ``~/.cache/uv``.  CI tools such as ``astral-sh/setup-uv``
    relocate the cache via ``UV_CACHE_DIR``, so a hard-coded ``~/.cache/uv`` is
    not sufficient.  The on-disk *layout* within a root is uv-internal and is
    handled by :func:`_walk_pip_wheel_caches`.
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

    @staticmethod
    def _build_wheel(source_root: Path, out_dir: Path) -> None:
        """Build a wheel from *source_root* into *out_dir*.

        Tries ``uv build`` first (handles build isolation internally),
        then falls back to ``pip wheel`` for environments without uv.
        """
        if shutil.which("uv"):
            subprocess.check_call(
                [
                    "uv",
                    "build",
                    "--wheel",
                    "--out-dir",
                    str(out_dir),
                    str(source_root),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        else:
            # Fallback: pip wheel handles build isolation via pyproject.toml
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--wheel-dir",
                    str(out_dir),
                    str(source_root),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )


class PipCacheWheelProvider(WheelProvider):
    """Locates a tolokaforge wheel from the pip/uv wheel cache.

    Used when tolokaforge was installed via ``pip install`` or
    ``uv sync`` — the installer already built/downloaded a wheel and
    cached it locally.
    """

    name = "pip-cache"
    priority = 20

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        ver = _installed_version()
        if ver is None:
            return None

        for whl in _walk_pip_wheel_caches():
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

        return None


class PipDownloadWheelProvider(WheelProvider):
    """Downloads a wheel from PyPI as a last-resort fallback.

    Only fires for PyPI installs (not git installs, which won't have
    a matching release on PyPI).
    """

    name = "pip-download"
    priority = 30

    def provide(self, cache_dir: Path) -> WheelArtifact | None:
        ver = _installed_version()
        if ver is None:
            return None

        # Don't attempt PyPI download for git installs.
        direct_url = _read_direct_url()
        if direct_url is not None and "vcs_info" in direct_url:
            logger.debug(
                "%s: skipping — installed from git, not PyPI",
                self.name,
            )
            return None

        logger.info(
            "%s: downloading wheel for %s==%s from PyPI",
            self.name,
            _ENGINE_PKG,
            ver,
        )
        try:
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    f"{_ENGINE_PKG}=={ver}",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--dest",
                    str(cache_dir),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "%s: pip download failed (exit %d)",
                self.name,
                exc.returncode,
            )
            return None

        whl = _newest_whl(cache_dir, _ENGINE_PKG)
        if whl is None:
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
    PipDownloadWheelProvider,
)


class WheelResolver:
    """Tries :class:`WheelProvider` instances in priority order.

    The default chain contains all three built-in providers.
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
            NoWheelError: If every provider returns ``None``.
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        attempted: list[str] = []
        for p in self._providers:
            artifact = p.provide(cache_dir)
            attempted.append(p.name)
            if artifact is not None:
                logger.info(
                    "Wheel resolver: '%s' provided %s (v%s, hash=%s)",
                    p.name,
                    artifact.path.name,
                    artifact.version,
                    artifact.content_hash[:12],
                )
                return artifact

        searched = ", ".join(str(p) for p in (*_uv_cache_bases(), *_pip_wheel_cache_bases()))
        uv_env = "set" if os.environ.get("UV_CACHE_DIR") else "unset"
        pip_env = "set" if os.environ.get("PIP_CACHE_DIR") else "unset"
        raise NoWheelError(
            f"No provider could produce a tolokaforge wheel.  "
            f"Tried providers: {', '.join(attempted)}.  "
            f"Searched wheel caches: {searched} "
            f"(UV_CACHE_DIR={uv_env}, PIP_CACHE_DIR={pip_env}).  "
            f"Set UV_CACHE_DIR/PIP_CACHE_DIR, clone the repo, or install via pip/uv."
        )

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
