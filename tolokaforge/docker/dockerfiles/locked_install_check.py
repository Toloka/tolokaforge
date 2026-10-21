"""Fail when an installed distribution escaped the image constraints file.

Runs inside images against the interpreter whose environment it checks, so it
imports only the standard library plus a marker evaluator: ``packaging`` when
the environment has it, otherwise the copy vendored in ``pip``.

Exit codes: 0 every distribution passes, 1 offenders found, 2 the check
itself could not run.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packaging.markers import Marker

_PACKAGING_ROOTS = ("packaging", "pip._vendor.packaging")
_PIN_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)(?:\s*;\s*(?P<marker>\S.*))?$"
)
_NAME_SEPARATORS_RE = re.compile(r"[-_.]+")


class GuardError(Exception):
    """The check could not run; maps to exit code 2."""


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    marker: Marker | None


@dataclass(frozen=True)
class InstalledDist:
    name: str
    version: str
    from_local_file: bool


class OffenceKind(str, Enum):
    NOT_CONSTRAINED = "not in constraints for this platform"
    VERSION_MISMATCH = "constraints pin"
    FIRST_PARTY_NOT_LOCAL = "first-party distribution not installed from a local file"


@dataclass(frozen=True)
class Offender:
    name: str
    version: str
    kind: OffenceKind
    pinned_versions: tuple[str, ...] = ()

    def __str__(self) -> str:
        detail = self.kind.value
        if self.kind is OffenceKind.VERSION_MISMATCH:
            detail = f"{detail} {', '.join(self.pinned_versions)}"
        return f"{self.name} {self.version}: {detail}"


def normalize_name(name: str) -> str:
    """PEP 503 normalized project name."""
    return _NAME_SEPARATORS_RE.sub("-", name).lower()


def _packaging_module(submodule: str) -> ModuleType:
    for root in _PACKAGING_ROOTS:
        try:
            return importlib.import_module(f"{root}.{submodule}")
        except ImportError:
            continue
    raise GuardError(
        f"no marker evaluator: neither of {', '.join(_PACKAGING_ROOTS)} is importable "
        f"from {sys.executable}"
    )


def parse_constraints(text: str) -> list[Pin]:
    """Parse ``name==version [; marker]`` lines, skipping blanks and ``#`` comments."""
    markers = _packaging_module("markers")
    version = _packaging_module("version")
    pins: list[Pin] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_LINE_RE.match(line)
        if match is None:
            raise GuardError(f"line {number}: not a `name==version [; marker]` pin: {line!r}")
        try:
            version.Version(match.group("version"))
            marker_text = match.group("marker")
            marker = markers.Marker(marker_text) if marker_text else None
        except (version.InvalidVersion, markers.InvalidMarker) as exc:
            raise GuardError(f"line {number}: {exc}: {line!r}")
        pins.append(Pin(normalize_name(match.group("name")), match.group("version"), marker))
    return pins


def applicable_pins(
    pins: Iterable[Pin], environment: Mapping[str, str]
) -> Mapping[str, frozenset[str]]:
    """Pinned versions per normalized name, keeping only pins whose marker matches *environment*."""
    applicable: dict[str, set[str]] = {}
    for pin in pins:
        if pin.marker is None or pin.marker.evaluate(dict(environment)):
            applicable.setdefault(pin.name, set()).add(pin.version)
    return {name: frozenset(versions) for name, versions in applicable.items()}


def _same_version(installed: str, pinned: str) -> bool:
    version = _packaging_module("version")
    try:
        return bool(version.Version(installed) == version.Version(pinned))
    except version.InvalidVersion:
        return False


def _offence(
    dist: InstalledDist,
    applicable: Mapping[str, frozenset[str]],
    first_party_names: set[str],
    exempt_names: set[str],
) -> Offender | None:
    name = normalize_name(dist.name)
    if name in exempt_names:
        return None
    if name in first_party_names:
        if dist.from_local_file:
            return None
        return Offender(name, dist.version, OffenceKind.FIRST_PARTY_NOT_LOCAL)
    pinned = applicable.get(name)
    if pinned is None:
        return Offender(name, dist.version, OffenceKind.NOT_CONSTRAINED)
    if any(_same_version(dist.version, version) for version in pinned):
        return None
    return Offender(name, dist.version, OffenceKind.VERSION_MISMATCH, tuple(sorted(pinned)))


def find_offenders(
    installed: Iterable[InstalledDist],
    applicable: Mapping[str, frozenset[str]],
    first_party: Iterable[str],
    exempt: Iterable[str],
) -> list[Offender]:
    """Every installed distribution that is neither exempt, a local-file first-party
    install, nor at a version an applicable pin names."""
    first_party_names = {normalize_name(name) for name in first_party}
    exempt_names = {normalize_name(name) for name in exempt}
    offences = (_offence(dist, applicable, first_party_names, exempt_names) for dist in installed)
    return sorted(
        (offender for offender in offences if offender is not None),
        key=lambda offender: offender.name,
    )


def _installed_from_local_file(dist: metadata.Distribution, name: str) -> bool:
    direct_url = dist.read_text("direct_url.json")
    if direct_url is None:
        return False
    try:
        url = json.loads(direct_url)["url"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GuardError(f"{name}: unreadable direct_url.json ({exc!r}): {direct_url!r}")
    return isinstance(url, str) and url.startswith("file://")


def installed_distributions(path: Sequence[str] | None = None) -> list[InstalledDist]:
    """Distributions visible on *path* (default ``sys.path``), with their PEP 610 origin."""
    found = metadata.distributions() if path is None else metadata.distributions(path=list(path))
    installed: list[InstalledDist] = []
    for dist in found:
        name = dist.metadata["Name"]
        if not name:
            raise GuardError(f"distribution metadata without a Name under {dist.locate_file('')}")
        installed.append(InstalledDist(name, dist.version, _installed_from_local_file(dist, name)))
    return installed


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail when an installed distribution escaped the image constraints file."
    )
    parser.add_argument("--constraints", required=True, type=Path)
    parser.add_argument("--first-party", action="append", default=[], metavar="NAME")
    parser.add_argument("--exempt", action="append", default=[], metavar="NAME")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        try:
            text = args.constraints.read_text(encoding="utf-8")
        except OSError as exc:
            raise GuardError(f"cannot read constraints file {args.constraints}: {exc}")
        environment = _packaging_module("markers").default_environment()
        applicable = applicable_pins(parse_constraints(text), environment)
        installed = installed_distributions()
        offenders = find_offenders(installed, applicable, args.first_party, args.exempt)
    except GuardError as exc:
        print(f"locked-install check could not run: {exc}", file=sys.stderr)
        return 2
    if offenders:
        for offender in offenders:
            print(offender, file=sys.stderr)
        return 1
    print(
        f"locked-install check: {len(installed)} distributions in {sys.prefix} match "
        f"{args.constraints} (first-party: {', '.join(sorted(args.first_party)) or 'none'}; "
        f"exempt: {', '.join(sorted(args.exempt)) or 'none'})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
