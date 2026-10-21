"""Read ``uv.lock`` and its pip-constraints export, ``tolokaforge/docker/image-constraints.txt``."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from urllib.parse import unquote

from packaging import tags
from packaging.markers import Marker
from packaging.utils import canonicalize_name, parse_wheel_filename

REPO_ROOT = Path(__file__).resolve().parents[2]
UV_LOCK_PATH = REPO_ROOT / "uv.lock"
IMAGE_CONSTRAINTS_PATH = REPO_ROOT / "tolokaforge" / "docker" / "image-constraints.txt"
IMAGE_PYTHON_VERSION = (REPO_ROOT / ".python-version").read_text().strip()
IMAGE_ARCHITECTURES = ("x86_64", "aarch64")

_PIN_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>\S+?)(?: ; (?P<marker>.+))?$"
)
_MANYLINUX_PLATFORM_RE = re.compile(r"^manylinux(?:1|2010|2014|_\d+_\d+)_(?P<arch>\w+)$")


@dataclass(frozen=True)
class ConstraintPin:
    name: str
    version: str
    marker: Marker | None


def parse_constraint_lines(text: str) -> list[ConstraintPin]:
    """Parse the export; raises ``ValueError`` naming any line that is not ``name==version [; marker]``."""
    pins: list[ConstraintPin] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _PIN_LINE_RE.match(line)
        if match is None:
            raise ValueError(
                f"{IMAGE_CONSTRAINTS_PATH.name}: not a `name==version [; marker]` pin: {line!r}"
            )
        marker = match.group("marker")
        pins.append(
            ConstraintPin(
                name=canonicalize_name(match.group("name")),
                version=match.group("version"),
                marker=Marker(marker) if marker else None,
            )
        )
    return pins


def lock_registry_packages() -> list[Mapping[str, object]]:
    """Every ``[[package]]`` entry of ``uv.lock`` resolved from a package index."""
    lock = tomllib.loads(UV_LOCK_PATH.read_text())
    return [package for package in lock["package"] if "registry" in package["source"]]


def lock_workspace_names() -> frozenset[str]:
    """Normalized names of the ``uv.lock`` packages built from this tree rather than an index."""
    lock = tomllib.loads(UV_LOCK_PATH.read_text())
    return frozenset(
        canonicalize_name(str(package["name"]))
        for package in lock["package"]
        if "registry" not in package["source"]
    )


def lock_registry_pins() -> frozenset[tuple[str, str]]:
    return frozenset(
        (canonicalize_name(str(package["name"])), str(package["version"]))
        for package in lock_registry_packages()
    )


def _linux_environment(arch: str) -> dict[str, str]:
    return {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": arch,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "python_version": IMAGE_PYTHON_VERSION,
        "python_full_version": f"{IMAGE_PYTHON_VERSION}.0",
    }


def _image_interpreter_abis() -> frozenset[tuple[str, str]]:
    version = tuple(int(part) for part in IMAGE_PYTHON_VERSION.split("."))
    interpreter = f"cp{version[0]}{version[1]}"
    supported = chain(
        tags.cpython_tags(version, platforms=["placeholder"]),
        tags.compatible_tags(version, interpreter, platforms=["placeholder"]),
    )
    return frozenset((tag.interpreter, tag.abi) for tag in supported)


def _wheel_installs_on_linux(
    wheel_filename: str, arch: str, interpreter_abis: frozenset[tuple[str, str]]
) -> bool:
    _, _, _, wheel_tags = parse_wheel_filename(wheel_filename)
    for tag in wheel_tags:
        if (tag.interpreter, tag.abi) not in interpreter_abis:
            continue
        manylinux = _MANYLINUX_PLATFORM_RE.match(tag.platform)
        if tag.platform == "any" or (manylinux is not None and manylinux.group("arch") == arch):
            return True
    return False


def _wheel_filenames(package: Mapping[str, object]) -> Iterator[str]:
    wheels = package.get("wheels", [])
    assert isinstance(wheels, list)
    for wheel in wheels:
        yield unquote(str(wheel["url"]).rsplit("/", 1)[1])


def _sdist_only_on(
    arch: str,
    pins: list[ConstraintPin],
    packages_by_pin: Mapping[tuple[str, str], Mapping[str, object]],
    interpreter_abis: frozenset[tuple[str, str]],
) -> set[str]:
    environment = _linux_environment(arch)
    applicable = [pin for pin in pins if pin.marker is None or pin.marker.evaluate(environment)]
    return {
        pin.name
        for pin in applicable
        if not any(
            _wheel_installs_on_linux(wheel, arch, interpreter_abis)
            for wheel in _wheel_filenames(packages_by_pin[(pin.name, pin.version)])
        )
    }


def linux_sdist_only_packages() -> frozenset[str]:
    """Names whose Linux-applicable pin has no ``any`` or manylinux wheel for the image interpreter on some image arch.

    This is the set every first-party image ``pip install`` exempts with ``--no-binary``.
    """
    packages_by_pin = {
        (canonicalize_name(str(package["name"])), str(package["version"])): package
        for package in lock_registry_packages()
    }
    interpreter_abis = _image_interpreter_abis()
    pins = parse_constraint_lines(IMAGE_CONSTRAINTS_PATH.read_text())
    return frozenset(
        chain.from_iterable(
            _sdist_only_on(arch, pins, packages_by_pin, interpreter_abis)
            for arch in IMAGE_ARCHITECTURES
        )
    )
