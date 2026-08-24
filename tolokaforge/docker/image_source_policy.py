"""Pull-vs-build decision policy for first-party service images.

Resolves the two-value concrete outcome (``"pull"`` or ``"build"``)
from the three-value config request (``"auto"``, ``"pull"``, ``"build"``)
plus the engine's install shape and version. Pure function — no
filesystem, no Docker SDK, no logging. The caller passes the two facts
it needs to know, and the caller (``stack._build_one_image``) is
responsible for the side effect that follows.

Semantics summary (see #1068 and ADR-0025 for the full context):

- Explicit ``"pull"`` or ``"build"`` always wins. That's the whole
  point of an escape hatch.
- ``"auto"`` falls through to a shape-based decision: pull only when we
  are running from a wheel install AND we have a concrete version to
  form a pull tag with. Otherwise build.
- The sentinel ``"0.0.0+unknown"`` (the value ``tolokaforge/__init__.py``
  sets when ``importlib.metadata.version("tolokaforge")`` raises
  ``PackageNotFoundError`` — a source checkout that hasn't been
  ``pip install``-ed) is the "no valid pull tag exists" case: fall
  through to build in ``auto`` mode.
- Any other version string is treated as pull-eligible in ``auto`` mode.
  Pre-release / dev / local versions (e.g. ``0.18.0.dev5``,
  ``0.18.0+dirty``) resolve to ``pull`` here; the pull itself will
  ``ImagePullError(kind="tag_missing")`` and the caller falls back to
  build with a warning. Keeping that policy in the caller — not here —
  is deliberate: the pull attempt is the *authoritative* check for
  whether an image exists at Docker Hub, and we do not want a stale
  denylist in this module to lie about it.
"""

from __future__ import annotations

from typing import Literal

ImageSource = Literal["auto", "pull", "build"]
ResolvedImageSource = Literal["pull", "build"]


class RunnerDockerCliUnavailableError(RuntimeError):
    """A run needs docker CLI inside the runner container but the resolved
    image source would ship a runner without it.

    Raised at orchestrator startup so the operator sees an actionable message
    instead of a trial dying opaquely on the first tool call with
    ``[Errno 2] No such file or directory: 'docker'``. The runner Dockerfile
    installs the docker CLI + compose plugin behind ``INSTALL_DOCKER_CLI=true``
    only when the image is built locally; pulled images ship without it (the
    published runners keep the smallest footprint that fits the common case).
    """


UNKNOWN_VERSION_SENTINEL = "0.0.0+unknown"
"""The value ``tolokaforge.__version__`` reports when the engine is
running from a source checkout that has not been ``pip install``-ed
(``importlib.metadata`` raises ``PackageNotFoundError``). Kept in this
module so both the policy and its tests can reference it symbolically —
a rename in ``tolokaforge/__init__.py`` will fail the test in
``tests/unit/test_image_source_policy.py`` that pins the two together.
"""


def resolve_image_source(
    *,
    request: ImageSource,
    is_wheel_install: bool,
    engine_version: str,
) -> ResolvedImageSource:
    """Resolve the tri-valued config request to a two-valued policy.

    Args:
        request: The tri-valued input, from
            :attr:`tolokaforge.core.models.docker_config.DockerConfig.image_source`.
        is_wheel_install: ``True`` when the running engine is a
            ``pip install``-ed wheel (no ``pyproject.toml`` alongside
            :func:`tolokaforge.docker.builder.repo_root`), ``False`` for
            a source checkout.
        engine_version: The engine's reported version, from
            :data:`tolokaforge.__version__`. The unknown-version
            sentinel is :data:`UNKNOWN_VERSION_SENTINEL`.

    Returns:
        ``"pull"`` when the caller should attempt a Docker Hub pull;
        ``"build"`` when the caller should build locally.
    """
    if request == "pull":
        return "pull"
    if request == "build":
        return "build"
    # request == "auto"
    if not is_wheel_install:
        return "build"
    if engine_version == UNKNOWN_VERSION_SENTINEL:
        return "build"
    # PEP 440 local-version segment (``+something``) is not a legal
    # Docker tag character (Docker tags match ``[A-Za-z0-9_.-]{1,128}``).
    # An editable install or a hatch build from a dirty tree emits
    # versions like ``0.18.0+dirty``, ``0.18.0+editable``, or
    # ``0.18.0+abcdef.dirty``; feeding these to Image.pull would produce
    # a client-side tag-format error that the pull-path error handler
    # would misclassify as ``unreachable`` (no HTTP round-trip, so no
    # status code). Route these to build in ``auto`` mode instead — the
    # local version means "this bit of code is not what's on Docker Hub"
    # by definition, so building is the right choice. Explicit ``pull``
    # still tries; the resulting tag-format error surfaces as a hard
    # failure that matches the operator's stated intent.
    if "+" in engine_version:
        return "build"
    return "pull"


def check_runner_docker_cli_available(
    *,
    needs_docker_cli: bool,
    request: ImageSource,
    is_wheel_install: bool,
    engine_version: str,
) -> None:
    """Fail loud when the run needs docker CLI in the runner but the resolved
    image source would pull a runner image without it.

    Args:
        needs_docker_cli: The orchestrator's decision from
            ``_run_needs_docker_cli(adapter_type, tasks)``. When ``False`` the
            check is a no-op.
        request: The three-valued config request, same input
            :func:`resolve_image_source` takes.
        is_wheel_install: Same input :func:`resolve_image_source` takes.
        engine_version: Same input :func:`resolve_image_source` takes.

    Raises:
        RunnerDockerCliUnavailableError: when ``needs_docker_cli`` is ``True``
            and :func:`resolve_image_source` returns ``"pull"``. The message
            names the resolved request and points at the three ways to switch
            to a locally-built runner.
    """
    if not needs_docker_cli:
        return
    resolved = resolve_image_source(
        request=request,
        is_wheel_install=is_wheel_install,
        engine_version=engine_version,
    )
    if resolved == "build":
        return
    raise RunnerDockerCliUnavailableError(
        "This run needs the docker CLI + docker-compose plugin inside the "
        "runner container (the terminal-bench adapter or a compose-variant "
        "tool shells out to `docker exec` to reach the task container), but "
        f"the resolved image source is 'pull' (docker.image_source={request!r}"
        f", wheel_install={is_wheel_install}). Published runner images ship "
        "without docker CLI, so the first tool call would die with `[Errno 2] "
        "No such file or directory: 'docker'`. "
        "Fix: rerun with `--image-source build`, set "
        "`docker.image_source: build` in the run config, or set "
        "`TOLOKAFORGE_IMAGE_SOURCE=build` in the environment. A locally "
        "built runner honours the Dockerfile's INSTALL_DOCKER_CLI=true "
        "build arg that the orchestrator sets for this run."
    )
