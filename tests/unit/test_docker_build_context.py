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
    assemble_build_context,
    build_image,
    get_image_definition,
    service_name_for_image,
)
from tolokaforge.docker.image import Image
from tolokaforge.docker.stack import EngineStack, ServiceDefinition
from tolokaforge.docker.wheel_resolver import WheelArtifact

pytestmark = pytest.mark.unit


@pytest.fixture()
def _mock_wheel(tmp_path: Path):
    """Mock ``resolve_wheel`` to return a fake ``.whl`` so tests exercising the
    remaining wheel-consuming stacks (rag-service, full stack) don't run a real
    build. The runner Dockerfile no longer consumes a host-side wheel — its
    multi-stage build produces the subset wheel in-container ([ADR-0025](../../docs/adr/0025-runner-wheel-split.md)),
    so ``tolokaforge.docker.stacks.core`` doesn't import ``resolve_wheel``
    anymore and is not patched here."""
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
def test_expected_image_ref_matches_real_content_hash() -> None:
    """``expected_image_ref('runner')`` equals ``name:hash8`` from the real
    context-assembly + content-hash path — driven here, not stubbed.

    Locks the plumbing: ``expected_image_ref`` assembles the real runner build
    context (``wheel_resolver`` + ``assemble_build_context``), routes it through
    the real ``_compute_content_hash``, slices ``[:8]``, and prefixes the image
    name (``name:hash[:8]``) — not a stub or a shortcut. Both sides call the same
    hash, so this does not catch a symmetric change to the hash's input set; it
    catches ``expected_image_ref`` diverging from that path or the tag format. It
    fails; it does not skip — no Docker daemon involved.
    """
    from tolokaforge.docker.builder import (
        assemble_build_context,
        expected_image_ref,
        get_image_definition,
        repo_root,
    )

    definition = get_image_definition("runner")
    build_context = assemble_build_context(
        repo_root=repo_root(),
        dockerfile=definition["dockerfile"],
        context_files=definition["context_files"],
    )
    try:
        dockerfile_path = build_context / definition["dockerfile"]
        content_hash = Image._compute_content_hash(
            dockerfile_path, build_context, definition["build_args"]
        )
    finally:
        shutil.rmtree(build_context, ignore_errors=True)

    assert expected_image_ref("runner") == f"{definition['name']}:{content_hash[:8]}"


def test_mock_web_context_scoped_to_service_files() -> None:
    """The real ``mock-web`` definition assembles an isolated context holding
    only its service files, so its content-hash moves with mock-web's own
    inputs and nothing else.

    Drives the real ``IMAGE_DEFINITIONS['mock-web']`` (no stubbing) through
    ``_prepared_build_context``. Assertion (3) is load-bearing: if no repo-root
    file (e.g. ``pyproject.toml``) reaches the context, then only
    ``tolokaforge/env/mock_web_service/**`` can move the content-hash — which is
    what makes the hash stable against unrelated repo edits. No Docker daemon.
    """
    from tolokaforge.docker.builder import _prepared_build_context

    with _prepared_build_context("mock-web") as (dockerfile, context, _name, _build_args):
        build_dir = Path(context)
        # (1) An assembled temp dir, not the literal repo ".".
        assert context != "."
        assert "tolokaforge-build-" in context
        # (2) The service files the Dockerfile COPYs are present, and the
        #     yielded Dockerfile path resolves under the assembled context.
        assert (build_dir / "tolokaforge/env/mock_web_service/requirements.txt").exists()
        assert (build_dir / "tolokaforge/env/mock_web_service/app.py").exists()
        assert dockerfile.startswith(context)
        assert Path(dockerfile).exists()
        # (3) No repo-root file leaks into the context.
        assert not (build_dir / "pyproject.toml").exists()


def test_runner_build_context_ships_source_tree_for_multi_stage_hatch_build() -> None:
    """The runner Dockerfile is multi-stage: its ``wheel-builder`` stage
    runs ``hatch build --target custom`` in-container to produce the
    runner-subset wheel, then the ``builder`` stage installs it (ADR-0025 /
    ADR-0027). The build context therefore carries the source files hatch
    needs — pyproject.toml, README, LICENSE, .python-version, the custom
    builder script, and the ``tolokaforge/`` source tree — not a
    pre-resolved host-side wheel.

    A regression that drops one of these entries produces a hatch
    ``file not found`` inside the image build, not a helpful host-side
    diagnostic; this test locks the expected context set explicitly."""
    runner_def = get_image_definition("runner")
    context_files = runner_def["context_files"]
    expected = {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        ".python-version",
        "scripts/hatch/",
        "tolokaforge/",
    }
    assert set(context_files) == expected, (
        "runner image context_files drifted from the ADR-0025 multi-stage "
        f"hatch-build source set. got: {sorted(context_files)}"
    )
    # No pre-built wheel should be listed — the subset wheel is built
    # in-container by the wheel-builder stage.
    assert not any(entry.endswith(".whl") for entry in context_files), (
        "runner image context_files still lists a pre-built .whl — the "
        "subset wheel is a Docker-only artifact built by the multi-stage "
        "Dockerfile, never a host-side input."
    )


def test_build_image_runner_ships_source_tree_not_a_wheel(monkeypatch) -> None:
    """The runner ``build_image('runner')`` path passes the source tree
    to docker as the build context, not a pre-resolved wheel. Locks the
    ADR-0025 multi-stage build contract: the ``.whl`` is produced *inside*
    the image, not copied in from the host."""
    captured: dict[str, object] = {}

    def fake_build(dockerfile, context, build_args=None, name=None, client=None):
        captured["dockerfile"] = dockerfile
        captured["context"] = context
        captured["build_args"] = build_args
        root = Path(context)

        # No host-side wheel is copied into the runner image build context;
        # the subset wheel is produced by the wheel-builder stage.
        wheels = list(root.glob("tolokaforge*.whl"))
        assert (
            wheels == []
        ), f"runner build context must not contain a pre-built wheel; found: {wheels}"
        # Instead, the source-tree entries hatch consumes must be present.
        assert (root / "pyproject.toml").exists()
        assert (root / "scripts" / "hatch" / "hatch_runner_subset_builder.py").exists()
        assert (root / "tolokaforge" / "__init__.py").exists()

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
    # PYTHON_VERSION is sourced from .python-version so a single upgrade
    # propagates to every runtime image. ``WHEEL_FILENAME`` is deliberately
    # absent — the multi-stage Dockerfile doesn't need it because the
    # subset wheel is produced in a preceding stage.
    from tolokaforge.docker.builder import PYTHON_VERSION

    assert captured["build_args"] == {"PYTHON_VERSION": PYTHON_VERSION}


def test_build_image_runner_non_force_path_uses_source_tree_context(monkeypatch) -> None:
    """The cached (non-force) path must also assemble the source-tree
    context the multi-stage runner build needs; wheel-based context is
    no longer part of the runner path."""
    captured: dict[str, object] = {}

    def fake_get_or_build(self, *, name, dockerfile, context, build_args=None):  # noqa: ARG001
        captured["dockerfile"] = dockerfile
        captured["context"] = context
        captured["build_args"] = build_args
        root = Path(context)
        # Source-tree entries hatch needs are present; no pre-built wheel.
        assert (root / "pyproject.toml").exists()
        assert (root / "tolokaforge" / "runner" / "_cli.py").exists()
        assert list(root.glob("tolokaforge*.whl")) == []
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
    from tolokaforge.docker.builder import PYTHON_VERSION

    assert captured["build_args"] == {"PYTHON_VERSION": PYTHON_VERSION}


def test_build_image_passes_python_version_build_arg_for_db_service(monkeypatch) -> None:
    """Every runtime image receives the pinned Python version as a build arg
    so ``FROM python:${PYTHON_VERSION}-slim`` resolves from ``.python-version``
    instead of a per-Dockerfile hardcoded minor version.
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
    from tolokaforge.docker.builder import PYTHON_VERSION

    assert captured["build_args"] == {"PYTHON_VERSION": PYTHON_VERSION}


def test_service_name_for_image_resolves_all_known_services_without_resolving_wheel(
    monkeypatch,
) -> None:
    """Reverse lookup must (a) cover dynamically-resolved services too and
    (b) never invoke the wheel resolver as a side effect.

    ``rag-service`` is still resolved via ``_rag_definition``; ``runner`` is
    now fully static (its multi-stage Dockerfile handles wheel production
    in-container per ADR-0025). Calling ``get_image_definition`` for every
    candidate would still invoke ``resolve_wheel()`` for rag-service even
    when looking up an unrelated service like db-service — propagating
    wheel-build failures from an unrelated lookup. The implementation must
    do neither.
    """

    def fail_resolve_wheel(*args, **kwargs):
        raise AssertionError("resolve_wheel must not be called during a name lookup")

    monkeypatch.setattr("tolokaforge.docker.builder.resolve_wheel", fail_resolve_wheel)

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
    stack = EngineStack()
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


def test_build_and_prepare_builds_images_and_networks_without_starting_containers(
    monkeypatch,
) -> None:
    """``EngineStack.build_and_prepare`` builds every declared image and
    creates every declared network, but skips the container-start phase.

    The per-trial substrate path needs the ``:local``-alias hook to find
    each engine image on the daemon (via ``get_image()``), yet the shared
    engine containers themselves go unused — every trial provisions its
    own runner/db-service through the task's own compose file. This
    method is the seam the orchestrator uses to avoid the wasted start.
    """
    built_names: list[str] = []

    def fake_get_or_build(self, *, name, dockerfile, context, build_args=None):
        # ``self`` and unused kwargs come from the ImageRegistry.get_or_build
        # signature we're patching over — the fake records the request and
        # hands back a synthetic Image.
        del self  # unused mock kwarg
        built_names.append(name)
        return Image(
            name=name,
            tag="deadbeef",
            image_id="dummy",
            dockerfile=dockerfile,
            context=context,
            context_hash="deadbeef",
            build_args=build_args or {},
        )

    from tolokaforge.docker import container as container_mod
    from tolokaforge.docker.registry import ImageRegistry

    monkeypatch.setattr(ImageRegistry, "get_or_build", fake_get_or_build)

    def _refuse_container_create(*_args, **_kwargs):
        # Positional/keyword arguments are Container.create's signature; the
        # fake ignores them because reaching this call is itself the failure.
        raise AssertionError("build_and_prepare must not create containers")

    monkeypatch.setattr(container_mod.Container, "create", classmethod(_refuse_container_create))

    svc_runner = ServiceDefinition(
        name="runner",
        image_name="tolokaforge-runner",
        dockerfile="tolokaforge/docker/dockerfiles/runner.Dockerfile",
        context=".",
        context_files=["pyproject.toml", "README.md", "tolokaforge/"],
        networks=["runner-net"],
    )
    svc_db = ServiceDefinition(
        name="db-service",
        image_name="tolokaforge-db-service",
        dockerfile="tolokaforge/docker/dockerfiles/db_service.Dockerfile",
        context=".",
        context_files=["pyproject.toml", "README.md", "tolokaforge/"],
        networks=["runner-net"],
    )
    stack = EngineStack()
    stack.add_service(svc_runner)
    stack.add_service(svc_db)

    stack.build_and_prepare()

    assert set(built_names) == {"tolokaforge-runner", "tolokaforge-db-service"}
    # Both images accessible via the same public lookup ``_ensure_engine_image_local_aliases`` uses.
    assert stack.get_image("runner") is not None
    assert stack.get_image("db-service") is not None
    # Idempotent no-op teardown when no containers were started — proves the
    # container-lifecycle side of the stack stayed untouched.
    stack.stop_all()  # would raise if containers had been created via the patched Container.create.
    stack.destroy(remove_networks=False)


def test_runner_build_context_resolves_from_installed_wheel(tmp_path: Path, monkeypatch) -> None:
    """On a wheel install the runner context must resolve to the PACKAGED
    copies, not the repo-root paths.

    ``repo_root()`` is ``Path(__file__).parents[2]``, which is
    ``site-packages`` once tolokaforge is installed as a wheel — so
    ``pyproject.toml`` / ``README.md`` / ``LICENSE`` / ``scripts/hatch`` are
    simply not there. 0.14.0 shipped the repo-relative set unconditionally,
    so every Docker-runtime run died in ``build_images()`` with
    ``FileNotFoundError: Declared context path not found: pyproject.toml``
    before a single trial executed — invisible to CI, which
    only ever builds from a source checkout.

    This locks the wheel-install branch: entries are absolute paths into the
    packaged ``_subset_build`` dir, and they land in the assembled context
    under exactly the names the Dockerfile ``COPY`` lines expect."""
    site_packages = tmp_path / "site-packages"
    pkg = site_packages / "tolokaforge"
    packaged = pkg / "_subset_build"
    (packaged / "scripts" / "hatch").mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (packaged / name).write_text(f"# {name}\n")
    (packaged / "scripts" / "hatch" / "hatch_runner_subset_builder.py").write_text("")
    (pkg / "_python_version.txt").write_text("3.12\n")
    # The base wheel ships the Dockerfiles inside the package, so
    # ``assemble_build_context`` still finds the runner Dockerfile under
    # ``repo_root``/``site-packages``.
    dockerfiles = pkg / "docker" / "dockerfiles"
    dockerfiles.mkdir(parents=True)
    (dockerfiles / "runner.Dockerfile").write_text("FROM scratch\n")
    # A wheel install has no repo-root pyproject.toml — that is the trigger.
    monkeypatch.setattr("tolokaforge.docker.builder.repo_root", lambda: site_packages)
    monkeypatch.setattr("tolokaforge.docker.builder.installed_package_dir", lambda: pkg)

    runner_def = get_image_definition("runner")
    entries = runner_def["context_files"]

    for entry in entries:
        src = Path(entry[0] if isinstance(entry, tuple) else entry)
        assert src.is_absolute(), f"wheel-install entry must be absolute: {entry}"
        assert src.exists(), f"wheel-install entry does not exist: {entry}"

    build_dir = assemble_build_context(site_packages, runner_def["dockerfile"], entries)
    try:
        for expected in (
            "pyproject.toml",
            "README.md",
            "LICENSE",
            ".python-version",
            "scripts/hatch",
            "tolokaforge",
        ):
            assert (build_dir / expected).exists(), (
                f"assembled runner context is missing '{expected}', which the "
                "runner Dockerfile COPYs for its hatchling build stage"
            )
        # Content assertions — a broken rename or empty copy would pass the
        # existence check above but fail the Dockerfile stage. The .python-
        # version entry is the tuple-form rename (source is
        # ``_python_version.txt``, destination is ``.python-version``); a
        # regression that dropped the tuple handling would land an empty
        # file or the wrong name here.
        assert (build_dir / ".python-version").read_text() == "3.12\n", (
            ".python-version content mismatch — the (source, destination) tuple "
            "form in ``assemble_build_context`` must copy source bytes to the "
            "renamed destination"
        )
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            assert (build_dir / name).read_text() == f"# {name}\n", (
                f"{name} content mismatch — flat-copy of an absolute path landed "
                "an empty or wrong-source file"
            )
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def test_runner_build_context_fails_loud_when_wheel_lacks_packaged_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    """A wheel built from a pyproject without the force-include entries must
    fail with an actionable message naming the missing paths, not with a bare
    ``FileNotFoundError`` from deep inside the context copy."""
    site_packages = tmp_path / "site-packages"
    pkg = site_packages / "tolokaforge"
    pkg.mkdir(parents=True)
    monkeypatch.setattr("tolokaforge.docker.builder.repo_root", lambda: site_packages)
    monkeypatch.setattr("tolokaforge.docker.builder.installed_package_dir", lambda: pkg)

    with pytest.raises(FileNotFoundError, match="force-include"):
        get_image_definition("runner")


def test_core_stack_runner_context_assembles_on_a_wheel_install(
    tmp_path: Path, monkeypatch
) -> None:
    """``core_stack()`` must produce a runner context that ASSEMBLES when the
    engine is installed as a wheel.

    ``core_stack()`` is the path a real run takes (orchestrator ->
    ``service_stack.start_all`` -> ``build_images`` ->
    ``assemble_build_context(repo_root(), svc.dockerfile, svc.context_files)``).
    It used to spell the repo-relative list out a second time, so when the
    builder learned to resolve packaged copies for a wheel install the service
    stack kept handing over repo-root paths — and v0.14.0 AND v0.14.1 both
    shipped an engine that died in ``build_images()`` with
    ``Declared context path not found: pyproject.toml``.

    This test must run INSIDE the wheel-install simulation. A plain equality
    assert in a source checkout cannot fail: there the builder returns the same
    six strings in the same order as the literal the bug reintroduces, so both
    sides match either way. Assembling under the patch is what actually gates
    it — reverting ``core.py`` to its duplicated list makes this raise."""
    from tolokaforge.docker.stacks.core import core_stack

    site_packages = tmp_path / "site-packages"
    pkg = site_packages / "tolokaforge"
    packaged = pkg / "_subset_build"
    (packaged / "scripts" / "hatch").mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (packaged / name).write_text(f"# {name}\n")
    (packaged / "scripts" / "hatch" / "hatch_runner_subset_builder.py").write_text("")
    (pkg / "_python_version.txt").write_text("3.12\n")
    dockerfiles = pkg / "docker" / "dockerfiles"
    dockerfiles.mkdir(parents=True)
    (dockerfiles / "runner.Dockerfile").write_text("FROM scratch\n")
    monkeypatch.setattr("tolokaforge.docker.builder.repo_root", lambda: site_packages)
    monkeypatch.setattr("tolokaforge.docker.builder.installed_package_dir", lambda: pkg)

    svc = core_stack().services["runner"]
    build_dir = assemble_build_context(site_packages, svc.dockerfile, svc.context_files)
    try:
        for expected in (
            "pyproject.toml",
            "README.md",
            "LICENSE",
            ".python-version",
            "scripts/hatch",
            "tolokaforge",
        ):
            assert (build_dir / expected).exists(), (
                f"core_stack()'s runner context is missing '{expected}' on a wheel "
                "install — the service stack is not using the builder's resolved list"
            )
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

    # Belt and braces: the two lists must also stay identical, so a future edit
    # to one cannot silently diverge. (Necessary, not sufficient — see above.)
    assert svc.context_files == get_image_definition("runner")["context_files"]
