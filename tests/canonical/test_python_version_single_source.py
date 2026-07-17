"""Guards that the runtime Python version stays single-sourced in ``.python-version``.

A regression that re-hardcodes a version — in a workflow's ``uv python install``
line, an ``actions/setup-python`` pin, or a runtime Dockerfile ``FROM`` — must fail
CI rather than silently drift from the pin. Runs under the ``canonical`` marker so it
participates in the existing CI smoke job without dedicated workflow wiring.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_DOCKERFILE_DIR = _REPO_ROOT / "tolokaforge" / "docker" / "dockerfiles"
_PINNED_VERSION = (_REPO_ROOT / ".python-version").read_text().strip()

_EXPECTED_INSTALL_ARG = '"$(cat .python-version)"'

_INSTALL_RE = re.compile(r"uv python install\s+(.*?)\s*$")
_TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")
_ARG_LINE_RE = re.compile(r"^ARG PYTHON_VERSION(?:=(\S+))?\s*$", re.MULTILINE)
_FROM_LINE_RE = re.compile(r"^FROM python:\$\{PYTHON_VERSION\}", re.MULTILINE)


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
