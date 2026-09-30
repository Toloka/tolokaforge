"""Custom hatchling builder producing the runner-subset wheel.

Wired from ``pyproject.toml`` under ``[tool.hatch.build.targets.custom]``.
Invoked with ``hatch build --target custom`` (equivalently
``uv run hatch build --target custom``); the result is a wheel named
``tolokaforge_runner_subset-<version>-py3-none-any.whl`` under ``dist/``.

The set of Python files the subset ships is enumerated in
``tolokaforge/core/_runner_subset.py`` and consumed by the ``only-include``
/ ``exclude`` lists in ``[tool.hatch.build.targets.custom]``. The
canonical test ``tests/canonical/test_runner_subset_partition.py`` fails
CI if pyproject and the enumeration module ever disagree.

Every override in this module exists so the subset wheel:

- is named ``tolokaforge-runner-subset`` (not ``tolokaforge``) — pip's
  install metadata inside the runner Docker image makes clear which
  build variant is installed;
- declares only the dependencies the runner runtime graph needs — the
  base wheel's ``[project].dependencies`` reachable from the subset, plus
  the base wheel's ``[project.optional-dependencies].runner`` group, each
  with the base wheel's own specifier;
- declares one console-script entry — ``tolokaforge = tolokaforge.runner._cli:main`` —
  binding the subset-native CLI shim (ADR-0027) that preserves the ADR-0024
  ``docker exec`` surface (``tolokaforge --version`` / ``tolokaforge run-trial``)
  inside the slim image. The base wheel's ``tolokaforge = tolokaforge._entry:main``
  is deliberately not carried through: ``tolokaforge._entry`` / ``dx/cli/*`` are
  base-wheel only, so a subset wheel that exposed them would create a dangling
  reference. Other entry-point tables (runtime backends, trial-grader
  factories, conductors) still point at orchestrator-only modules and stay
  base-wheel only.

The subset wheel is a Docker-only build artifact. It is not — and
per ``docs/adr/0025-runner-wheel-split.md`` never will be — published to
PyPI. The published surface remains one ``tolokaforge`` wheel.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomllib
from hatchling.builders.wheel import WheelBuilder
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from hatchling.builders.wheel import RecordFile, WheelArchive


# Distribution name for the subset wheel. Import paths inside
# ``site-packages/`` are unchanged (``tolokaforge.runner``,
# ``tolokaforge.core.models``, ...); only the pip-level distribution
# identifier differs.
SUBSET_DISTRIBUTION_NAME = "tolokaforge-runner-subset"

# Base wheel entry-point groups the runner subset MUST carry. Every seam
# the runner reaches through ``load_*`` at boot or during a Grade RPC —
# the six sub-component seams reachable from ``RunnerServiceImpl``
# (ADR-0040) — is loaded via ``importlib.metadata.entry_points``, so the
# group's rows must appear in the subset wheel's ``entry_points.txt``
# even though their target modules are already inside the subset
# partition. Groups NOT listed here — ``runtime_backends``,
# ``trial_graders``, ``conductors``, ``service_readiness_probes``,
# ``turn_policies``, ``agent_loops`` (all called from
# ``tolokaforge.core.runner``, which lives outside the subset
# partition; the runner container calls
# ``tolokaforge.runner.__main__`` instead), and ``grading_substrates``
# (runner instantiates ``InProcessGradingSubstrate`` directly; the
# group's ``live_callback`` row targets ``substrate_live.py``, which is
# grader-side and excluded from the subset partition) — are
# deliberately kept out. The
# ``test_subset_partition_load_calls_are_in_the_allowlist`` drift-lock
# guards this list against a new runner-side ``load_*`` call being
# added silently.
RUNNER_REACHABLE_ENTRY_POINT_GROUPS: tuple[str, ...] = (
    "tolokaforge.custom_check_executors",
    "tolokaforge.judge_model_providers",
    "tolokaforge.rubric_evaluators",
    "tolokaforge.transcript_rule_matchers",
    "tolokaforge.state_check_backends",
    "tolokaforge.trace_check_operators",
    "tolokaforge.grading_methods",
    "tolokaforge.grader_kinds",
    "tolokaforge.judge_kinds",
)


def _read_pyproject_entry_points() -> dict[str, dict[str, str]]:
    """Read ``[project.entry-points.*]`` tables from the repo-root pyproject."""
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    return data.get("project", {}).get("entry-points", {}) or {}


def _build_subset_entry_points() -> str:
    """Assemble the subset wheel's ``entry_points.txt`` contents.

    Carries the subset-native ``[console_scripts]`` shim (ADR-0027) plus,
    for every group named in :data:`RUNNER_REACHABLE_ENTRY_POINT_GROUPS`,
    the base wheel's registrations verbatim. Reading pyproject at build
    time keeps the subset's registrations from drifting when a new seam
    entry is added to the base wheel.
    """
    lines = ["[console_scripts]", "tolokaforge = tolokaforge.runner._cli:main"]
    pyproject_ep = _read_pyproject_entry_points()
    for group in RUNNER_REACHABLE_ENTRY_POINT_GROUPS:
        rows = pyproject_ep.get(group)
        if not rows:
            raise RuntimeError(
                f"runner-reachable entry-point group {group!r} missing from "
                "pyproject.toml — check "
                "RUNNER_REACHABLE_ENTRY_POINT_GROUPS against "
                "[project.entry-points.*]"
            )
        lines.append("")
        lines.append(f"[{group}]")
        for name, target in rows.items():
            lines.append(f"{name} = {target}")
    return "\n".join(lines) + "\n"


# The subset wheel's ``entry_points.txt`` — the subset-native CLI shim's
# ``[project.scripts]`` binding plus every runner-reachable seam group.
# Kept as a module constant so ``pip show``,
# ``importlib.metadata.entry_points``, and the canonical drift-lock test
# all read the same string. See ADR-0027 for the shim and ADR-0040 for
# the seams.
SUBSET_ENTRY_POINTS: str = _build_subset_entry_points()


# Runtime dependencies the runner container needs, named without version
# specifiers. Each emitted ``Requires-Dist`` is the base project's own
# requirement with the same name and extras, read from hatch's metadata at
# build time, so a range edited in ``pyproject.toml`` reaches the runner image
# without a second edit here. The set itself is locked by
# ``tests/canonical/test_runner_subset_partition.py``.
#
# Selected from:
#   - the base wheel's ``[project.dependencies]`` — every entry the runner
#     subset's import graph reaches at runtime, including
#     ``tolokaforge-models`` which supplies the pricing / preset / provider
#     binding data files the runner's model-data accessors resolve;
#   - the base wheel's ``[project.optional-dependencies].runner`` — the
#     domain-tool runtime deps (fastapi/uvicorn/sqlalchemy/asyncpg/...).
#
# ``docker`` and ``testcontainers`` are intentionally omitted: they are
# reached only from orchestrator-side runtime backends
# (``per_trial_runtime``, ``shared_stack_runtime``, ``docker_adapter``)
# which live in the base wheel and are not shipped inside the subset.
SUBSET_REQUIREMENT_NAMES: tuple[str, ...] = (
    # Reachable from the runner subset (subset of ``[project.dependencies]``).
    "tolokaforge-models",
    "litellm",
    "pydantic",
    "pydantic[email]",
    "jsonschema",
    "packaging",
    "pyyaml",
    "python-dotenv",
    "click",
    "jsonpath-ng",
    "httpx",
    "tenacity",
    "jinja2",
    "loguru",
    "docstring_parser",
    "deepdiff",
    "toml",
    "addict",
    "starlette",
    "typesense",
    "structlog",
    # The generated runner_pb2 / runner_pb2_grpc modules the subset ships
    # check the grpcio and protobuf runtime versions at import time.
    "grpcio",
    "grpcio-health-checking",
    "protobuf",
    "mcp",
    # Domain-tool runtime deps (``[project.optional-dependencies].runner``).
    "asyncpg",
    "psycopg2-binary",
    "alembic",
    "python-jose",
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "odata-query",
)

RequirementKey = tuple[str, frozenset[str]]


def requirement_key(requirement: Requirement) -> RequirementKey:
    """Identity of a requirement irrespective of its specifier and marker."""
    return (canonicalize_name(requirement.name), frozenset(requirement.extras))


def select_subset_requirements(
    selection: Sequence[str], base_requirements: Iterable[str]
) -> list[str]:
    """Return the base requirement for each selected name, in selection order.

    Raises ``ValueError`` when a selected name carries a specifier, marker or
    URL, matches no base requirement, or matches more than one.
    """
    base_by_key: dict[RequirementKey, list[str]] = {}
    for base in base_requirements:
        base_by_key.setdefault(requirement_key(Requirement(base)), []).append(base)
    selected: list[str] = []
    for name in selection:
        wanted = Requirement(name)
        if str(wanted.specifier) or wanted.marker is not None or wanted.url:
            raise ValueError(
                f"runner-subset selection {name!r} must name a requirement "
                "without a specifier, marker or URL — the specifier comes from "
                "pyproject.toml"
            )
        matches = base_by_key.get(requirement_key(wanted), [])
        if len(matches) != 1:
            raise ValueError(
                f"runner-subset selection {name!r} matches {len(matches)} "
                "requirements in pyproject.toml [project.dependencies] ∪ "
                f"[project.optional-dependencies].runner, expected 1: {matches}"
            )
        selected.append(matches[0])
    return selected


class RunnerSubsetBuilder(WheelBuilder):
    """WheelBuilder subclass producing the runner-subset Docker-only wheel."""

    PLUGIN_NAME = "custom"

    @property
    def project_id(self) -> str:
        return (
            f"{self.normalize_file_name_component(SUBSET_DISTRIBUTION_NAME)}"
            f"-{self.metadata.version}"
        )

    @property
    def artifact_project_id(self) -> str:
        return self.project_id

    def construct_entry_points_file(self) -> str:  # type: ignore[override]
        """Emit the subset wheel's ``entry_points.txt``.

        The single ``[console_scripts]`` entry binds the subset-native CLI
        shim so pip registers a ``tolokaforge`` console script inside the
        runner image. See ADR-0027 for the full rationale."""
        return SUBSET_ENTRY_POINTS

    def write_project_metadata(
        self,
        archive: WheelArchive,
        records: RecordFile,
        extra_dependencies: Sequence[str] = (),
    ) -> None:
        record = archive.write_metadata(
            "METADATA", self._construct_subset_metadata_file(extra_dependencies)
        )
        records.write(record)

    def _construct_subset_metadata_file(self, extra_dependencies: Sequence[str]) -> str:
        core: Any = self.metadata.core
        parts: list[str] = ["Metadata-Version: 2.3"]
        parts.append(f"Name: {SUBSET_DISTRIBUTION_NAME}")
        parts.append(f"Version: {self.metadata.version}")
        base_summary = core.description or ""
        parts.append(
            "Summary: Tolokaforge runner-subset wheel — Docker-only build "
            "artifact, not published to PyPI"
            + (f" (base project summary: {base_summary})" if base_summary else "")
        )
        if core.urls:
            for label, url in core.urls.items():
                parts.append(f"Project-URL: {label}, {url}")
        if core.requires_python:
            parts.append(f"Requires-Python: {core.requires_python}")
        if core.license_expression:
            parts.append(f"License: {core.license_expression}")
        elif core.license:
            for i, license_line in enumerate(core.license.splitlines()):
                prefix = "License: " if i == 0 else "         "
                parts.append(f"{prefix}{license_line}")
        base_requirements = [*core.dependencies, *core.optional_dependencies["runner"]]
        for dep in select_subset_requirements(SUBSET_REQUIREMENT_NAMES, base_requirements):
            parts.append(f"Requires-Dist: {dep}")
        for dep in extra_dependencies:
            parts.append(f"Requires-Dist: {dep}")
        return "\n".join(parts) + "\n"


def get_builder() -> type[RunnerSubsetBuilder]:
    """Plugin-loader entry point.

    ``hatchling.plugin.utils.load_plugin_from_script`` finds every
    ``BuilderInterface`` subclass in this file — including ``WheelBuilder``
    itself, which is imported for subclassing. Returning
    ``RunnerSubsetBuilder`` explicitly disambiguates the selection.
    """
    return RunnerSubsetBuilder
