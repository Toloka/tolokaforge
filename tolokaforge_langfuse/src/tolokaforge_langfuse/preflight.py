"""One reader of a deployment's Langfuse block for both producers, and the pre-check of a run.

A deployment configures Langfuse in the tolokaforge run configuration itself, under
``observability.tracing.options.langfuse``: in a run config, or once for every run under
``run_defaults`` of the enclosing ``project.yaml`` (``docs/OBSERVABILITY.md``, "The deployment
profile"). Two producers read that block:

- the live observer (:mod:`tolokaforge_langfuse.plugin`), from the engine's merged run config;
- the offline connector, which depends on this wheel and never on the engine, through
  :func:`load_langfuse_block`.

Both compute the same :class:`TracingPlan` with :func:`resolve_plan`: the tags with their origins,
the metadata, the native environment, the profile, the model-name resolver and the project the
credentials must open. Everything here imports no engine module at load time (a test pins it);
only the command below reaches for the engine, lazily, to layer a run config the way
``tolokaforge run`` does.

``python -m tolokaforge_langfuse.preflight --config <run or shard config> [--environment <name>]
[--tags a:b,...] [--metadata k=v,...] [--offline]`` prints the plan a run would trace under and
exits 2 on the first error: no block, no ``project``, no ``environments``, an undeclared
environment or one that accepts no trial, a tag conflict, a missing required tag, or an engine
without the trial-observer seam. ``--offline`` reads one YAML file (a ``project.yaml`` or a run
config) without the engine, the way the connector does.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError

from tolokaforge_langfuse import __api_version__, __version__
from tolokaforge_langfuse.config import ACCEPTS_TRIAL, LangfuseConfig
from tolokaforge_langfuse.model_names import (
    NONE,
    ModelNameResolver,
    ModelNameResolverError,
    build_model_name_resolver,
)
from tolokaforge_langfuse.profile import (
    ENVIRONMENT_ENV,
    METADATA_ENV,
    NO_PROFILE,
    PROFILE_ENV,
    TracingProfile,
    TracingProfileError,
    check_caller_inputs,
    check_environment,
    describe,
    load_tracing_profile,
    parse_metadata_variable,
    profile_from_mapping,
)
from tolokaforge_langfuse.vocabulary import (
    DEFAULT_ENVIRONMENT_RULE,
    VocabularyError,
    validate_caller_tag,
)

__all__ = [
    "PROJECT_FILENAME",
    "LangfuseBlock",
    "PreflightError",
    "TracingPlan",
    "anchor_directory",
    "find_project_yaml",
    "load_langfuse_block",
    "main",
    "merge_metadata",
    "merge_tag_sources",
    "producer_version",
    "read_settings",
    "resolve_environment",
    "resolve_model_names",
    "resolve_plan",
    "resolve_profile",
]

PROJECT_FILENAME = "project.yaml"
# the engine's loader looks in the start directory and this many parents
PROJECT_SEARCH_PARENTS = 8
TRACING_TAGS_ENV = "TOLOKAFORGE_TRACING_TAGS"
TRACING_EXPECT_PROJECT_ENV = "TOLOKAFORGE_TRACING_EXPECT_PROJECT"
LANGFUSE_PROJECT_ENV = "LANGFUSE_PROJECT"
_BLOCK_KEYS = ("observability", "tracing", "options", "langfuse")
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


class PreflightError(ValueError):
    """The block, or a launcher's inputs, cannot be honoured; the message names the key."""


class TracingInputs(Protocol):
    """What the plan reads of a tracing section (the engine's ``TracingConfig`` has it)."""

    tags: Sequence[str]
    metadata: Mapping[str, Any]
    options: Mapping[str, Any]


class LangfuseBlock(NamedTuple):
    """A validated ``options.langfuse`` block and the directory its relative paths anchor to."""

    config: LangfuseConfig
    base_dir: Path


@dataclass(frozen=True)
class _Section:
    tags: Sequence[str]
    metadata: Mapping[str, Any]
    options: Mapping[str, Any]


@dataclass(frozen=True)
class TracingPlan:
    """What a run traces under, computed before any receiver is contacted."""

    settings: LangfuseConfig
    base_dir: Path
    profile: TracingProfile
    tags: tuple[str, ...]
    origins: Mapping[str, str]
    metadata: Mapping[str, Any]
    environment: str | None
    expect_project: str | None
    resolver: ModelNameResolver
    warnings: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        """The native ``version`` field the live producer writes under this plan."""
        return producer_version(
            f"tolokaforge-langfuse-{__version__}", self.resolver.rules_version, self.profile
        )

    def lines(self) -> list[str]:
        """The plan as the preflight prints it (names and values, never a credential)."""
        tags = ", ".join(
            f"{tag} ({self.origins.get(tag.partition(':')[0], '?')})" for tag in self.tags
        )
        return [
            f"project: {self.expect_project or '(none)'}"
            + (f" (id {self.settings.project_id})" if self.settings.project_id else ""),
            f"environment: {self.environment or '(the receiver default)'}",
            f"tags: {tags or '(none)'}",
            f"metadata keys: {sorted(self.metadata) or '(none)'}",
            f"profile: {describe(self.profile) if self.profile.present else 'none'}",
            f"model-name rules: {self.resolver.rules_version}",
            f"version: {self.version}",
            f"relative paths anchor to: {self.base_dir}",
        ]


def find_project_yaml(start: Path) -> Path | None:
    """The nearest ``project.yaml`` in ``start`` (a file's directory) or its parents, the engine
    loader's own walk."""
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for _ in range(PROJECT_SEARCH_PARENTS + 1):
        candidate = current / PROJECT_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
    return None


def anchor_directory(cwd: Path | None = None) -> Path:
    """Where a live run's relative paths anchor: the directory of the nearest ``project.yaml``
    above the working directory, else the working directory (the plugin receives the block
    without the file it came from)."""
    here = Path.cwd() if cwd is None else Path(cwd)
    found = find_project_yaml(here)
    return found.parent if found is not None else here.resolve()


def _placeholders(node: Any, where: str) -> list[str]:
    if isinstance(node, str):
        return [where] if _PLACEHOLDER.search(node) else []
    if isinstance(node, Mapping):
        return [
            hit for key, value in node.items() for hit in _placeholders(value, f"{where}.{key}")
        ]
    if isinstance(node, list):
        return [
            hit for i, value in enumerate(node) for hit in _placeholders(value, f"{where}[{i}]")
        ]
    return []


def _read_yaml(path: Path) -> Any:
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise PreflightError(f"{path}: cannot be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PreflightError(f"{path}: not valid YAML: {exc}") from exc


def _tracing_section(data: Any, path: Path) -> tuple[Mapping[str, Any], str]:
    """The ``observability.tracing`` mapping of a run config (top level) or of a
    ``project.yaml`` (under ``run_defaults``), and the key path it sits at."""
    if not isinstance(data, Mapping):
        raise PreflightError(f"{path}: the document must be a mapping")
    is_project = path.name == PROJECT_FILENAME or "run_defaults" in data
    if is_project:
        if "observability" in data:
            raise PreflightError(
                f"{path}: 'observability' sits at the top level of a project file, where the"
                " engine ignores it; a project's settings go under run_defaults"
            )
        root: Any = data.get("run_defaults")
        prefix = "run_defaults."
    else:
        root = data
        prefix = ""
    node: Any = root
    walked = prefix.rstrip(".")
    for key in _BLOCK_KEYS[:2]:
        if not isinstance(node, Mapping) or key not in node:
            keys = sorted(node) if isinstance(node, Mapping) else []
            raise PreflightError(
                f"{path}: no {prefix}observability.tracing.options.langfuse block"
                f" ({walked or 'the top level'} has keys {keys})"
            )
        node = node[key]
        walked = f"{walked}.{key}" if walked else key
    if not isinstance(node, Mapping):
        raise PreflightError(f"{path}: {walked} must be a mapping")
    return node, walked


def load_langfuse_block(path: str | Path) -> LangfuseBlock:
    """Find, validate and anchor the ``observability.tracing.options.langfuse`` block of one
    YAML file (a run config, or a ``project.yaml`` under ``run_defaults``). No engine module is
    imported: this is the offline connector's reader. A ``${...}`` placeholder inside the block
    is refused, because the file is read as written and never interpolated here."""
    path = Path(path)
    data = _read_yaml(path)
    section, where = _tracing_section(data, path)
    options = section.get("options")
    if not isinstance(options, Mapping) or "langfuse" not in options:
        raise PreflightError(f"{path}: no {where}.options.langfuse block")
    block = options["langfuse"]
    found = _placeholders(block, f"{where}.options.langfuse")
    if found:
        raise PreflightError(
            f"{path}: ${{...}} placeholders are not allowed in the Langfuse block ({', '.join(found)});"
            " it holds names and paths, never values from the environment"
        )
    try:
        config = LangfuseConfig.model_validate(block)
    except ValidationError as exc:
        raise PreflightError(f"{path}: {where}.options.langfuse: {exc}") from exc
    return LangfuseBlock(config=config, base_dir=path.resolve().parent)


def _block_section(path: Path) -> _Section:
    """The tracing section beside the block, for the offline plan (tags and metadata of the
    same document; a launcher passes its own as flags)."""
    section, _ = _tracing_section(_read_yaml(path), path)
    tags = section.get("tags") or []
    metadata = section.get("metadata") or {}
    if not isinstance(tags, list) or not isinstance(metadata, Mapping):
        raise PreflightError(f"{path}: observability.tracing tags must be a list, metadata a map")
    return _Section(tags=list(tags), metadata=dict(metadata), options=section.get("options") or {})


def read_settings(options: Mapping[str, Any]) -> LangfuseConfig:
    """Validate only this plugin's namespace of ``observability.tracing.options``."""
    try:
        return LangfuseConfig.model_validate(options.get("langfuse", {}))
    except ValidationError as exc:
        raise PreflightError(f"observability.tracing.options.langfuse: {exc}") from exc


def _anchored(value: str, base_dir: Path) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else base_dir / candidate


def resolve_profile(
    settings: LangfuseConfig,
    environ: Mapping[str, str],
    base_dir: Path,
    warnings: list[str] | None = None,
) -> TracingProfile:
    """The profile in force: the block's (inline, or a TOML / YAML path anchored to
    ``base_dir``), else ``TOLOKAFORGE_TRACING_PROFILE``, else none."""
    named = (environ.get(PROFILE_ENV) or "").strip()
    try:
        if settings.profile is not None:
            if named and warnings is not None:
                kind = "inline profile" if isinstance(settings.profile, dict) else "profile path"
                warnings.append(
                    f"{PROFILE_ENV}={named} is set next to the block's {kind}; the block's"
                    " profile is used"
                )
            if isinstance(settings.profile, dict):
                return profile_from_mapping(settings.profile, base_dir=base_dir)
            return load_tracing_profile(_anchored(settings.profile, base_dir))
        if named:
            return load_tracing_profile(named)
    except TracingProfileError as exc:
        raise PreflightError(str(exc)) from exc
    return NO_PROFILE


def validate_tag(tag: str, profile: TracingProfile = NO_PROFILE) -> str:
    """A caller tag under a caller prefix of the vocabulary (the launcher's ``project``
    admitted), inside the closed list when the prefix has one."""
    try:
        return validate_caller_tag(tag, profile_values=profile.values, launcher=True)
    except VocabularyError as exc:
        raise PreflightError(f"tracing tag: {exc}") from exc


def merge_tag_sources(
    *sources: tuple[str, Sequence[str]], profile: TracingProfile = NO_PROFILE
) -> tuple[list[str], dict[str, str]]:
    """Tags from several sources (``(origin, tags)`` pairs, in precedence order), validated and
    deduplicated, plus where each prefix's value came from; a prefix carrying two different
    values is an error (a trace never carries two values under one prefix)."""
    merged: list[str] = []
    values: dict[str, str] = {}
    origins: dict[str, str] = {}
    for origin, tags in sources:
        for tag in tags:
            validate_tag(tag, profile)
            prefix, _, value = tag.partition(":")
            if prefix in values and values[prefix] != value:
                raise PreflightError(
                    f"tracing tag prefix {prefix!r} given twice with different values: "
                    f"{values[prefix]!r} ({origins[prefix]}) and {value!r} ({origin})"
                )
            if prefix not in values:
                origins[prefix] = origin
            values[prefix] = value
            if tag not in merged:
                merged.append(tag)
    return merged, origins


def launcher_tags(environ: Mapping[str, str]) -> list[str]:
    """``TOLOKAFORGE_TRACING_TAGS``: comma-separated ``<prefix>:<value>`` tags a launcher adds."""
    raw = environ.get(TRACING_TAGS_ENV, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _expected_project(settings: LangfuseConfig, environ: Mapping[str, str]) -> str | None:
    declared = settings.expected_project()
    for name in (TRACING_EXPECT_PROJECT_ENV, LANGFUSE_PROJECT_ENV):
        given = (environ.get(name) or "").strip()
        if declared and given and given != declared:
            raise PreflightError(
                f"{name}={given!r} contradicts the block's project {declared!r}; the"
                " configuration names the one project the credentials must open"
            )
    return (
        declared
        or (environ.get(TRACING_EXPECT_PROJECT_ENV) or "").strip()
        or (environ.get(LANGFUSE_PROJECT_ENV) or "").strip()
        or None
    )


def resolve_environment(
    settings: LangfuseConfig,
    profile: TracingProfile,
    tags: Sequence[str],
    environ: Mapping[str, str],
    *,
    source: str = ACCEPTS_TRIAL,
    warnings: list[str] | None = None,
) -> str | None:
    """The receiver's native ``environment``. With ``environments`` declared,
    ``LANGFUSE_ENVIRONMENT`` must name one of them whose ``accepts`` admits ``source``;
    otherwise the variable wins, then the block's literal, then the profile's rule (None leaves
    the receiver's default)."""
    selected = (environ.get(ENVIRONMENT_ENV) or "").strip() or None
    declared = settings.environments
    if declared is not None:
        names = sorted(declared)
        if selected is None:
            raise PreflightError(
                f"{ENVIRONMENT_ENV} is required: the block declares the environments {names}"
            )
        entry = declared.get(selected)
        if entry is None:
            raise PreflightError(
                f"{ENVIRONMENT_ENV}={selected!r} is not a declared environment; declared: {names}"
            )
        if not entry.admits(source):
            raise PreflightError(
                f"environment {selected!r} accepts {list(entry.accepts)}, not source:{source}"
            )
        if profile.environment != DEFAULT_ENVIRONMENT_RULE and warnings is not None:
            warnings.append(
                "the profile's environment rule is not used: the block declares environments"
            )
        return selected
    value = selected or settings.environment or profile.environment.resolve(tags)
    if value is None:
        return None
    try:
        return check_environment(value)
    except TracingProfileError as exc:
        raise PreflightError(str(exc)) from exc


def merge_metadata(
    configured: Mapping[str, Any],
    profile: TracingProfile,
    environ: Mapping[str, str],
    reserved: Iterable[str] = (),
) -> dict[str, Any]:
    """The caller's trace metadata: the profile's fixed keys, the config's, then
    ``TOLOKAFORGE_TRACING_METADATA`` (the launcher's per-run values, which win). A key the
    projection writes itself (``reserved``) is an error."""
    try:
        launcher = parse_metadata_variable(environ.get(METADATA_ENV))
    except TracingProfileError as exc:
        raise PreflightError(str(exc)) from exc
    merged: dict[str, Any] = {**profile.fixed_metadata, **configured, **launcher}
    clashes = sorted(set(merged) & set(reserved))
    if clashes:
        raise PreflightError(
            "tracing metadata may not use the keys the projection writes itself: "
            + ", ".join(clashes)
        )
    return merged


def resolve_model_names(
    settings: LangfuseConfig, profile: TracingProfile, base_dir: Path
) -> ModelNameResolver:
    """Explicit normalizer rules (anchored to ``base_dir``) override the profile's rules."""
    normalizer_kind = settings.model_name_normalizer
    rules = (
        str(_anchored(settings.model_name_rules, base_dir)) if settings.model_name_rules else None
    )
    if rules is None and profile.model_name_rules is not None:
        normalizer_kind, rules = "toloka", profile.model_name_rules
    try:
        return build_model_name_resolver(normalizer_kind, rules)
    except ModelNameResolverError as exc:
        raise PreflightError(str(exc)) from exc


def producer_version(producer: str, rules_version: str, profile: TracingProfile) -> str:
    """The native ``version`` field: the producer's identity plus the model-name rules and the
    deployment profile it ran under (differs by producer, by design)."""
    text = producer
    if rules_version and rules_version != NONE:
        text += f"+{rules_version}"
    if profile.version != NONE:
        text += f"+{profile.version}"
    return text


def resolve_plan(
    tracing: TracingInputs,
    environ: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    *,
    source: str = ACCEPTS_TRIAL,
    reserved_metadata: Iterable[str] = (),
) -> TracingPlan:
    """The plan of a run under ``tracing`` (config tags, metadata and ``options``) and the
    launcher's variables in ``environ``: no network, no engine. Raises PreflightError on the
    first problem, naming it."""
    environ = os.environ if environ is None else environ
    base_dir = anchor_directory() if base_dir is None else Path(base_dir)
    warnings: list[str] = []
    settings = read_settings(tracing.options)
    profile = resolve_profile(settings, environ, base_dir, warnings)
    tags, origins = merge_tag_sources(
        ("config", tracing.tags),
        ("launcher", launcher_tags(environ)),
        ("profile", profile.fixed_tags),
        profile=profile,
    )
    expect_project = _expected_project(settings, environ)
    if expect_project and not any(tag.startswith("project:") for tag in tags):
        # the project tag mirrors the project the credentials must open
        tags, origins = merge_tag_sources(
            *[(origins[t.partition(":")[0]], [t]) for t in tags],
            ("receiver", [f"project:{expect_project}"]),
        )
    environment = resolve_environment(
        settings, profile, tags, environ, source=source, warnings=warnings
    )
    metadata = merge_metadata(tracing.metadata, profile, environ, reserved_metadata)
    try:
        check_caller_inputs(profile, tags, metadata=metadata.keys(), launcher=True)
    except TracingProfileError as exc:
        raise PreflightError(str(exc)) from exc
    resolver = resolve_model_names(settings, profile, base_dir)
    return TracingPlan(
        settings=settings,
        base_dir=base_dir,
        profile=profile,
        tags=tuple(tags),
        origins=dict(origins),
        metadata=metadata,
        environment=environment,
        expect_project=expect_project,
        resolver=resolver,
        warnings=tuple(warnings),
    )


# -- the command -----------------------------------------------------------------------------------


def _require_deployment(settings: LangfuseConfig, where: str) -> None:
    if not settings.project:
        raise PreflightError(f"{where}: the Langfuse block declares no project")
    if not settings.environments:
        raise PreflightError(f"{where}: the Langfuse block declares no environments")


def _engine_plan(config: Path, environ: Mapping[str, str], out: list[str]) -> TracingPlan:
    """The live path's view: the engine layers ``project.run_defaults`` under the run config,
    the plugin anchors to the project above the working directory."""
    import importlib

    try:
        factory = importlib.import_module("tolokaforge.observability.factory")
    except ImportError as exc:
        raise PreflightError(
            "the installed engine has no trial-observer seam (tolokaforge.observability.factory"
            " cannot be imported): pin tolokaforge 0.27.0 or later"
        ) from exc
    offered = getattr(factory, "PLUGIN_API_VERSION", None)
    if offered != __api_version__:
        raise PreflightError(
            f"tolokaforge-langfuse {__version__} speaks trial-observer API v{__api_version__},"
            f" the installed engine offers {offered!r}: upgrade the one that is behind"
        )
    from tolokaforge.core.models import TracingConfig
    from tolokaforge.core.project_loader import load_effective_run_config

    try:
        merged, _ = load_effective_run_config(config)
    except Exception as exc:  # noqa: BLE001 - any load failure is the run's own start failure
        raise PreflightError(f"{config}: the engine cannot load it: {exc}") from exc
    tracing_data = (merged.get("observability") or {}).get("tracing")
    options = (tracing_data or {}).get("options") if isinstance(tracing_data, Mapping) else None
    if not isinstance(options, Mapping) or "langfuse" not in options:
        raise PreflightError(
            f"{config}: the effective run config has no"
            " observability.tracing.options.langfuse block (a misspelt parent key in"
            " project.yaml or the run config drops it silently)"
        )
    try:
        tracing = TracingConfig.model_validate(tracing_data)
    except ValidationError as exc:
        raise PreflightError(f"{config}: observability.tracing: {exc}") from exc
    unclaimed = sorted(set(tracing.options) - set(factory.installed_plugins()))
    for name in unclaimed:
        out.append(f"WARN options.{name}: no installed trial-observer plugin claims it")
    base_dir = anchor_directory()
    project_file = find_project_yaml(config)
    if project_file is not None and project_file.parent != base_dir:
        out.append(
            f"WARN the engine layers {project_file}, but the plugin anchors relative paths to"
            f" {base_dir} (run from the project root)"
        )
    if project_file is not None:
        try:
            own = load_langfuse_block(project_file)
        except PreflightError:
            own = None
        if own is not None and own.config != read_settings(tracing.options):
            out.append(
                f"WARN the run config changes the Langfuse block of {project_file}; the offline"
                " connector reads that file alone"
            )
    from tolokaforge_langfuse.projection import schema_keys

    _require_deployment(read_settings(tracing.options), str(config))
    return resolve_plan(tracing, environ, base_dir, reserved_metadata=schema_keys())


def _offline_plan(config: Path, environ: Mapping[str, str], out: list[str]) -> TracingPlan:
    """The connector's view: one YAML file, read as written, anchored to its own directory."""
    block = load_langfuse_block(config)
    _require_deployment(block.config, str(config))
    section = _block_section(config)
    if section.tags or section.metadata:
        out.append(
            "WARN observability.tracing tags / metadata beside the block are not read offline;"
            " the connector takes them as --tag / --metadata"
        )
    try:
        from tolokaforge_langfuse.projection import schema_keys

        reserved: Iterable[str] = schema_keys()
    except ImportError:
        reserved = ()
        out.append("NOTE the engine is not installed: the metadata schema-key check is skipped")
    return resolve_plan(
        _Section(tags=[], metadata={}, options=section.options),
        environ,
        block.base_dir,
        reserved_metadata=reserved,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m tolokaforge_langfuse.preflight --config <file> [--environment <name>]
    [--tags a:b,...] [--metadata k=v,...] [--offline]``: print the plan, exit 2 on an error."""
    parser = argparse.ArgumentParser(
        prog="python -m tolokaforge_langfuse.preflight",
        description="Check a run's Langfuse configuration before the run starts.",
    )
    parser.add_argument("--config", required=True, type=Path, help="run, shard or project file")
    parser.add_argument("--environment", help=f"the selector (default: {ENVIRONMENT_ENV})")
    parser.add_argument("--tags", help=f"launcher tags a:b,c:d (default: {TRACING_TAGS_ENV})")
    parser.add_argument("--metadata", help=f"launcher metadata k=v,... (default: {METADATA_ENV})")
    parser.add_argument(
        "--offline", action="store_true", help="read the one file without the engine"
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    environ = dict(os.environ)
    if args.environment is not None:
        environ[ENVIRONMENT_ENV] = args.environment
    if args.tags is not None:
        environ[TRACING_TAGS_ENV] = args.tags
    if args.metadata is not None:
        environ[METADATA_ENV] = args.metadata
    notes: list[str] = []
    try:
        if not args.config.is_file():
            raise PreflightError(f"{args.config}: no such file")
        plan = (_offline_plan if args.offline else _engine_plan)(args.config, environ, notes)
    except PreflightError as exc:
        for note in notes:
            print(note, file=sys.stderr)
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 2
    for note in [*notes, *(f"WARN {w}" for w in plan.warnings)]:
        print(note, file=sys.stderr)
    print(f"OK {args.config} ({'offline' if args.offline else 'engine layering'})")
    for line in plan.lines():
        print(f"  {line}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the module's command-line entry
    sys.exit(main())
