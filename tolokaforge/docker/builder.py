"""Image Builder module for Docker Foundation Layer.

Provides a single source of truth for all project Docker image definitions
and utility functions for building images individually or in bulk.

Uses the foundation layer's ImageRegistry for content-hash caching.

Example:
    >>> from tolokaforge.docker.builder import build_all_images, build_image
    >>>
    >>> # Build all images
    >>> images = build_all_images()
    >>>
    >>> # Build core images only
    >>> images = build_all_images(core_only=True)
    >>>
    >>> # Build a single image
    >>> image = build_image("db-service")
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from tolokaforge.docker.image import Image
from tolokaforge.docker.registry import ImageRegistry
from tolokaforge.docker.wheel_resolver import resolve_wheel

logger = logging.getLogger(__name__)


def repo_root() -> Path:
    """Repository root, resolved relative to this module — independent of CWD."""
    return Path(__file__).resolve().parents[2]


def _pinned_python_version() -> str:
    """Read the pinned Python minor version from ``.python-version``.

    Single source of truth for the runtime Python version — dev, CI,
    devcontainer, and every runtime Docker image resolve from this file.
    Passed to Dockerfiles as the ``PYTHON_VERSION`` build arg so
    ``FROM python:${PYTHON_VERSION}-slim`` follows automatically. The
    Dockerfiles still default to the current pin so a manual
    ``docker build`` without the arg produces the same image.

    Resolution order:

    1. ``tolokaforge/_python_version.txt`` inside the installed package.
       Populated at wheel-build time by hatchling's ``force-include``
       (see ``pyproject.toml``). This is the path a wheel install sees
       — ``site-packages/tolokaforge/_python_version.txt``.
    2. ``.python-version`` at the repo root. This is the path a source
       checkout / editable install sees. The repo-root file is the
       single source of truth at *write* time; the packaged copy is a
       *build artifact* of it.

    Both branches read the same value on a matching install; the
    two-branch shape only exists to cover the wheel-install case where
    the repo-root dotfile is not available in ``site-packages``.
    """
    try:
        packaged = resources.files("tolokaforge").joinpath("_python_version.txt")
        if packaged.is_file():
            return packaged.read_text().strip()
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        # ``resources.files`` may raise when the package isn't fully
        # discoverable yet (e.g. during hatchling's own build
        # introspection, or on some editable-install layouts). Fall
        # through to the repo-root read.
        pass
    return (repo_root() / ".python-version").read_text().strip()


PYTHON_VERSION = _pinned_python_version()
_PYTHON_BUILD_ARGS: dict[str, str] = {"PYTHON_VERSION": PYTHON_VERSION}


#: Build-context entries the runner Dockerfile's ``hatchling build`` stage
#: needs, as repo-relative source-checkout paths. ``_runner_definition()``
#: maps each to a packaged copy when the repo root is not available.
_RUNNER_SOURCE_CONTEXT_FILES: list[str] = [
    "pyproject.toml",
    "README.md",
    "LICENSE",
    ".python-version",
    "scripts/hatch/",
    "tolokaforge/",
    # The runner-subset wheel declares ``tolokaforge-models>=1.0.0,<2.0.0``
    # as a Requires-Dist. Before the first ``models-vX.Y.Z`` PyPI publish
    # that pin resolves against no upstream, so the runner Dockerfile
    # builds the models wheel from source in the same wheel-builder stage
    # and installs both wheels together. Post-publish this remains the
    # bit-for-bit source of truth (docker installs the freshly-built wheel
    # from the checked-out tree, not whatever PyPI currently ships).
    "tolokaforge_models/",
]

#: Where the base wheel's ``force-include`` table lands the repo-root files
#: above, relative to the installed ``tolokaforge`` package. Keys are the
#: build-context names the Dockerfile ``COPY`` lines expect.
_PACKAGED_SUBSET_BUILD_DIR = "_subset_build"


# =============================================================================
# Image Definitions — Single Source of Truth
# =============================================================================
#
# Every service the project knows about appears in IMAGE_DEFINITIONS. Each
# entry holds the static facts (name, dockerfile, context) and, when the
# Dockerfile needs no host-side resolution, the static context_files too.
#
# The rag-service image still depends on the host-side wheel resolver
# (it installs a pre-built ``tolokaforge`` wheel into its rag service);
# it declares only its static fields here and pairs with ``_rag_definition``
# to augment the base with ``context_files`` and ``build_args`` at call
# time. ``get_image_definition`` routes to that factory; everything that
# only needs the static facts (reverse lookups, service enumeration, group
# membership) reads IMAGE_DEFINITIONS directly and never triggers wheel
# resolution as a side effect.
#
# The runner image is different: its Dockerfile is multi-stage
# (ADR-0025 § subset build target), producing the runner-subset wheel
# in-container via ``hatch build --target custom``. Its context is a source-
# tree slice rather than a resolved wheel, so it needs no host-side wheel
# resolution — but it DOES pair with ``_runner_definition()``, which picks
# between the repo-root paths (source checkout) and the packaged copies
# (wheel install, where the repo root is ``site-packages``).

IMAGE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "db-service": {
        "name": "tolokaforge-db-service",
        "dockerfile": "tolokaforge/docker/dockerfiles/db_service.Dockerfile",
        "context": ".",
        "context_files": [
            "tolokaforge/env/json_db_service/",
        ],
        "build_args": dict(_PYTHON_BUILD_ARGS),
    },
    "runner": {
        "name": "tolokaforge-runner",
        "dockerfile": "tolokaforge/docker/dockerfiles/runner.Dockerfile",
        "context": ".",
        # The runner Dockerfile is multi-stage: a wheel-builder stage runs
        # ``hatch build --target custom`` against the source tree in-container
        # to produce the subset wheel, and a builder stage installs it into
        # /opt/venv. The build context ships the sources hatch needs; the
        # subset wheel is a Docker-only artifact (ADR-0025).
        #
        # These entries are the *source-checkout* paths. On a wheel install
        # the repo-root files are absent from ``site-packages``, so
        # ``_runner_definition()`` swaps in the packaged copies — see that
        # factory and ``docs/adr/0025-runner-wheel-split.md``.
        "context_files": _RUNNER_SOURCE_CONTEXT_FILES,
        "build_args": dict(_PYTHON_BUILD_ARGS),
    },
    "rag-service": {
        "name": "tolokaforge-rag-service",
        "dockerfile": "tolokaforge/docker/dockerfiles/rag.Dockerfile",
        "context": ".",
        # context_files + build_args added by _rag_definition() at call time
    },
    "mock-web": {
        "name": "tolokaforge-mock-web",
        "dockerfile": "tolokaforge/docker/dockerfiles/mock_web.Dockerfile",
        "context": ".",
        "context_files": [
            "tolokaforge/env/mock_web_service/",
        ],
        "build_args": dict(_PYTHON_BUILD_ARGS),
    },
}

# Factories that augment a static base entry with wheel-resolver-dependent
# fields. Keyed by service name; absent for services whose full definition
# fits in IMAGE_DEFINITIONS verbatim.
_DYNAMIC_DEFINITIONS: dict[str, Callable[[], dict[str, Any]]] = {}

# Service groups for selective building
CORE_IMAGES: list[str] = ["db-service", "runner"]
EXTENDED_IMAGES: list[str] = ["rag-service", "mock-web"]

_ALL_KNOWN_SERVICES = frozenset(IMAGE_DEFINITIONS)


def _rag_definition() -> dict[str, Any]:
    """Augment the rag-service base entry with the resolved wheel + service files.

    The rag service needs both the tolokaforge wheel (for
    ``import tolokaforge.secrets``) and its own service files
    (``requirements.txt`` + ``app.py``).
    """
    artifact = resolve_wheel()
    return {
        **IMAGE_DEFINITIONS["rag-service"],
        "context_files": [
            str(artifact.path),  # wheel (absolute → flat copy)
            "tolokaforge/env/rag_service/",  # service files (relative)
        ],
        "build_args": {**_PYTHON_BUILD_ARGS, "WHEEL_FILENAME": artifact.path.name},
    }


_DYNAMIC_DEFINITIONS["rag-service"] = _rag_definition


def installed_package_dir() -> Path:
    """Directory of the installed ``tolokaforge`` package (its source root)."""
    return Path(__file__).resolve().parents[1]


def packaged_subset_build_dir() -> Path:
    """Directory holding the packaged copies of the runner's build inputs.

    The base wheel's ``force-include`` table copies the repo-root files the
    runner Dockerfile needs into ``tolokaforge/_subset_build/``. Same shape
    as the ``.python-version`` -> ``tolokaforge/_python_version.txt`` copy
    that :func:`_pinned_python_version` reads: the repo-root file stays the
    single source of truth at write time, the packaged copy is a build
    artifact of it.
    """
    return installed_package_dir() / _PACKAGED_SUBSET_BUILD_DIR


def _runner_definition() -> dict[str, Any]:
    """Resolve the runner build context for source-checkout OR wheel install.

    The Dockerfile's ``hatchling build --target custom`` stage needs the
    pyproject (which carries the ``[tool.hatch.build.targets.custom]``
    table), the metadata files that pyproject references, the custom builder
    script, and the ``tolokaforge`` sources. In a source checkout those sit
    at the repo root. Installed as a wheel they do NOT: ``repo_root()`` is
    ``site-packages``, so the repo-relative entries resolve to paths that do
    not exist and the build dies before any trial runs.

    Absolute context entries are copied flat into the build dir, so the
    packaged copies land under exactly the names the ``COPY`` lines expect.
    """
    if (repo_root() / "pyproject.toml").is_file():
        # Source checkout / editable install — repo-relative entries work.
        return dict(IMAGE_DEFINITIONS["runner"])

    packaged = packaged_subset_build_dir()
    pkg_dir = installed_package_dir()
    # Flat-copied absolutes land under their own basename, which is already
    # the name each COPY line expects. ``.python-version`` is the exception:
    # it ships as ``_python_version.txt`` (one force-include key per source),
    # so it needs an explicit destination.
    entries: list[Any] = [
        packaged / "pyproject.toml",
        packaged / "README.md",
        packaged / "LICENSE",
        packaged / "scripts",  # dir -> build_dir/scripts (holds hatch/)
        pkg_dir,  # dir -> build_dir/tolokaforge
        (pkg_dir / "_python_version.txt", ".python-version"),
    ]
    missing = [
        str(e[0] if isinstance(e, tuple) else e)
        for e in entries
        if not (e[0] if isinstance(e, tuple) else e).exists()
    ]
    if missing:
        raise FileNotFoundError(
            "Runner build context is incomplete for a wheel install. The base "
            "wheel must force-include the runner's subset-build inputs into "
            f"'tolokaforge/{_PACKAGED_SUBSET_BUILD_DIR}/' — missing: "
            f"{missing}. Rebuild the wheel from a pyproject that carries the "
            "[tool.hatch.build.targets.wheel.force-include] entries, or run "
            "from a source checkout."
        )
    return {
        **IMAGE_DEFINITIONS["runner"],
        "context_files": [(str(e[0]), e[1]) if isinstance(e, tuple) else str(e) for e in entries],
    }


_DYNAMIC_DEFINITIONS["runner"] = _runner_definition


def get_image_definition(service_name: str) -> dict[str, Any]:
    """Return the full image definition for *service_name*.

    For services with a dynamic factory, that factory is called (it may
    invoke ``resolve_wheel()``); otherwise the static entry is returned
    as-is.

    Raises:
        KeyError: If *service_name* is not a known service.
    """
    if service_name in _DYNAMIC_DEFINITIONS:
        return _DYNAMIC_DEFINITIONS[service_name]()
    if service_name in IMAGE_DEFINITIONS:
        return IMAGE_DEFINITIONS[service_name]
    raise KeyError(f"Unknown service '{service_name}'. Available: {sorted(_ALL_KNOWN_SERVICES)}")


def service_name_for_image(image_name: str) -> str | None:
    """Return the service whose definition produces ``image_name`` (without tag).

    Reads IMAGE_DEFINITIONS directly — every service's image name lives there
    as a static field, even for services with a dynamic factory, so no wheel
    resolution is triggered as a side effect of an unrelated lookup.
    """
    for svc, defn in IMAGE_DEFINITIONS.items():
        if defn["name"] == image_name:
            return svc
    return None


def build_all_images(
    core_only: bool = False,
    force: bool = False,
    registry: ImageRegistry | None = None,
) -> dict[str, Image]:
    """Build all project Docker images.

    Uses ImageRegistry for content-hash caching. Only rebuilds when
    Dockerfile or context files change.

    Args:
        core_only: If True, only build core images (db-service + runner).
        force: If True, force rebuild even if cached.
        registry: Optional ImageRegistry instance (creates new if None).

    Returns:
        Dictionary mapping service name to built Image.

    Example:
        >>> images = build_all_images(core_only=True)
        >>> images["db-service"].full_tag
        'tolokaforge-db-service:a3b8f2c1'
    """
    if registry is None:
        registry = ImageRegistry()

    services_to_build = list(CORE_IMAGES)
    if not core_only:
        services_to_build.extend(EXTENDED_IMAGES)

    logger.info(
        "Building %d images (core_only=%s, force=%s)",
        len(services_to_build),
        core_only,
        force,
    )

    images: dict[str, Image] = {}
    failed: list[str] = []

    for service_name in services_to_build:
        try:
            image = build_image(service_name, registry=registry, force=force)
            images[service_name] = image
            logger.info("✓ Built %s → %s", service_name, image.full_tag)
        except Exception as e:
            logger.error("✗ Failed to build %s: %s", service_name, e)
            failed.append(service_name)

    if failed:
        logger.warning(
            "Image build completed with %d failure(s): %s",
            len(failed),
            failed,
        )
    else:
        logger.info("All %d images built successfully", len(images))

    return images


def build_image(
    service_name: str,
    registry: ImageRegistry | None = None,
    force: bool = False,
) -> Image:
    """Build a single service image.

    Args:
        service_name: Name of the service (key in IMAGE_DEFINITIONS).
        registry: Optional ImageRegistry instance.
        force: If True, force rebuild.

    Returns:
        Built Image instance.

    Raises:
        KeyError: If service_name is not in IMAGE_DEFINITIONS.
        ImageError: If build fails.

    Example:
        >>> image = build_image("db-service")
        >>> image.exists()
        True
    """
    with _prepared_build_context(service_name) as (dockerfile, context, name, build_args):
        if force:
            logger.info("Force building image for '%s'", service_name)
            return Image.build(
                dockerfile=dockerfile,
                context=context,
                name=name,
                build_args=build_args,
            )

        if registry is None:
            registry = ImageRegistry()

        return registry.get_or_build(
            name=name,
            dockerfile=dockerfile,
            context=context,
            build_args=build_args,
        )


@contextmanager
def _prepared_build_context(
    service_name: str,
) -> Iterator[tuple[str, str, str, dict[str, str]]]:
    """Yield ``(dockerfile, context, name, build_args)`` for a service build.

    For services that declare ``context_files`` (runner, rag-service) this
    assembles an isolated temp build context and removes it on exit; otherwise
    it yields the static repo-relative paths. Both ``build_image`` and
    ``expected_image_ref`` consume this so a real build and its predicted ref
    hash exactly the same inputs.
    """
    definition = get_image_definition(service_name)
    context_files = definition.get("context_files", [])
    build_args = definition.get("build_args") or {}
    name = definition["name"]

    if not context_files:
        yield definition["dockerfile"], definition["context"], name, build_args
        return

    build_context = assemble_build_context(
        repo_root=repo_root(),
        dockerfile=definition["dockerfile"],
        context_files=context_files,
    )
    try:
        dockerfile_path = build_context / definition["dockerfile"]
        yield str(dockerfile_path), str(build_context), name, build_args
    finally:
        shutil.rmtree(build_context, ignore_errors=True)


def expected_image_ref(service_name: str) -> str:
    """The exact ``name:tag`` that ``build_image(service_name)`` would assign.

    Computed via the same context-assembly + content-hash path a real build
    uses (``Image.expected_ref`` shares ``Image._content_hash_and_tag`` with
    ``Image.build``), so the returned ref matches a real build by construction.
    Does not build.
    """
    with _prepared_build_context(service_name) as (dockerfile, context, name, build_args):
        return Image.expected_ref(
            dockerfile=dockerfile,
            context=context,
            name=name,
            build_args=build_args,
        )


def assemble_build_context(
    repo_root: Path,
    dockerfile: str,
    context_files: Sequence[str | tuple[str, str]],
) -> Path:
    """Create a temporary build directory with only the declared files.

    Instead of using the entire repo as Docker build context, this function
    creates a self-contained temp directory with only the files needed for
    the build. This makes content hashing deterministic and builds faster.

    Args:
        repo_root: Repository root directory.
        dockerfile: Path to Dockerfile (relative to repo_root).
        context_files: File/directory paths to include. Each entry is
            either a path (relative to repo_root, or absolute -> copied
            flat under its basename) or a ``(source, destination)`` pair
            when the context name must differ from the source basename.

    Returns:
        Path to temporary build directory. The caller owns the directory and
        must remove it with ``shutil.rmtree`` (typically inside a ``finally``).

    Raises:
        FileNotFoundError: If a declared file or directory does not exist.
    """
    build_dir = Path(tempfile.mkdtemp(prefix="tolokaforge-build-"))

    # Copy Dockerfile
    src_dockerfile = repo_root / dockerfile
    dst_dockerfile = build_dir / dockerfile
    dst_dockerfile.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_dockerfile, dst_dockerfile)

    # Copy declared context files.
    # Paths may be relative (resolved against repo_root) or absolute
    # (e.g. a wheel from the wheel-cache — copied flat into build_dir).
    # An entry may also be a ``(source, destination)`` pair when the name the
    # Dockerfile expects differs from the source basename — used by
    # ``_runner_definition()`` to land the packaged ``_python_version.txt``
    # back under its repo-root dotfile name.
    for entry in context_files:
        if isinstance(entry, tuple):
            src_spec, dest_rel = entry
            src = Path(src_spec)
            if not src.is_absolute():
                src = repo_root / src
            if not src.is_file():
                shutil.rmtree(build_dir, ignore_errors=True)
                raise FileNotFoundError(
                    f"Declared context path not found: {src_spec} -> {dest_rel}"
                )
            dst = build_dir / dest_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            continue
        entry_path = Path(entry)
        if entry_path.is_absolute():
            # Absolute path: copy the file flat into the build dir root.
            if entry_path.is_file():
                shutil.copy2(entry_path, build_dir / entry_path.name)
            elif entry_path.is_dir():
                shutil.copytree(
                    entry_path,
                    build_dir / entry_path.name,
                    dirs_exist_ok=True,
                )
            else:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise FileNotFoundError(f"Declared absolute context path not found: {entry}")
        else:
            # Relative path: resolve against repo_root (original behavior).
            src = repo_root / entry
            dst = build_dir / entry
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            else:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise FileNotFoundError(
                    f"Declared context path not found: {entry} (resolved to {src})"
                )

    return build_dir


def list_built_images(
    registry: ImageRegistry | None = None,
) -> list[dict[str, str]]:
    """List status of all project images.

    Returns a list of dicts with service name, image tag, and whether
    the image exists in the local Docker daemon.

    Args:
        registry: Optional ImageRegistry to check cache state.

    Returns:
        List of status dictionaries with keys: service, image_name,
        dockerfile, status.

    Example:
        >>> statuses = list_built_images()
        >>> for s in statuses:
        ...     print(f"{s['service']}: {s['status']}")
    """
    statuses: list[dict[str, str]] = []

    for service_name, definition in IMAGE_DEFINITIONS.items():
        status_info: dict[str, str] = {
            "service": service_name,
            "image_name": definition["name"],
            "dockerfile": definition["dockerfile"],
            "status": "not_built",
        }

        if registry:
            images = registry.get_images_by_name(definition["name"])
            if images:
                status_info["status"] = "cached"
                status_info["tag"] = images[0].full_tag
            else:
                status_info["status"] = "not_cached"

        statuses.append(status_info)

    return statuses
