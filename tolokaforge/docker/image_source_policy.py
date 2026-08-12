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
            :attr:`tolokaforge.docker.config.DockerConfig.image_source`.
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
    return "pull"
