"""Build contexts exclude stale outputs from sibling package directories."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tolokaforge.docker.builder import assemble_build_context

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("absolute_context", [False, True])
def test_assemble_build_context_omits_nested_build_artifacts(
    tmp_path: Path, absolute_context: bool
) -> None:
    """Both directory-copy paths omit stale dist, build, and egg-info outputs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM alpine\n")

    models = repo / "tolokaforge_models"
    (models / "src").mkdir(parents=True)
    (models / "src" / "model.py").write_text("VALUE = 1\n")
    (models / "dist").mkdir()
    (models / "dist" / "stale.whl").write_bytes(b"stale wheel")
    (models / "pkg" / "build").mkdir(parents=True)
    (models / "pkg" / "build" / "generated.py").write_text("stale = True\n")
    (models / "pkg" / "example.egg-info").mkdir()
    (models / "pkg" / "example.egg-info" / "PKG-INFO").write_text("stale metadata\n")

    context_entry = models if absolute_context else "tolokaforge_models"
    build_dir = assemble_build_context(repo, "Dockerfile", [context_entry])
    try:
        staged_models = build_dir / "tolokaforge_models"
        assert (staged_models / "src" / "model.py").is_file()
        assert not (staged_models / "dist").exists()
        assert not (staged_models / "pkg" / "build").exists()
        assert not (staged_models / "pkg" / "example.egg-info").exists()
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
