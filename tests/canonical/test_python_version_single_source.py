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
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.utils.ruff_targets import per_file_target_versions

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_DOCKERFILE_DIR = _REPO_ROOT / "tolokaforge" / "docker" / "dockerfiles"
_PINNED_VERSION = (_REPO_ROOT / ".python-version").read_text().strip()
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
                rel = path.relative_to(_REPO_ROOT)
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
        rel = path.relative_to(_REPO_ROOT)
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
        rel = path.relative_to(_REPO_ROOT)
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


def _package_pyprojects() -> dict[Path, dict]:
    found: dict[Path, dict] = {}
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        rel_dir = Path(dirpath).relative_to(_REPO_ROOT)
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".")
            and name != "node_modules"
            and (*rel_dir.parts, name) not in _PYPROJECT_SKIPPED_PREFIXES
        ]
        if "pyproject.toml" in filenames:
            path = Path(dirpath) / "pyproject.toml"
            found[path.relative_to(_REPO_ROOT)] = tomllib.loads(path.read_text())
    names = {doc.get("project", {}).get("name") for doc in found.values()}
    missing = _LIBRARY_DISTRIBUTIONS - names
    assert not missing, (
        f"pyproject discovery under {_REPO_ROOT} missed library distributions {sorted(missing)} "
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
    tool = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())["tool"]
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
    docs = sorted((*(_REPO_ROOT / "docs").glob("*.md"), _REPO_ROOT / "README.md"))
    violations: list[str] = []
    for path in docs:
        rel = path.relative_to(_REPO_ROOT)
        for block in _TOML_FENCE_RE.finditer(path.read_text()):
            violations.extend(
                f"{rel}: fenced TOML declares requires-python = {match.group(1)!r} "
                f"— expected {_EXPECTED_REQUIRES_PYTHON!r} (the .python-version floor)"
                for match in _DOC_REQUIRES_PYTHON_RE.finditer(block.group(1))
                if match.group(1) != _EXPECTED_REQUIRES_PYTHON
            )
    assert not violations, "\n".join(violations)


_MIDDLEWARE_PROXY = (
    "tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/middleware_proxy.py"
)
_CODING_HARNESS_ENVIRONMENT = "examples/native/coding_harness/environment"
_DOCKERFILE_FROM_PYTHON_RE = re.compile(r"^FROM python:(\d+\.\d+)\S*", re.MULTILINE)


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _python_files_matching(pattern: str) -> Iterator[Path]:
    for path in _REPO_ROOT.glob(pattern):
        if path.is_dir():
            yield from path.rglob("*.py")
        elif path.suffix == ".py":
            yield path


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


def test_the_coding_harness_example_target_matches_its_image() -> None:
    pattern = f"{_CODING_HARNESS_ENVIRONMENT}/**"
    dockerfile = _REPO_ROOT / _CODING_HARNESS_ENVIRONMENT / "Dockerfile"
    image = _DOCKERFILE_FROM_PYTHON_RE.search(dockerfile.read_text())
    assert image is not None, f"{dockerfile.relative_to(_REPO_ROOT)}: no `FROM python:X.Y` line"
    version = per_file_target_versions().get(pattern)
    assert version == image.group(1), (
        f"pyproject.toml: [tool.ruff.per-file-target-version] {pattern!r} is {version!r} "
        f"— expected {image.group(1)!r}, the `FROM python:` of {dockerfile.relative_to(_REPO_ROOT)}"
    )


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
