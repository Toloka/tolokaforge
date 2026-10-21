"""Every first-party Dockerfile installs third-party packages at ``uv.lock`` versions.

Each stage that runs ``pip install`` copies the lock's pip export into the image
and points ``PIP_CONSTRAINT`` and ``PIP_BUILD_CONSTRAINT`` at it before its first
install. Every install is wheel-only except the lock's Linux sdist-only packages
(``--only-binary :all:`` followed by ``--no-binary <set>``), names only lock
packages, installs first-party distributions by wheel path, and is followed in
the same ``RUN`` by the locked-install guard with the stage's exemption row.
CPU torch comes from the CPU index alone and without dependencies, and the
standalone image installs the langfuse and terminal-bench adapter wheels exactly
for the extras that require them.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.utils.image_constraints import (
    IMAGE_CONSTRAINTS_PATH,
    REPO_ROOT,
    linux_sdist_only_packages,
    lock_workspace_names,
    parse_constraint_lines,
)

pytestmark = pytest.mark.canonical

_DOCKERFILE_DIR = REPO_ROOT / "tolokaforge" / "docker" / "dockerfiles"
_CONSTRAINTS_SOURCE = IMAGE_CONSTRAINTS_PATH.relative_to(REPO_ROOT).as_posix()
_IMAGE_CONSTRAINTS = "/opt/tolokaforge/image-constraints.txt"
_CPU_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
_SUBSET_DISTRIBUTION = "tolokaforge-runner-subset"

_SEPARATORS = frozenset({"&&", "||", ";", ";;", "|", "(", ")"})
_VALUED_OPTIONS = frozenset({"--only-binary", "--no-binary", "--index-url", "-r"})
_FLAG_OPTIONS = frozenset({"--no-cache-dir", "--no-compile", "--no-deps"})
_PLUGIN_WHEEL_RE = re.compile(r"/(?P<stem>tolokaforge_\w+)-\*\.whl")


@dataclass(frozen=True)
class GuardRow:
    first_party: frozenset[str] = frozenset()
    exempt: frozenset[str] = frozenset({"pip"})


_BOOTSTRAP = GuardRow()
_RUNNER = GuardRow(
    frozenset({_SUBSET_DISTRIBUTION, "tolokaforge-models", "tolokaforge-coding-harnesses"})
)
_FULL_WHEEL = GuardRow(
    frozenset({"tolokaforge", "tolokaforge-models", "tolokaforge-coding-harnesses"})
)
_STANDALONE = GuardRow(
    _FULL_WHEEL.first_party | {"tolokaforge-langfuse", "tolokaforge-adapter-terminal-bench"}
)

_GUARD_ROWS: dict[str, dict[str, GuardRow]] = {
    "runner.Dockerfile": {"wheel-builder": _BOOTSTRAP, "builder": _RUNNER, "stage 2": _RUNNER},
    "grader.Dockerfile": {"wheel-builder": _BOOTSTRAP, "builder": _FULL_WHEEL},
    "trial_node.Dockerfile": {"wheel-builder": _BOOTSTRAP, "builder": _FULL_WHEEL},
    "standalone.Dockerfile": {
        "wheel-builder": _BOOTSTRAP,
        "builder": _STANDALONE,
        "runtime": _STANDALONE,
    },
    "rag.Dockerfile": {"sibling-wheel-builder": _BOOTSTRAP, "stage 1": _FULL_WHEEL},
    "db_service.Dockerfile": {"stage 0": _BOOTSTRAP},
    "mock_web.Dockerfile": {"stage 0": _BOOTSTRAP},
}

_CPU_TORCH_STAGES = frozenset({("rag.Dockerfile", "stage 1"), ("standalone.Dockerfile", "builder")})


@dataclass(frozen=True)
class _Instruction:
    keyword: str
    args: str


@dataclass(frozen=True)
class _Stage:
    name: str
    instructions: tuple[_Instruction, ...]


@dataclass(frozen=True)
class _Command:
    run: int
    tokens: tuple[str, ...]

    def pip_install_args(self) -> tuple[str, ...] | None:
        for index, token in enumerate(self.tokens[:-1]):
            if PurePosixPath(token).name == "pip" and self.tokens[index + 1] == "install":
                return self.tokens[index + 2 :]
        return None

    def is_pip_check(self) -> bool:
        return any(
            PurePosixPath(token).name == "pip" and self.tokens[index + 1] == "check"
            for index, token in enumerate(self.tokens[:-1])
        )

    def guard_args(self) -> tuple[str, ...] | None:
        for index, token in enumerate(self.tokens):
            if token.endswith("locked_install_check.py"):
                return self.tokens[index + 1 :]
        return None


def _logical_lines(text: str) -> Iterator[str]:
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        yield pending + line
        pending = ""
    if pending:
        yield pending


def parse_stages(text: str) -> list[_Stage]:
    """Split a Dockerfile into its build stages; an unnamed stage is ``stage <index>``."""
    stages: list[_Stage] = []
    name = ""
    instructions: list[_Instruction] = []
    for line in _logical_lines(text):
        keyword, _, args = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            if name:
                stages.append(_Stage(name, tuple(instructions)))
            words = args.split()
            alias = words[-1] if len(words) >= 3 and words[-2].upper() == "AS" else None
            name = alias or f"stage {len(stages)}"
            instructions = []
        elif name:
            instructions.append(_Instruction(keyword, args.strip()))
    if name:
        stages.append(_Stage(name, tuple(instructions)))
    return stages


def _shell_tokens(script: str) -> list[str]:
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _commands(stage: _Stage) -> list[_Command]:
    commands: list[_Command] = []
    for run, instruction in enumerate(stage.instructions):
        if instruction.keyword != "RUN":
            continue
        current: list[str] = []
        for token in [*_shell_tokens(instruction.args), ";"]:
            if token in _SEPARATORS:
                if current:
                    commands.append(_Command(run, tuple(current)))
                current = []
            else:
                current.append(token)
    return commands


def _option_values(args: tuple[str, ...], option: str) -> list[str]:
    return [args[index + 1] for index, token in enumerate(args[:-1]) if token == option]


def _env_assignments(stage: _Stage, before_run: int) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for instruction in stage.instructions[:before_run]:
        if instruction.keyword != "ENV":
            continue
        for word in shlex.split(instruction.args):
            key, _, value = word.partition("=")
            assignments[key] = value
    return assignments


def _copied_files(stage: _Stage, before_run: int) -> dict[str, str]:
    """Image path -> repo source of every single-file ``COPY`` from the build context."""
    copied: dict[str, str] = {}
    for instruction in stage.instructions[:before_run]:
        words = shlex.split(instruction.args)
        if instruction.keyword != "COPY" or any(word.startswith("--from") for word in words):
            continue
        *sources, destination = words
        for source in sources:
            if not (REPO_ROOT / source).is_file():
                continue
            if destination.endswith("/") or destination == ".":
                copied[PurePosixPath(source).name] = source
            else:
                copied[PurePosixPath(destination).name] = source
    return copied


def _lock_names() -> frozenset[str]:
    return frozenset(pin.name for pin in parse_constraint_lines(IMAGE_CONSTRAINTS_PATH.read_text()))


def _first_party_names() -> frozenset[str]:
    return lock_workspace_names() | {_SUBSET_DISTRIBUTION}


def _requirement_offences(requirement_text: str) -> list[str]:
    name = canonicalize_name(Requirement(requirement_text).name)
    if name in _first_party_names():
        return [f"first-party `{requirement_text}` installed by name, not by local wheel path"]
    if name not in _lock_names():
        return [f"`{requirement_text}` is not a uv.lock package"]
    return []


def _requirements_file_lines(stage: _Stage, run: int, path: str) -> list[str]:
    source = _copied_files(stage, run).get(PurePosixPath(path).name)
    assert source is not None, f"`-r {path}`: no COPY from the build context lands it"
    lines = (REPO_ROOT / source).read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _install_offences(stage: _Stage, command: _Command, sdist_only: frozenset[str]) -> list[str]:
    args = command.pip_install_args()
    assert args is not None
    offences: list[str] = []
    only_binary = _option_values(args, "--only-binary")
    no_binary = _option_values(args, "--no-binary")
    if only_binary != [":all:"] or len(no_binary) != 1:
        offences.append("needs exactly one `--only-binary :all:` and one `--no-binary`")
    elif args.index("--only-binary") > args.index("--no-binary"):
        offences.append("`--no-binary` precedes `--only-binary :all:`, which clears it")
    elif frozenset(no_binary[0].split(",")) != sdist_only:
        offences.append(f"`--no-binary {no_binary[0]}` is not the lock's sdist-only set")
    if "--upgrade" in args or "-U" in args:
        offences.append("`--upgrade` moves a package off the lock")
    index_urls = _option_values(args, "--index-url")
    requirements: list[str] = []
    skip_next = False
    for position, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token in _VALUED_OPTIONS:
            skip_next = True
            if token == "-r":
                requirements += _requirements_file_lines(stage, command.run, args[position + 1])
            continue
        if token.startswith("-"):
            if token not in _FLAG_OPTIONS:
                offences.append(f"unaudited pip option `{token}`")
            continue
        if "/" in token or token.startswith("$"):
            continue
        requirements.append(token)
    if index_urls and (
        index_urls != [_CPU_TORCH_INDEX] or "--no-deps" not in args or requirements != ["torch"]
    ):
        offences.append(f"`--index-url` is reserved for `--no-deps` torch from {_CPU_TORCH_INDEX}")
    for requirement in requirements:
        offences += _requirement_offences(requirement)
    return offences


def _guard_row(args: tuple[str, ...]) -> tuple[str | None, GuardRow]:
    constraints = _option_values(args, "--constraints")
    row = GuardRow(
        frozenset(canonicalize_name(name) for name in _option_values(args, "--first-party")),
        frozenset(canonicalize_name(name) for name in _option_values(args, "--exempt")),
    )
    return (constraints[0] if len(constraints) == 1 else None), row


def _env_offences(stage: _Stage, first_install_run: int) -> list[str]:
    offences: list[str] = []
    env = _env_assignments(stage, first_install_run)
    for variable in ("PIP_CONSTRAINT", "PIP_BUILD_CONSTRAINT"):
        if env.get(variable) != _IMAGE_CONSTRAINTS:
            offences.append(f"`{variable}` is not {_IMAGE_CONSTRAINTS} before the first install")
    copied = _copied_files(stage, first_install_run)
    if copied.get(PurePosixPath(_IMAGE_CONSTRAINTS).name) != _CONSTRAINTS_SOURCE:
        offences.append(f"{_CONSTRAINTS_SOURCE} is not copied in before the first install")
    return offences


def _guard_offences(commands: list[_Command], installs: list[int], row: GuardRow) -> list[str]:
    last_install = installs[-1]
    guards = [
        index
        for index, command in enumerate(commands)
        if index > last_install
        and command.run == commands[last_install].run
        and command.guard_args() is not None
    ]
    if not guards:
        return ["no locked-install guard after the last install in the same RUN"]
    guard_args = commands[guards[0]].guard_args()
    assert guard_args is not None
    constraints, found = _guard_row(guard_args)
    offences: list[str] = []
    if constraints != _IMAGE_CONSTRAINTS:
        offences.append(f"guard checks `{constraints}`, not {_IMAGE_CONSTRAINTS}")
    if found != row:
        offences.append(f"guard row {found} is not {row}")
    return offences


def _stage_offences(stage: _Stage, row: GuardRow | None) -> list[str]:
    commands = _commands(stage)
    installs = [index for index, cmd in enumerate(commands) if cmd.pip_install_args() is not None]
    if not installs:
        return [] if row is None else ["has a guard row but no pip install"]
    if row is None:
        return ["installs with pip but has no guard row in this test"]
    offences = _env_offences(stage, commands[installs[0]].run)
    sdist_only = linux_sdist_only_packages()
    for index in installs:
        offences += _install_offences(stage, commands[index], sdist_only)
    return offences + _guard_offences(commands, installs, row)


def find_unlocked_installs(text: str, rows: Mapping[str, GuardRow]) -> list[str]:
    """One ``<stage>: <reason>`` line per way a Dockerfile's installs escape the lock."""
    stages = parse_stages(text)
    offences = [
        f"{stage.name}: {offence}"
        for stage in stages
        for offence in _stage_offences(stage, rows.get(stage.name))
    ]
    unknown = sorted(set(rows) - {stage.name for stage in stages})
    return offences + [f"{name}: guard row names no stage" for name in unknown]


def _dockerfiles() -> list[Path]:
    paths = sorted(_DOCKERFILE_DIR.glob("*.Dockerfile"))
    assert paths, f"no Dockerfiles under {_DOCKERFILE_DIR}"
    return paths


def test_every_first_party_dockerfile_has_guard_rows() -> None:
    assert {path.name for path in _dockerfiles()} == set(_GUARD_ROWS)


@pytest.mark.parametrize("dockerfile", sorted(_GUARD_ROWS))
def test_dockerfile_installs_are_locked(dockerfile: str) -> None:
    offences = find_unlocked_installs(
        (_DOCKERFILE_DIR / dockerfile).read_text(), _GUARD_ROWS[dockerfile]
    )
    assert not offences, f"{dockerfile} installs outside uv.lock:\n" + "\n".join(offences)


def _cpu_torch_offences(stage: _Stage) -> list[str]:
    commands = _commands(stage)
    torch_steps = [
        index
        for index, command in enumerate(commands)
        if _CPU_TORCH_INDEX in (command.pip_install_args() or ())
    ]
    if len(torch_steps) != 1:
        return [f"expected one CPU-index torch install, found {len(torch_steps)}"]
    step = torch_steps[0]
    later = [command for command in commands[step + 1 :] if command.run == commands[step].run]
    offences: list[str] = []
    if not any(command.pip_install_args() is not None for command in later):
        offences.append("no PyPI install resolves torch's dependencies in the same RUN")
    if not any(command.is_pip_check() for command in later):
        offences.append("no `pip check` after the torch install in the same RUN")
    if not any("endswith('+cpu')" in " ".join(command.tokens) for command in later):
        offences.append("no `+cpu` assertion on torch.__version__ in the same RUN")
    return offences


def test_cpu_torch_comes_from_the_cpu_index() -> None:
    found: set[tuple[str, str]] = set()
    offences: list[str] = []
    for path in _dockerfiles():
        for stage in parse_stages(path.read_text()):
            uses_cpu_index = any(
                _CPU_TORCH_INDEX in (command.pip_install_args() or ())
                for command in _commands(stage)
            )
            if not uses_cpu_index:
                continue
            found.add((path.name, stage.name))
            offences += [f"{path.name} {stage.name}: {o}" for o in _cpu_torch_offences(stage)]
    assert found == _CPU_TORCH_STAGES
    assert not offences, "\n".join(offences)


def _extras_first_party(extra: str, extras: Mapping[str, list[str]]) -> frozenset[str]:
    first_party: set[str] = set()
    for requirement_text in extras[extra]:
        requirement = Requirement(requirement_text)
        name = canonicalize_name(requirement.name)
        if name == "tolokaforge":
            for nested in requirement.extras:
                first_party |= _extras_first_party(nested, extras)
        elif name in _first_party_names():
            first_party.add(name)
    return frozenset(first_party)


@dataclass(frozen=True)
class _CaseArm:
    patterns: tuple[str, ...]
    variable: str
    distribution: str


def _case_arms(tokens: list[str]) -> Iterator[_CaseArm]:
    """``case ",${TOLOKAFORGE_EXTRAS}," in <patterns>) VAR="... <wheel glob>" ;; esac`` arms."""
    for start, token in enumerate(tokens):
        if token != "case":
            continue
        assert tokens[start + 1 : start + 3] == [",${TOLOKAFORGE_EXTRAS},", "in"], tokens[start:]
        close = tokens.index(")", start)
        end = tokens.index(";;", close)
        body = " ".join(tokens[close + 1 : end])
        wheel = _PLUGIN_WHEEL_RE.search(body)
        assert wheel is not None, f"case arm installs no plugin wheel: {body}"
        patterns = tuple(t for t in tokens[start + 3 : close] if t != "|")
        variable = body.partition("=")[0]
        yield _CaseArm(patterns, variable, canonicalize_name(wheel.group("stem")))


def _standalone_builder() -> _Stage:
    stages = parse_stages((_DOCKERFILE_DIR / "standalone.Dockerfile").read_text())
    return next(stage for stage in stages if stage.name == "builder")


def test_standalone_installs_plugin_wheels_exactly_for_the_extras_that_require_them() -> None:
    extras = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    builder = _standalone_builder()
    tokens = [
        token
        for instruction in builder.instructions
        if instruction.keyword == "RUN"
        for token in _shell_tokens(instruction.args)
    ]
    arms = list(_case_arms(tokens))
    assert {arm.distribution for arm in arms} == {
        "tolokaforge-langfuse",
        "tolokaforge-adapter-terminal-bench",
    }
    for extra in extras:
        selected = frozenset(
            arm.distribution
            for arm in arms
            if any(fnmatchcase(f",{extra},", pattern) for pattern in arm.patterns)
        )
        assert selected == _extras_first_party(extra, extras), extra
    install = next(
        args
        for command in _commands(builder)
        if "${BASE_WHL}[${TOLOKAFORGE_EXTRAS}]" in (args := command.pip_install_args() or ())
    )
    for arm in arms:
        assert f"${{{arm.variable}}}" in install, (
            f"{arm.distribution} wheels in ${arm.variable} are not installed alongside the "
            "base wheel's extras, so pip may take the extra's requirement from an index"
        )


_LOCKED_STAGE = f"""
FROM python:3.12-slim AS builder
COPY {_CONSTRAINTS_SOURCE} /opt/tolokaforge/
ENV PIP_CONSTRAINT={_IMAGE_CONSTRAINTS} PIP_BUILD_CONSTRAINT={_IMAGE_CONSTRAINTS}
RUN pip install --no-cache-dir {{flags}} "hatchling>=1.24,<2.0" \\
    && python /opt/tolokaforge/locked_install_check.py --constraints {_IMAGE_CONSTRAINTS} \\
        --exempt pip
"""


def _sdist_only_flags() -> str:
    return f"--only-binary :all: --no-binary {','.join(sorted(linux_sdist_only_packages()))}"


def test_a_locked_stage_passes() -> None:
    text = _LOCKED_STAGE.format(flags=_sdist_only_flags())
    assert find_unlocked_installs(text, {"builder": _BOOTSTRAP}) == []


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        pytest.param(
            _LOCKED_STAGE.format(
                flags=f"--no-binary {','.join(sorted(linux_sdist_only_packages()))} "
                "--only-binary :all:"
            ),
            "`--no-binary` precedes `--only-binary :all:`",
            id="reversed-binary-flags",
        ),
        pytest.param(
            "FROM python:3.12-slim AS builder\n"
            'RUN pip install --no-cache-dir "hatchling>=1.24,<2.0"\n',
            "`PIP_CONSTRAINT` is not",
            id="unconstrained-stage",
        ),
        pytest.param(
            _LOCKED_STAGE.format(flags=_sdist_only_flags()).replace("hatchling", "tolokaforge"),
            "first-party `tolokaforge>=1.24,<2.0` installed by name",
            id="first-party-by-name",
        ),
        pytest.param(
            _LOCKED_STAGE.format(flags=_sdist_only_flags()).replace(
                "--exempt pip", "--exempt pip --exempt setuptools"
            ),
            "guard row",
            id="widened-guard-row",
        ),
        pytest.param(
            _LOCKED_STAGE.format(flags=f"{_sdist_only_flags()} --upgrade pip"),
            "`--upgrade`",
            id="upgrade-pip",
        ),
    ],
)
def test_an_unlocked_stage_is_rejected(text: str, reason: str) -> None:
    offences = find_unlocked_installs(text, {"builder": _BOOTSTRAP})
    assert any(reason in offence for offence in offences), offences
