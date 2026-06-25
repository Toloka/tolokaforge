"""Unit tests for Docker build context isolation.

Tests assemble_build_context() creates isolated directories with only declared files,
and that content hashes are stable across unrelated changes.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from tolokaforge.docker.builder import (
    build_image,
    get_image_definition,
    service_name_for_image,
)
from tolokaforge.docker.image import Image
from tolokaforge.docker.stack import ServiceDefinition, ServiceStack
from tolokaforge.docker.wheel_resolver import WheelArtifact

pytestmark = pytest.mark.unit


@pytest.fixture()
def _mock_wheel(tmp_path: Path):
    """Mock resolve_wheel to return a fake .whl so tests don't run a real build."""
    whl = tmp_path / "tolokaforge-0.2.0-py3-none-any.whl"
    whl.write_bytes(b"PK\x03\x04test-wheel")
    artifact = WheelArtifact(
        path=whl,
        version="0.2.0",
        content_hash="testhash",
        provider_name="test",
    )
    with (
        patch(
            "tolokaforge.docker.wheel_resolver.resolve_wheel",
            return_value=artifact,
        ),
        patch(
            "tolokaforge.docker.builder.resolve_wheel",
            return_value=artifact,
        ),
        patch(
            "tolokaforge.docker.stacks.core.resolve_wheel",
            return_value=artifact,
        ),
    ):
        yield artifact


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


def test_assemble_build_context_raises_on_missing_declared_path(tmp_path: Path) -> None:
    """Declared context paths that don't exist must fail loudly, not silently
    produce a malformed temp build dir whose docker build later fails far from
    the cause.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM alpine\n")
    (repo / "pyproject.toml").write_text("name = 'test'")

    from tolokaforge.docker.builder import assemble_build_context

    with pytest.raises(FileNotFoundError, match="missing/file"):
        assemble_build_context(
            repo_root=repo,
            dockerfile="Dockerfile",
            context_files=["pyproject.toml", "missing/file"],
        )


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


@pytest.mark.usefixtures("_mock_wheel")
def test_runner_build_context_contains_wheel() -> None:
    """The runner image context should contain the resolved wheel (not source)."""
    runner_def = get_image_definition("runner")
    context_files = runner_def["context_files"]
    # Exactly one entry: the absolute path to the .whl.
    assert len(context_files) == 1
    assert context_files[0].endswith(".whl")


def test_build_image_respects_context_files(monkeypatch, _mock_wheel) -> None:
    """build_image() should place the resolved wheel in the Docker context."""
    captured = {}

    def fake_build(dockerfile, context, build_args=None, name=None, client=None):
        captured["dockerfile"] = dockerfile
        captured["context"] = context
        captured["build_args"] = build_args
        root = Path(context)

        # The wheel should be in the build context root.
        wheels = list(root.glob("tolokaforge-*.whl"))
        assert len(wheels) == 1, f"Expected one wheel, found: {wheels}"
        # Source package should NOT be present (the tolokaforge/ dir may
        # exist as a parent of the Dockerfile path on the split branch,
        # but it must not contain the Python source).
        assert not (root / "pyproject.toml").exists()
        assert not (root / "tolokaforge" / "__init__.py").exists()

        return Image(
            name=name or "test",
            tag="deadbeef",
            image_id="dummy",
            dockerfile=dockerfile,
            context=context,
            context_hash="deadbeef",
            build_args=build_args or {},
        )

    monkeypatch.setattr(Image, "build", fake_build)

    image = build_image("runner", force=True)
    assert image.full_tag == "tolokaforge-runner:deadbeef"
    assert not Path(captured["context"]).exists(), "Temporary build context should be cleaned up"
    # WHEEL_FILENAME must reach docker build. The Dockerfile's ARG default
    # is a placeholder that doesn't match the real wheel on disk, so `COPY
    # ${WHEEL_FILENAME}` fails whenever the harness omits this build arg.
    assert captured["build_args"] == {"WHEEL_FILENAME": _mock_wheel.path.name}


def test_build_image_non_force_path_uses_isolated_context(monkeypatch, _mock_wheel) -> None:
    """The cached (non-force) path must also assemble the isolated context.

    Production builds go through ``ImageRegistry.get_or_build``.
    """
    captured: dict[str, object] = {}

    def fake_get_or_build(self, *, name, dockerfile, context, build_args=None):  # noqa: ARG001
        captured["dockerfile"] = dockerfile
        captured["context"] = context
        captured["build_args"] = build_args
        root = Path(context)
        # Wheel should be present in the isolated context.
        assert list(root.glob("tolokaforge-*.whl"))
        return Image(
            name=name,
            tag="cafe1234",
            image_id="dummy",
            dockerfile=dockerfile,
            context=context,
            context_hash="cafe1234",
            build_args=build_args or {},
        )

    from tolokaforge.docker.registry import ImageRegistry

    monkeypatch.setattr(ImageRegistry, "get_or_build", fake_get_or_build)

    image = build_image("runner")
    assert image.full_tag == "tolokaforge-runner:cafe1234"
    assert not Path(captured["context"]).exists(), "Temporary build context should be cleaned up"
    assert captured["build_args"] == {"WHEEL_FILENAME": _mock_wheel.path.name}


def test_build_image_passes_no_build_args_when_definition_has_none(monkeypatch) -> None:
    """Services whose definition has no ``build_args`` entry (e.g. db-service)
    must receive ``build_args=None`` — not ``{}`` — so the content hash and
    docker build call are bit-identical to the pre-feature behaviour.
    """
    captured: dict[str, object] = {}

    def fake_build(dockerfile, context, build_args=None, name=None, client=None):
        captured["build_args"] = build_args
        return Image(
            name=name or "test",
            tag="deadbeef",
            image_id="dummy",
            dockerfile=dockerfile,
            context=context,
            context_hash="deadbeef",
            build_args=build_args or {},
        )

    monkeypatch.setattr(Image, "build", fake_build)

    build_image("db-service", force=True)
    assert captured["build_args"] is None


@pytest.mark.usefixtures("_mock_wheel")
def test_service_name_for_image_resolves_all_known_services() -> None:
    """Reverse lookup must cover dynamically-resolved services too.

    Integration test fixtures rely on this to auto-build runner / rag-service
    images on a cold machine; iterating only ``IMAGE_DEFINITIONS`` misses them
    because they are resolved via ``_runner_definition`` / ``_rag_definition``.
    """
    assert service_name_for_image("tolokaforge-db-service") == "db-service"
    assert service_name_for_image("tolokaforge-mock-web") == "mock-web"
    assert service_name_for_image("tolokaforge-runner") == "runner"
    assert service_name_for_image("tolokaforge-rag-service") == "rag-service"
    assert service_name_for_image("tolokaforge-does-not-exist") is None


def test_start_service_builds_with_isolated_context_when_skipping_build_images(
    monkeypatch,
) -> None:
    """``_start_service`` must honor ``context_files`` even when called without
    a prior ``build_images()`` (e.g. via ``start_all(build=False)``).

    Regression: the fallback in ``_start_service`` previously called
    ``registry.get_or_build`` with the full repo context, defeating isolation.
    """
    captured: dict[str, str] = {}

    def fake_get_or_build(self, *, name, dockerfile, context, build_args=None):  # noqa: ARG001
        captured["dockerfile"] = dockerfile
        captured["context"] = context
        return Image(
            name=name,
            tag="abcd0001",
            image_id="dummy",
            dockerfile=dockerfile,
            context=context,
            context_hash="abcd0001",
            build_args=build_args or {},
        )

    class _Sentinel(Exception):
        pass

    from tolokaforge.docker import container as container_mod
    from tolokaforge.docker.registry import ImageRegistry

    monkeypatch.setattr(ImageRegistry, "get_or_build", fake_get_or_build)
    monkeypatch.setattr(
        container_mod.Container,
        "create",
        classmethod(lambda *a, **kw: (_ for _ in ()).throw(_Sentinel("stop after build"))),
    )

    svc = ServiceDefinition(
        name="runner",
        image_name="tolokaforge-runner",
        dockerfile="tolokaforge/docker/dockerfiles/runner.Dockerfile",
        context=".",
        context_files=["pyproject.toml", "README.md", "tolokaforge/"],
    )
    stack = ServiceStack()
    stack.add_service(svc)

    with pytest.raises(_Sentinel):
        stack._start_service(svc, wait=False)

    # Build context must be a temp dir (assembled), not the literal "." that
    # would have rebaked the entire repo.
    assert captured["context"] != "."
    assert "tolokaforge-build-" in captured["context"]
    # Dockerfile path is resolved against the temp build dir.
    assert captured["dockerfile"].endswith("tolokaforge/docker/dockerfiles/runner.Dockerfile")
    assert captured["dockerfile"].startswith(captured["context"])
