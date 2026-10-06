"""Guards that the runtime Python version stays single-sourced in ``.python-version``.

A regression that re-hardcodes a version — in a workflow's ``uv python install``
line, an ``actions/setup-python`` pin, or a runtime Dockerfile ``FROM`` — must fail
CI rather than silently drift from the pin. The pin's ``major.minor`` is also the
declared install floor and the lint/type-check target: every package's
``requires-python``, its version classifiers, the ``requires-python`` lines in
documented TOML snippets, and the ruff, black and mypy targets must name it. Runs under
the ``canonical`` marker so it participates in the existing CI smoke job without
dedicated workflow wiring.

Code that runs inside a task image runs on that image's ``python3``, not on the pin.
Each such path carries a ``[tool.ruff.per-file-target-version]`` entry naming the
lowest interpreter it supports, and the sandbox checks keep those entries tied to
real paths and to the images they describe.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.utils.ruff_targets import REPO_ROOT, per_file_target_versions
from tolokaforge_coding_harnesses import MIDDLEWARE_PROXY_SCRIPT

pytestmark = pytest.mark.canonical


_WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
_DOCKERFILE_DIR = REPO_ROOT / "tolokaforge" / "docker" / "dockerfiles"
_PINNED_VERSION = (REPO_ROOT / ".python-version").read_text().strip()
_FLOOR = ".".join(_PINNED_VERSION.split(".")[:2])
_EXPECTED_REQUIRES_PYTHON = f">={_FLOOR}"
_LIBRARY_DISTRIBUTIONS = frozenset(
    {"tolokaforge", "tolokaforge-models", "tolokaforge-langfuse", "tolokaforge-coding-harnesses"}
)
_PYPROJECT_SKIPPED_PREFIXES = (("contrib",), ("tests", "data", "projects"))

_EXPECTED_INSTALL_ARG = '"$(cat .python-version)"'

_INSTALL_RE = re.compile(r"uv python install\s+(.*?)\s*$")
_TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")
_ARG_LINE_RE = re.compile(r"^ARG PYTHON_VERSION(?:=(\S+))?\s*$", re.MULTILINE)
_FROM_LINE_RE = re.compile(r"^FROM python:\$\{PYTHON_VERSION\}", re.MULTILINE)
_VERSION_CLASSIFIER_RE = re.compile(r"^Programming Language :: Python :: (3\.\d+)$")
_TOML_FENCE_RE = re.compile(r"^```toml\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_DOC_REQUIRES_PYTHON_RE = re.compile(r'^requires-python\s*=\s*"([^"]*)"', re.MULTILINE)


def _workflow_files() -> list[Path]:
    files = sorted((*_WORKFLOW_DIR.glob("*.yml"), *_WORKFLOW_DIR.glob("*.yaml")))
    assert files, f"no workflow files found under {_WORKFLOW_DIR} — the guard would pass vacuously"
    return files


def _iter_steps(doc: object):
    if not isinstance(doc, dict):
        return
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict):
                yield step


def test_workflow_uv_python_install_reads_the_pin() -> None:
    violations: list[str] = []
    for path in _workflow_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            match = _INSTALL_RE.search(line)
            if match is None:
                continue
            arg = _TRAILING_COMMENT_RE.sub("", match.group(1)).strip()
            if arg != _EXPECTED_INSTALL_ARG:
                rel = path.relative_to(REPO_ROOT)
                violations.append(
                    f"{rel}:{lineno}: `uv python install {arg}` hardcodes the version — "
                    f"expected `uv python install {_EXPECTED_INSTALL_ARG}`"
                )
    message = "Workflows must install the pinned Python via .python-version:\n" + "\n".join(
        violations
    )
    assert not violations, message


def test_workflow_setup_python_uses_version_file() -> None:
    violations: list[str] = []
    for path in _workflow_files():
        doc = yaml.safe_load(path.read_text())
        rel = path.relative_to(REPO_ROOT)
        for step in _iter_steps(doc):
            uses = step.get("uses", "")
            if not isinstance(uses, str) or not uses.startswith("actions/setup-python"):
                continue
            with_block = step.get("with") or {}
            if "python-version" in with_block:
                violations.append(
                    f"{rel}: `actions/setup-python` pins `python-version` — "
                    "use `python-version-file: .python-version` instead"
                )
            elif with_block.get("python-version-file") != ".python-version":
                violations.append(
                    f"{rel}: `actions/setup-python` must set `python-version-file: .python-version`"
                )
    assert not violations, "\n".join(violations)


def test_runtime_dockerfiles_single_source_python() -> None:
    dockerfiles = sorted(_DOCKERFILE_DIR.glob("*.Dockerfile"))
    assert dockerfiles, f"no runtime Dockerfiles found under {_DOCKERFILE_DIR}"

    violations: list[str] = []
    for path in dockerfiles:
        text = path.read_text()
        rel = path.relative_to(REPO_ROOT)
        arg_match = _ARG_LINE_RE.search(text)
        if arg_match is None:
            violations.append(
                f"{rel}: missing `ARG PYTHON_VERSION` — runtime image must accept the pin"
            )
            continue
        if _FROM_LINE_RE.search(text) is None:
            violations.append(
                f"{rel}: missing `FROM python:${{PYTHON_VERSION}}` — image must build from the ARG, not a literal"
            )
        default = arg_match.group(1)
        if default is not None and default.strip() != _PINNED_VERSION:
            violations.append(
                f"{rel}: `ARG PYTHON_VERSION={default}` default diverges from "
                f".python-version ({_PINNED_VERSION}) — a plain `docker build` would use the stale default"
            )
    assert not violations, "\n".join(violations)


def _walk_repo(
    skipped_prefixes: tuple[tuple[str, ...], ...] = (),
) -> Iterator[tuple[str, list[str]]]:
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        rel_dir = Path(dirpath).relative_to(REPO_ROOT)
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".")
            and name != "node_modules"
            and (*rel_dir.parts, name) not in skipped_prefixes
        ]
        yield dirpath, filenames


def _package_pyprojects() -> dict[Path, dict]:
    found: dict[Path, dict] = {}
    for dirpath, filenames in _walk_repo(_PYPROJECT_SKIPPED_PREFIXES):
        if "pyproject.toml" in filenames:
            path = Path(dirpath) / "pyproject.toml"
            found[path.relative_to(REPO_ROOT)] = tomllib.loads(path.read_text())
    names = {doc.get("project", {}).get("name") for doc in found.values()}
    missing = _LIBRARY_DISTRIBUTIONS - names
    assert not missing, (
        f"pyproject discovery under {REPO_ROOT} missed library distributions {sorted(missing)} "
        "— the floor guard would pass vacuously"
    )
    return found


def test_every_package_requires_the_pinned_floor() -> None:
    violations = [
        f"{rel}: [project].requires-python is {doc.get('project', {}).get('requires-python')!r} "
        f"— expected {_EXPECTED_REQUIRES_PYTHON!r} (the .python-version floor)"
        for rel, doc in sorted(_package_pyprojects().items())
        if doc.get("project", {}).get("requires-python") != _EXPECTED_REQUIRES_PYTHON
    ]
    assert not violations, "\n".join(violations)


def test_lint_and_type_check_targets_name_the_floor() -> None:
    tool = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]
    floor_tag = "py" + _FLOOR.replace(".", "")
    found = {
        "[tool.ruff].target-version": (tool["ruff"].get("target-version"), floor_tag),
        "[tool.black].target-version": (tool["black"].get("target-version"), [floor_tag]),
        "[tool.mypy].python_version": (tool["mypy"].get("python_version"), _FLOOR),
    }
    violations = [
        f"pyproject.toml: {field} is {actual!r} — expected {expected!r} (the .python-version floor)"
        for field, (actual, expected) in found.items()
        if actual != expected
    ]
    assert not violations, "\n".join(violations)


def test_version_classifiers_name_only_the_floor() -> None:
    violations: list[str] = []
    for rel, doc in sorted(_package_pyprojects().items()):
        classifiers = doc.get("project", {}).get("classifiers", [])
        versions = [
            match.group(1)
            for match in map(_VERSION_CLASSIFIER_RE.match, classifiers)
            if match is not None
        ]
        violations.extend(
            f"{rel}: [project].classifiers lists `Python :: {version}` "
            f"— expected only `Python :: {_FLOOR}` (the .python-version floor)"
            for version in versions
            if version != _FLOOR
        )
        if classifiers and _FLOOR not in versions:
            violations.append(
                f"{rel}: [project].classifiers lacks `Programming Language :: Python :: {_FLOOR}` "
                "— expected the .python-version floor classifier"
            )
    assert not violations, "\n".join(violations)


def test_documented_toml_snippets_require_the_floor() -> None:
    docs = sorted((*(REPO_ROOT / "docs").glob("*.md"), REPO_ROOT / "README.md"))
    violations: list[str] = []
    for path in docs:
        rel = path.relative_to(REPO_ROOT)
        for block in _TOML_FENCE_RE.finditer(path.read_text()):
            violations.extend(
                f"{rel}: fenced TOML declares requires-python = {match.group(1)!r} "
                f"— expected {_EXPECTED_REQUIRES_PYTHON!r} (the .python-version floor)"
                for match in _DOC_REQUIRES_PYTHON_RE.finditer(block.group(1))
                if match.group(1) != _EXPECTED_REQUIRES_PYTHON
            )
    assert not violations, "\n".join(violations)


_MIDDLEWARE_PROXY = MIDDLEWARE_PROXY_SCRIPT.relative_to(REPO_ROOT).as_posix()
_DOCKERFILE_FROM_PYTHON_RE = re.compile(r"^FROM python:(\d+\.\d+)\S*", re.MULTILINE)
_TASK_MANIFESTS = ("task.yaml", "task.toml")


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _python_files_matching(pattern: str) -> Iterator[Path]:
    for path in REPO_ROOT.glob(pattern):
        if path.is_dir():
            yield from path.rglob("*.py")
        elif path.suffix == ".py":
            yield path


def _image_python_dockerfiles() -> dict[Path, str]:
    """Map each Dockerfile built ``FROM python:X.Y`` to the lowest ``X.Y`` it names."""
    images: dict[Path, str] = {}
    for dirpath, filenames in _walk_repo():
        for name in filenames:
            if name != "Dockerfile" and not name.endswith(".Dockerfile"):
                continue
            path = Path(dirpath) / name
            versions = _DOCKERFILE_FROM_PYTHON_RE.findall(path.read_text())
            if versions:
                images[path] = min(versions, key=_version_tuple)
    assert images, f"no `FROM python:X.Y` Dockerfile found under {REPO_ROOT} — the guard is vacuous"
    return images


def _sandbox_tree(dockerfile: Path) -> Path:
    """The task directory that ships ``dockerfile``, or its own directory outside a task."""
    for directory in dockerfile.parents:
        if directory == REPO_ROOT:
            break
        if any((directory / manifest).is_file() for manifest in _TASK_MANIFESTS):
            return directory
    return dockerfile.parent


def _ruff_linted_files(trees: list[Path]) -> set[Path]:
    listing = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--show-files",
            "--force-exclude",
            *map(str, trees),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {Path(line) for line in listing.stdout.splitlines() if line.endswith(".py")}


def test_the_middleware_proxy_declares_a_sandbox_target_at_or_below_the_floor() -> None:
    version = per_file_target_versions().get(_MIDDLEWARE_PROXY)
    assert version is not None, (
        f"pyproject.toml: [tool.ruff.per-file-target-version] has no entry for {_MIDDLEWARE_PROXY} "
        f"— the proxy runs on the task image's python3, so lint must target its lowest supported "
        f"version rather than the {_FLOOR} floor"
    )
    assert _version_tuple(version) <= _version_tuple(_FLOOR), (
        f"pyproject.toml: [tool.ruff.per-file-target-version] {_MIDDLEWARE_PROXY} is {version} "
        f"— expected a version at or below the {_FLOOR} floor"
    )


def test_linted_task_image_code_targets_at_most_its_image_python() -> None:
    images = _image_python_dockerfiles()
    trees = {dockerfile: _sandbox_tree(dockerfile) for dockerfile in images}
    linted = _ruff_linted_files(sorted(set(trees.values())))
    assert linted, (
        "ruff --show-files listed no Python file under "
        f"{sorted(str(tree.relative_to(REPO_ROOT)) for tree in set(trees.values()))} "
        "— the sandbox target guard would pass vacuously"
    )
    coverage = {
        pattern: (version, set(_python_files_matching(pattern)))
        for pattern, version in per_file_target_versions().items()
    }
    violations: list[str] = []
    for dockerfile, image in sorted(images.items()):
        for path in sorted(p for p in linted if p.is_relative_to(trees[dockerfile])):
            targets = [version for version, files in coverage.values() if path in files]
            target = max(targets, key=_version_tuple) if targets else _FLOOR
            if _version_tuple(target) > _version_tuple(image):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}: ruff lints it as Python {target} but "
                    f"{dockerfile.relative_to(REPO_ROOT)} runs it on python:{image} — add a "
                    f"[tool.ruff.per-file-target-version] entry at or below {image}"
                )
    assert not violations, "\n".join(violations)


def test_every_sandbox_target_names_an_existing_path() -> None:
    entries = per_file_target_versions()
    assert entries, "pyproject.toml declares no [tool.ruff.per-file-target-version] entries"
    stale = [
        f"pyproject.toml: [tool.ruff.per-file-target-version] {pattern!r} matches no Python file "
        "— a rename dropped the sandbox lint target"
        for pattern in sorted(entries)
        if not any(_python_files_matching(pattern))
    ]
    assert not stale, "\n".join(stale)
