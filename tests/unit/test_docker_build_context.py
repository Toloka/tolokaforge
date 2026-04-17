"""Unit tests for Docker build context isolation.

Tests assemble_build_context() creates isolated directories with only declared files,
and that content hashes are stable across unrelated changes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tolokaforge.docker.image import Image

pytestmark = pytest.mark.unit


def test_assemble_build_context_contains_only_declared_files(tmp_path: Path) -> None:
    """assemble_build_context() creates temp dir with only declared files."""
    # Create a mock repo structure
    repo = tmp_path / "repo"
    repo.mkdir()

    # Create files
    (repo / "pyproject.toml").write_text("name = 'test'")
    (repo / "README.md").write_text("# Test")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('hello')")
    (repo / "results").mkdir()
    (repo / "results" / "run.log").write_text("should not be included")
    (repo / "plans").mkdir()
    (repo / "plans" / "plan.md").write_text("should not be included")

    # Create a Dockerfile
    dockerfile = repo / "Dockerfile"
    dockerfile.write_text("FROM alpine\nCOPY src/ ./src/\n")

    from tolokaforge.docker.builder import assemble_build_context

    build_dir = assemble_build_context(
        repo_root=repo,
        dockerfile="Dockerfile",
        context_files=["pyproject.toml", "src/"],
    )

    try:
        # Declared files should be present
        assert (build_dir / "pyproject.toml").exists()
        assert (build_dir / "src" / "main.py").exists()
        assert (build_dir / "Dockerfile").exists()

        # Undeclared files should NOT be present
        assert not (build_dir / "README.md").exists()
        assert not (build_dir / "results").exists()
        assert not (build_dir / "plans").exists()
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def test_isolated_context_hash_stable_across_unrelated_changes(
    tmp_path: Path,
) -> None:
    """Content hash of isolated context is not affected by files outside the context."""
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('hello')")
    dockerfile = repo / "Dockerfile"
    dockerfile.write_text("FROM alpine\nCOPY src/ ./src/\n")

    from tolokaforge.docker.builder import assemble_build_context

    # Build context 1
    ctx1 = assemble_build_context(repo, "Dockerfile", ["src/"])
    hash1 = Image._compute_content_hash(ctx1 / "Dockerfile", ctx1, {})

    # Add unrelated file to repo
    (repo / "results").mkdir()
    (repo / "results" / "output.log").write_text("some output")

    # Build context 2 — same declared files
    ctx2 = assemble_build_context(repo, "Dockerfile", ["src/"])
    hash2 = Image._compute_content_hash(ctx2 / "Dockerfile", ctx2, {})

    assert hash1 == hash2, "Hash should be stable when unrelated files change"

    shutil.rmtree(ctx1, ignore_errors=True)
    shutil.rmtree(ctx2, ignore_errors=True)
