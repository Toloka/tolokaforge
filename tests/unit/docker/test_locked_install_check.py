"""Locks the locked-install guard that every first-party image runs after its installs."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.utils.image_constraints import IMAGE_CONSTRAINTS_PATH, parse_constraint_lines
from tests.utils.wheel_builds import hatchling_build
from tolokaforge.docker.dockerfiles import locked_install_check as guard
from tolokaforge.docker.dockerfiles.locked_install_check import (
    GuardError,
    InstalledDist,
    OffenceKind,
    Offender,
)

GUARD_SCRIPT = Path(guard.__file__)
LINUX = {"sys_platform": "linux", "platform_system": "Linux", "platform_machine": "x86_64"}
DARWIN = {"sys_platform": "darwin", "platform_system": "Darwin", "platform_machine": "arm64"}
PROBE_PROJECT = "lockguard-probe"


def _committed_pins() -> list[guard.Pin]:
    return guard.parse_constraints(IMAGE_CONSTRAINTS_PATH.read_text())


def _offenders(
    installed: list[InstalledDist],
    applicable: Mapping[str, frozenset[str]],
    first_party: tuple[str, ...] = (),
    exempt: tuple[str, ...] = (),
) -> list[str]:
    return [str(o) for o in guard.find_offenders(installed, applicable, first_party, exempt)]


def _run_guard(
    python: Path | str, constraints: Path, *flags: str, python_flags: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), *python_flags, str(GUARD_SCRIPT), "--constraints", str(constraints), *flags],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.unit
def test_committed_file_parses_to_the_same_pins_as_the_lock_reader() -> None:
    guard_view = {
        (p.name, p.version, str(p.marker) if p.marker else None) for p in _committed_pins()
    }
    lock_view = {
        (p.name, p.version, str(p.marker) if p.marker else None)
        for p in parse_constraint_lines(IMAGE_CONSTRAINTS_PATH.read_text())
    }
    assert guard_view == lock_view
    assert len(guard_view) > 100


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment", "installed_version", "expected"),
    [
        (LINUX, "2.14.1", ["torch 2.14.1: constraints pin 2.14.1+cpu"]),
        (LINUX, "2.14.1+cpu", []),
        (DARWIN, "2.14.1", []),
        (DARWIN, "2.14.1+cpu", ["torch 2.14.1+cpu: constraints pin 2.14.1"]),
    ],
    ids=["linux-plain", "linux-cpu", "darwin-plain", "darwin-cpu"],
)
def test_torch_pin_is_chosen_by_marker(
    environment: dict[str, str], installed_version: str, expected: list[str]
) -> None:
    applicable = guard.applicable_pins(_committed_pins(), environment)
    assert _offenders([InstalledDist("torch", installed_version, False)], applicable) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("installed", "first_party", "exempt", "expected"),
    [
        pytest.param(InstalledDist("litellm", "1.93.0", False), (), (), [], id="pinned-version"),
        pytest.param(
            InstalledDist("litellm", "1.107.0", False),
            (),
            (),
            ["litellm 1.107.0: constraints pin 1.93.0"],
            id="version-mismatch",
        ),
        pytest.param(
            InstalledDist("left-pad", "1.0", False),
            (),
            (),
            ["left-pad 1.0: not in constraints for this platform"],
            id="absent-name",
        ),
        pytest.param(
            InstalledDist("Typing_Extensions", "4.15.0", False),
            (),
            (),
            [],
            id="installed-name-normalized",
        ),
        pytest.param(
            InstalledDist("tolokaforge-models", "1.8.1", False),
            ("tolokaforge_models",),
            (),
            ["tolokaforge-models 1.8.1: first-party distribution not installed from a local file"],
            id="first-party-from-index",
        ),
        pytest.param(
            InstalledDist("tolokaforge-models", "1.8.1", True),
            ("Tolokaforge.Models",),
            (),
            [],
            id="first-party-from-local-file",
        ),
        pytest.param(
            InstalledDist("litellm", "1.107.0", True),
            ("tolokaforge",),
            (),
            ["litellm 1.107.0: constraints pin 1.93.0"],
            id="local-file-third-party-still-pinned",
        ),
        pytest.param(InstalledDist("Pip", "25.0.1", False), (), ("PIP",), [], id="exempt-absent"),
        pytest.param(
            InstalledDist("litellm", "1.107.0", False), (), ("litellm",), [], id="exempt-mismatch"
        ),
    ],
)
def test_find_offenders(
    installed: InstalledDist,
    first_party: tuple[str, ...],
    exempt: tuple[str, ...],
    expected: list[str],
) -> None:
    applicable = {
        "litellm": frozenset({"1.93.0"}),
        "typing-extensions": frozenset({"4.15.0"}),
    }
    assert _offenders([installed], applicable, first_party, exempt) == expected


@pytest.mark.unit
def test_offender_carries_kind_and_pins() -> None:
    [offender] = guard.find_offenders(
        [InstalledDist("torch", "2.14.1", False)],
        {"torch": frozenset({"2.14.1+cpu"})},
        first_party=(),
        exempt=(),
    )
    assert offender == Offender("torch", "2.14.1", OffenceKind.VERSION_MISMATCH, ("2.14.1+cpu",))


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "--index-url https://download.pytorch.org/whl/cpu",
        "litellm>=1.93.0",
        "-e ./tolokaforge_models",
        "litellm==not a version",
        "torch==2.14.1 ; sys_platform =! 'darwin'",
    ],
)
def test_unparseable_line_is_refused(line: str) -> None:
    with pytest.raises(GuardError, match="line 2"):
        guard.parse_constraints(f"# header\n{line}\n")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("direct_url", "from_local_file"),
    [
        (None, False),
        ('{"url": "file:///wheels/probe-0.1.0-py3-none-any.whl", "archive_info": {}}', True),
        ('{"url": "file:///src/probe", "dir_info": {"editable": true}}', True),
        (
            '{"url": "https://files.example/probe-0.1.0-py3-none-any.whl", "archive_info": {}}',
            False,
        ),
    ],
    ids=["no-direct-url", "local-wheel", "local-editable", "remote-url"],
)
def test_origin_is_read_from_direct_url_json(
    tmp_path: Path, direct_url: str | None, from_local_file: bool
) -> None:
    _write_dist_info(tmp_path, direct_url)
    assert guard.installed_distributions([str(tmp_path)]) == [
        InstalledDist("probe", "0.1.0", from_local_file)
    ]


@pytest.mark.unit
def test_malformed_direct_url_json_is_refused(tmp_path: Path) -> None:
    _write_dist_info(tmp_path, '{"dir_info": {}}')
    with pytest.raises(GuardError, match="probe: unreadable direct_url.json"):
        guard.installed_distributions([str(tmp_path)])


def _write_dist_info(site_packages: Path, direct_url: str | None) -> None:
    dist_info = site_packages / "probe-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: probe\nVersion: 0.1.0\n")
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(direct_url)


@pytest.mark.unit
def test_cli_flags_every_unconstrained_distribution(tmp_path: Path) -> None:
    constraints = tmp_path / "empty.txt"
    constraints.write_text("")
    result = _run_guard(sys.executable, constraints)
    assert result.returncode == 1, result.stderr
    assert any(
        line.startswith("pytest ") and line.endswith(": not in constraints for this platform")
        for line in result.stderr.splitlines()
    ), result.stderr


@pytest.mark.unit
def test_cli_refuses_a_missing_constraints_file(tmp_path: Path) -> None:
    result = _run_guard(sys.executable, tmp_path / "absent.txt")
    assert result.returncode == 2
    assert "cannot read constraints file" in result.stderr


@pytest.mark.unit
def test_cli_refuses_an_unparseable_constraints_file(tmp_path: Path) -> None:
    constraints = tmp_path / "ranges.txt"
    constraints.write_text("litellm>=1.93.0\n")
    result = _run_guard(sys.executable, constraints)
    assert result.returncode == 2
    assert "line 1" in result.stderr


@pytest.mark.unit
def test_cli_refuses_to_run_without_a_marker_evaluator(tmp_path: Path) -> None:
    constraints = tmp_path / "empty.txt"
    constraints.write_text("")
    result = _run_guard(sys.executable, constraints, python_flags=("-S",))
    assert result.returncode == 2
    assert "no marker evaluator" in result.stderr


@pytest.mark.unit
def test_module_entry_point_matches_the_script(tmp_path: Path) -> None:
    constraints = tmp_path / "empty.txt"
    constraints.write_text("")
    result = subprocess.run(
        [sys.executable, "-m", guard.__name__, "--constraints", str(constraints)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1
    assert "pytest " in result.stderr


@pytest.fixture(scope="module")
def probe_venv(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A venv with only pip, plus the wheel of a trivial local project; returns (python, wheel)."""
    project = tmp_path_factory.mktemp("probe_project")
    (project / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[project]\n"
        f'name = "{PROBE_PROJECT}"\n'
        'version = "0.1.0"\n'
    )
    package = project / "lockguard_probe"
    package.mkdir()
    (package / "__init__.py").write_text("")
    wheel = hatchling_build(
        project_dir=project,
        target="wheel",
        dist_dir=tmp_path_factory.mktemp("probe_dist"),
        wheel_glob="lockguard_probe-*.whl",
        label=PROBE_PROJECT,
    )
    venv_dir = tmp_path_factory.mktemp("probe_venv")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, capture_output=True)
    return venv_dir / "bin" / "python", wheel


def _pip_install(python: Path, *args: str) -> None:
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--no-index", "--no-deps", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def _site_packages(python: Path) -> str:
    return subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.canonical
def test_local_file_origin_is_read_from_a_real_install(
    probe_venv: tuple[Path, Path], tmp_path: Path
) -> None:
    python, wheel = probe_venv
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    flags = ("--first-party", PROBE_PROJECT, "--exempt", "pip")

    _pip_install(python, "--force-reinstall", str(wheel))
    installed = guard.installed_distributions([_site_packages(python)])
    assert InstalledDist(PROBE_PROJECT, "0.1.0", True) in installed
    assert next(d for d in installed if d.name == "pip").from_local_file is False
    by_path = _run_guard(python, empty, *flags)
    assert by_path.returncode == 0, by_path.stderr
    assert by_path.stdout.startswith("locked-install check: 2 distributions"), by_path.stdout

    _pip_install(python, "--force-reinstall", "--find-links", str(wheel.parent), PROBE_PROJECT)
    assert InstalledDist(PROBE_PROJECT, "0.1.0", False) in guard.installed_distributions(
        [_site_packages(python)]
    )
    by_name = _run_guard(python, empty, *flags)
    assert by_name.returncode == 1
    assert by_name.stderr.splitlines() == [
        f"{PROBE_PROJECT} 0.1.0: first-party distribution not installed from a local file"
    ]
