"""Fail when an installed distribution escaped the image constraints file.

A third-party distribution passes only at a version a constraint line pins for
this platform. A ``--first-party`` distribution passes only when its PEP 610
``direct_url.json`` names a ``file://`` wheel that still exists and whose
``RECORD`` lists exactly the files installed, so an index wheel that ships its
own ``direct_url.json`` naming a local wheel passes only if it installed exactly
that wheel's files. The comparison leaves out the rows pip or uv writes itself:
``INSTALLER``, ``REQUESTED``, ``direct_url.json``, uv's ``uv_cache.json``,
bytecode compiled from the wheel's own modules, and the console scripts its
``entry_points.txt`` declares. Run it before the wheels it verifies against are
deleted.

Runs inside images against the interpreter whose environment it checks, so it
imports only the standard library plus a marker evaluator: ``packaging`` when
the environment has it, otherwise the copy vendored in ``pip``.

Exit codes: 0 every distribution passes, 1 offenders found, 2 the check
itself could not run.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import re
import sys
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Protocol
from urllib.parse import unquote, urlparse

_PACKAGING_ROOTS = ("packaging", "pip._vendor.packaging")
_PIN_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)(?:\s*;\s*(?P<marker>\S.*))?$"
)
_NAME_SEPARATORS_RE = re.compile(r"[-_.]+")
_INSTALLER_WRITTEN_METADATA = frozenset(
    {"INSTALLER", "REQUESTED", "direct_url.json", "RECORD", "uv_cache.json"}
)
_SCRIPT_GROUPS = ("console_scripts", "gui_scripts")
_LIBRARY_DATA_DIRS = frozenset({"purelib", "platlib"})
_SHOWN_PATHS = 5


class GuardError(Exception):
    """The check could not run; maps to exit code 2."""


class MarkerEvaluator(Protocol):
    def evaluate(self, environment: dict[str, str] | None = None) -> bool: ...


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    marker: MarkerEvaluator | None


class Origin(str, Enum):
    INDEX = "index"
    LOCAL_WHEEL = "local_wheel"
    UNVERIFIED_LOCAL = "unverified_local"


@dataclass(frozen=True)
class InstalledDist:
    name: str
    version: str
    origin: Origin
    unverified_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.origin is Origin.UNVERIFIED_LOCAL) != (self.unverified_reason is not None):
            raise ValueError(
                f"{self.name}: unverified_reason must be set exactly for {Origin.UNVERIFIED_LOCAL}"
            )


class OffenceKind(str, Enum):
    NOT_CONSTRAINED = "not_constrained"
    VERSION_MISMATCH = "version_mismatch"
    FIRST_PARTY_NOT_LOCAL = "first_party_not_local"
    FIRST_PARTY_UNVERIFIED = "first_party_unverified"


@dataclass(frozen=True)
class Offender:
    name: str
    version: str
    kind: OffenceKind
    pinned_versions: tuple[str, ...] = ()
    unverified_reason: str | None = None

    def __str__(self) -> str:
        if self.kind is OffenceKind.NOT_CONSTRAINED:
            detail = "not in constraints for this platform"
        elif self.kind is OffenceKind.VERSION_MISMATCH:
            detail = f"constraints pin {', '.join(self.pinned_versions)}"
        elif self.kind is OffenceKind.FIRST_PARTY_NOT_LOCAL:
            detail = "first-party distribution not installed from a local file"
        else:
            detail = f"first-party distribution's {self.unverified_reason}"
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
        if dist.origin is Origin.LOCAL_WHEEL:
            return None
        if dist.origin is Origin.INDEX:
            return Offender(name, dist.version, OffenceKind.FIRST_PARTY_NOT_LOCAL)
        return Offender(
            name,
            dist.version,
            OffenceKind.FIRST_PARTY_UNVERIFIED,
            unverified_reason=dist.unverified_reason,
        )
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
    """Every installed distribution that is neither exempt, a verified local-wheel
    first-party install, nor at a version an applicable pin names."""
    first_party_names = {normalize_name(name) for name in first_party}
    exempt_names = {normalize_name(name) for name in exempt}
    offences = (_offence(dist, applicable, first_party_names, exempt_names) for dist in installed)
    return sorted(
        (offender for offender in offences if offender is not None),
        key=lambda offender: offender.name,
    )


class _Uncomparable(Exception):
    """A local wheel's RECORD cannot be compared with the installed one."""


@dataclass(frozen=True)
class _WheelRecord:
    dist_info: str
    rows: Mapping[str, str]


def _record_rows(text: str) -> dict[str, str]:
    """RECORD ``path -> hash`` rows."""
    return {row[0]: row[1] for row in csv.reader(text.splitlines()) if row}


def _installed_path(wheel_path: str) -> str:
    top, _, rest = wheel_path.partition("/")
    if not top.endswith(".data"):
        return wheel_path
    category, _, path = rest.partition("/")
    if category not in _LIBRARY_DATA_DIRS:
        raise _Uncomparable(f"it installs {top}/{category}/ outside site-packages")
    return path


def _read_wheel_record(wheel: Path) -> _WheelRecord:
    """The wheel's RECORD rows keyed by the path each file installs to."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            records = [
                name
                for name in archive.namelist()
                if name.count("/") == 1 and name.endswith(".dist-info/RECORD")
            ]
            if len(records) != 1:
                raise _Uncomparable(f"it holds {len(records)} .dist-info/RECORD files")
            text = archive.read(records[0]).decode("utf-8")
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError) as exc:
        raise _Uncomparable(f"it is unreadable ({exc})") from exc
    rows = {
        _installed_path(path): digest
        for path, digest in _record_rows(text).items()
        if path != records[0]
    }
    return _WheelRecord(records[0].split("/", 1)[0], rows)


def _installer_written(path: str, expected: _WheelRecord, scripts: frozenset[str]) -> bool:
    pure = PurePosixPath(path)
    if pure.parent.as_posix() == expected.dist_info:
        return pure.name in _INSTALLER_WRITTEN_METADATA
    if pure.parent.name == "__pycache__" and pure.suffix == ".pyc":
        source = pure.parent.parent / f"{pure.name.split('.', 1)[0]}.py"
        return source.as_posix() in expected.rows
    return path.startswith("../") and pure.name in scripts


def _differing_paths(dist: metadata.Distribution, expected: _WheelRecord) -> list[str]:
    installed_record = dist.read_text("RECORD")
    if installed_record is None:
        raise _Uncomparable("the installed distribution has no RECORD")
    scripts = frozenset(
        entry_point.name for entry_point in dist.entry_points if entry_point.group in _SCRIPT_GROUPS
    )
    installed = {
        (path, digest)
        for path, digest in _record_rows(installed_record).items()
        if not _installer_written(path, expected, scripts)
    }
    return sorted({path for path, _ in installed ^ set(expected.rows.items())})


def _local_wheel_origin(dist: metadata.Distribution, wheel: Path) -> tuple[Origin, str | None]:
    if not wheel.is_file():
        return Origin.UNVERIFIED_LOCAL, f"local wheel {wheel} does not exist"
    try:
        differing = _differing_paths(dist, _read_wheel_record(wheel))
    except _Uncomparable as exc:
        return Origin.UNVERIFIED_LOCAL, f"local wheel {wheel} cannot be compared: {exc}"
    if not differing:
        return Origin.LOCAL_WHEEL, None
    shown = ", ".join(differing[:_SHOWN_PATHS])
    more = f" and {len(differing) - _SHOWN_PATHS} more" if len(differing) > _SHOWN_PATHS else ""
    return Origin.UNVERIFIED_LOCAL, f"files differ from its local wheel {wheel}: {shown}{more}"


def _origin(dist: metadata.Distribution, name: str) -> tuple[Origin, str | None]:
    direct_url = dist.read_text("direct_url.json")
    if direct_url is None:
        return Origin.INDEX, None
    try:
        url = json.loads(direct_url)["url"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GuardError(f"{name}: unreadable direct_url.json ({exc!r}): {direct_url!r}")
    if not isinstance(url, str) or not url.startswith("file://"):
        return Origin.INDEX, None
    return _local_wheel_origin(dist, Path(unquote(urlparse(url).path)))


def installed_distributions(path: Sequence[str] | None = None) -> list[InstalledDist]:
    """Distributions visible on *path* (default ``sys.path``), with their verified origin."""
    found = metadata.distributions() if path is None else metadata.distributions(path=list(path))
    installed: list[InstalledDist] = []
    for dist in found:
        name = dist.metadata["Name"]
        if not name:
            raise GuardError(f"distribution metadata without a Name under {dist.locate_file('')}")
        installed.append(InstalledDist(name, dist.version, *_origin(dist, name)))
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
