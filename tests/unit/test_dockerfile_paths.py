"""Unit tests for Dockerfile COPY path validation.

Ensures that all COPY source paths referenced in Dockerfiles actually exist
in the repository root. This catches stale paths that would cause Docker
build failures.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]  # tests/unit -> repo root
DOCKER_DIR = REPO_ROOT / "docker"


def parse_copy_sources(dockerfile_path: Path) -> list[tuple[str, int, str]]:
    """Parse COPY source paths from a Dockerfile.

    Returns list of (source_path, line_number, dockerfile_name) tuples.
    """
    results = []
    with open(dockerfile_path) as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped.startswith("COPY"):
                continue
            # Skip multi-stage COPY --from=...
            if "--from=" in stripped:
                continue
            # Skip comments
            if stripped.startswith("#"):
                continue
            # Extract source path (first non-flag arg after COPY)
            parts = stripped.split()
            # Remove flags like --chown=...
            args = [p for p in parts[1:] if not p.startswith("--")]
            if len(args) >= 2:
                source = args[0]
                # Skip variable references and globs
                if "$" in source or "*" in source:
                    continue
                results.append((source, lineno, dockerfile_path.name))
    return results


@pytest.mark.unit
def test_dockerfile_copy_sources_exist():
    """Verify all Dockerfile COPY source paths reference existing repo paths.

    This test catches stale COPY instructions that reference directories or
    files that have been moved, renamed, or removed. Such paths would cause
    Docker build failures at COPY time.
    """
    dockerfiles = sorted(DOCKER_DIR.glob("*.Dockerfile"))
    assert dockerfiles, f"No Dockerfiles found in {DOCKER_DIR}"

    missing = []
    for dockerfile in dockerfiles:
        sources = parse_copy_sources(dockerfile)
        for source, lineno, fname in sources:
            source_path = REPO_ROOT / source
            if not source_path.exists():
                missing.append(f"  {fname}:{lineno} — COPY {source} (not found)")

    if missing:
        details = "\n".join(missing)
        pytest.fail(
            f"Dockerfile COPY instructions reference {len(missing)} non-existent "
            f"path(s):\n{details}\n\n"
            f"Either update the COPY paths or create the missing directories."
        )
