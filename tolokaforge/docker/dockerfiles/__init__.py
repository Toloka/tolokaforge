"""Docker infrastructure files bundled as package data.

When tolokaforge is installed as a wheel (pip/uv), the Dockerfiles live here
inside the package so they're always available — regardless of the working
directory.

Usage::

    from tolokaforge.docker.dockerfiles import get_dockerfile_path
    path = get_dockerfile_path("db_service")
    # -> /path/to/site-packages/tolokaforge/docker/dockerfiles/db_service.Dockerfile
"""

from __future__ import annotations

from pathlib import Path

_DOCKERFILES_DIR = Path(__file__).resolve().parent


def get_dockerfile_path(name: str) -> str:
    """Return the absolute path to a bundled Dockerfile.

    Args:
        name: Dockerfile stem (e.g. "db_service", "runner", "rag", "mock_web").
              The ".Dockerfile" suffix is added automatically.

    Returns:
        Absolute path as a string.

    Raises:
        FileNotFoundError: If the Dockerfile doesn't exist in the package.
    """
    path = _DOCKERFILES_DIR / f"{name}.Dockerfile"
    if not path.exists():
        available = sorted(p.stem for p in _DOCKERFILES_DIR.glob("*.Dockerfile"))
        raise FileNotFoundError(
            f"Dockerfile '{name}.Dockerfile' not found in package. Available: {available}"
        )
    return str(path)
