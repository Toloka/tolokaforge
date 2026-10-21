"""Every workflow install and build resolves third-party packages from ``uv.lock``.

``uv sync`` must run ``--locked``, ``uv pip install`` must pass the lock's pip
export as ``--constraint`` and ``uv build`` must pass it as
``--build-constraint``, so neither a dependency range nor a build backend
resolves against live PyPI. A ``uvx`` call installs outside the lock and is
refused unless its exemption below names why the lock cannot hold it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import pytest
import yaml

from tests.utils.image_constraints import IMAGE_CONSTRAINTS_PATH, REPO_ROOT

pytestmark = pytest.mark.canonical

_WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
_CONSTRAINTS = IMAGE_CONSTRAINTS_PATH.relative_to(REPO_ROOT).as_posix()

_COMMAND_SEPARATOR_RE = re.compile(r"&&|\|\||;|\|")
_UV_SYNC_RE = re.compile(r"\buv\s+sync\b")
_UV_PIP_INSTALL_RE = re.compile(r"\buv\s+pip\s+install\b")
_UV_BUILD_RE = re.compile(r"\buv\s+build\b")
_UVX_RE = re.compile(r"\buvx\b")

_UVX_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("integrate-model.yml", "litellm[proxy]"): (
        "#1685: the integrate-model LLM gateway runs litellm[proxy], whose extra "
        "cannot join uv.lock without moving existing pins"
    ),
}


@dataclass(frozen=True)
class _Command:
    workflow: str
    text: str


def _logical_lines(script: str) -> Iterator[str]:
    pending = ""
    for raw in script.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        yield pending + line
        pending = ""
    if pending:
        yield pending


def _commands(workflow: str, doc: object) -> Iterator[_Command]:
    assert isinstance(doc, dict), f"{workflow}: not a workflow mapping"
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            script = step.get("run")
            if not isinstance(script, str):
                continue
            for line in _logical_lines(script):
                for segment in _COMMAND_SEPARATOR_RE.split(line):
                    if segment.strip():
                        yield _Command(workflow, segment.strip())


def _unlocked(command: _Command) -> str | None:
    text = command.text
    if _UV_SYNC_RE.search(text) and "--locked" not in text.split():
        return "`uv sync` without `--locked`"
    if _UV_PIP_INSTALL_RE.search(text) and f"--constraint {_CONSTRAINTS}" not in text:
        return f"`uv pip install` without `--constraint {_CONSTRAINTS}`"
    if _UV_BUILD_RE.search(text) and f"--build-constraint {_CONSTRAINTS}" not in text:
        return f"`uv build` without `--build-constraint {_CONSTRAINTS}`"
    if _UVX_RE.search(text) and _uvx_exemption(command) is None:
        return "`uvx` installs outside uv.lock and has no exemption"
    return None


def _uvx_exemption(command: _Command) -> tuple[str, str] | None:
    for key in _UVX_EXEMPTIONS:
        workflow, needle = key
        if command.workflow == workflow and needle in command.text:
            return key
    return None


def _offenders(commands: Iterable[_Command]) -> list[str]:
    return [
        f"{command.workflow}: {reason}: {command.text}"
        for command in commands
        if (reason := _unlocked(command)) is not None
    ]


def find_unlocked_installs(workflow: str, workflow_text: str) -> list[str]:
    """One ``<workflow>: <reason>: <command>`` line per install or build that escapes the lock."""
    return _offenders(_commands(workflow, yaml.safe_load(workflow_text)))


def _workflow_commands() -> list[_Command]:
    paths = sorted((*_WORKFLOW_DIR.glob("*.yml"), *_WORKFLOW_DIR.glob("*.yaml")))
    assert paths, f"no workflow files under {_WORKFLOW_DIR}"
    return [
        command
        for path in paths
        for command in _commands(path.name, yaml.safe_load(path.read_text()))
    ]


def test_every_workflow_install_and_build_is_locked() -> None:
    commands = _workflow_commands()
    offenders = _offenders(commands)
    assert not offenders, "workflow installs or builds resolve outside uv.lock:\n" + "\n".join(
        offenders
    )
    for pattern in (_UV_SYNC_RE, _UV_PIP_INSTALL_RE, _UV_BUILD_RE):
        assert any(
            pattern.search(command.text) for command in commands
        ), f"no workflow command matches {pattern.pattern!r}; the scan no longer sees it"


def test_every_uvx_exemption_still_matches_a_command() -> None:
    used = {_uvx_exemption(command) for command in _workflow_commands()}
    stale = sorted(set(_UVX_EXEMPTIONS) - used)
    assert not stale, f"uvx exemptions match no workflow command; delete them: {stale}"


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ("uv sync --dev", "`uv sync` without `--locked`"),
        ("uv pip install dist/tolokaforge-*.whl", "`uv pip install` without `--constraint"),
        (
            "uv pip install --constraint other.txt dist/tolokaforge-*.whl",
            "`uv pip install` without `--constraint",
        ),
        ("uv build --wheel --package tolokaforge", "`uv build` without `--build-constraint"),
        ("cd / && uv build", "`uv build` without `--build-constraint"),
        (
            "uv venv /tmp/env\nVIRTUAL_ENV=/tmp/env uv pip install \\\n  dist/a.whl",
            "`uv pip install` without `--constraint",
        ),
        ("uvx --from 'litellm[proxy]==1.87.0' litellm", "`uvx` installs outside uv.lock"),
    ],
)
def test_an_unlocked_command_is_rejected(script: str, reason: str) -> None:
    workflow = yaml.safe_dump({"jobs": {"build": {"steps": [{"run": script}]}}})
    offenders = find_unlocked_installs("synthetic.yml", workflow)
    assert len(offenders) == 1, offenders
    assert reason in offenders[0]


def test_a_locked_command_passes() -> None:
    script = (
        "# uvx and uv sync in a comment are not commands\n"
        "uv sync --dev --locked\n"
        f"uv pip install \\\n  --constraint {_CONSTRAINTS} \\\n  dist/a.whl\n"
        f"uv build --wheel --build-constraint {_CONSTRAINTS}\n"
    )
    workflow = yaml.safe_dump({"jobs": {"build": {"steps": [{"run": script}]}}})
    assert find_unlocked_installs("synthetic.yml", workflow) == []
