"""The deployment profile of the trace export (ADR-0047, parity and vocabulary amendments).

Everything a deployment decides about its traces, and neither producer may know as a value,
arrives at run time in one TOML file: the live observer reads it through
``observability.tracing.profile`` or ``TOLOKAFORGE_TRACING_PROFILE``, the offline bundle uploader
through its ``--tag-profile`` flag. Over the default vocabulary of
:mod:`tolokaforge_langfuse.vocabulary` (the prefixes, the producer's derived tags, the engine's
closed lists, the default environment rule) a profile says:

- ``[environment]``: the receiver's native ``environment``, a literal or a rule over one tag
  prefix (default: the vocabulary's rule, ``production`` for ``run_kind:eval``);
- ``[tags] fixed``: the tags every trace of the deployment carries (a caller may not contradict
  them); ``[tags] derived``: which groups of bundle-derived tags the producers emit (default: all);
- ``[tags.values]``: closed value lists for caller prefixes (narrowing the engine's lists, or
  giving a list to a free-form prefix); ``[tags.required]``: prefixes a source's traces must
  carry beyond the vocabulary's own required set;
- ``[derive.<prefix>]``: how the offline command turns a launcher input into a tag (fnmatch
  patterns over a git ref, an orchestrator input; first match wins);
- ``[metadata] keys``: the per-run metadata keys a caller may set; ``[metadata.fixed]``: metadata
  every trace carries;
- the profile ``version`` that joins the native ``version`` field;
- ``[models] rules``: the model-name rules file the ``toloka`` normalizer runs under (relative to
  the profile file).

A profile adds no prefix of its own. The producers validate the shape and apply the profile
mechanically: a profile that does not load, an environment outside the receiver's alphabet, a
value outside a closed list, a fixed tag under a producer-owned prefix or a metadata key the
projection itself writes is a configuration error at run start.

Example (neutral values; a deployment's file lives in its own repository)::

    schema = 2
    version = "acme-2026.09.17.1"

    [environment]
    from_tag = "run_kind"
    default = "development"
    [environment.values]
    eval = "production"

    [tags]
    fixed = ["team:pilot"]
    [tags.values]
    dataset = ["v1", "v3"]
    [tags.required]
    trial = ["domain", "config"]

    [derive.dataset]
    "eval/pilot-v3*" = "v3"

    [metadata]
    keys = ["campaign"]
    [metadata.fixed]
    deployment = "pilot"

    [models]
    rules = "model_name_rules.toml"

``python -m tolokaforge_langfuse.profile <file> [--tags a:b,c:d] [--metadata k=v,...]``
validates a file, and optionally a launcher's tags and metadata against it (exit 2 on the first
error).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tolokaforge_langfuse.vocabulary import (
    ALL_DERIVED_GROUPS,
    CORE_PREFIXES,
    CORE_VALUES,
    DEFAULT_ENVIRONMENT_RULE,
    DERIVED_GROUPS,
    PRODUCER_PREFIXES,
    REQUIRED_ALWAYS,
    REQUIRED_FOR_TRIAL,
    SOURCE_TRIAL,
    EnvironmentRule,
    VocabularyError,
    check_caller_prefix,
    check_value,
    split_tag,
    validate_caller_tag,
)

__all__ = [
    "ENVIRONMENT_ENV",
    "METADATA_ENV",
    "NO_PROFILE",
    "PROFILE_ENV",
    "SCHEMA_VERSION",
    "SCHEMA_VERSIONS",
    "EnvironmentRule",
    "TracingProfile",
    "TracingProfileError",
    "check_caller_inputs",
    "check_environment",
    "check_tag",
    "describe",
    "load_tracing_profile",
    "parse_metadata_variable",
    "profile_from_mapping",
]

SCHEMA_VERSION = 2
SCHEMA_VERSIONS = frozenset(
    {1, 2}
)  # schema 1 files (environment, fixed tags and metadata, models) still load
PROFILE_ENV = "TOLOKAFORGE_TRACING_PROFILE"
ENVIRONMENT_ENV = "LANGFUSE_ENVIRONMENT"
METADATA_ENV = "TOLOKAFORGE_TRACING_METADATA"
# the receiver's alphabet for the native environment field (Langfuse: lowercase letters, digits,
# ``-`` and ``_``, at most 40 characters, never starting with ``langfuse``)
_ENVIRONMENT_SHAPE = re.compile(r"^(?!langfuse)[a-z0-9_-]{1,40}$")
_PREFIX_SHAPE = re.compile(r"^[a-z][a-z0-9_]*$")
_TOP_KEYS = frozenset({"schema", "version", "environment", "tags", "derive", "metadata", "models"})
_ENVIRONMENT_KEYS = frozenset({"literal", "from_tag", "default", "values"})
_TAGS_KEYS = frozenset({"fixed", "derived", "values", "required"})
_METADATA_KEYS = frozenset({"fixed", "keys"})
_MODELS_KEYS = frozenset({"rules"})
_SCALARS = (str, int, float, bool)


class TracingProfileError(ValueError):
    """The profile cannot be honoured as written; the message names the key, never a value that
    could be a credential."""


@dataclass(frozen=True)
class TracingProfile:
    version: str
    schema: int = SCHEMA_VERSION
    environment: EnvironmentRule = DEFAULT_ENVIRONMENT_RULE
    fixed_tags: tuple[str, ...] = ()
    derived_groups: frozenset[str] = ALL_DERIVED_GROUPS
    values: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    required: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    derive: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    metadata_keys: tuple[str, ...] = ()
    fixed_metadata: Mapping[str, Any] = field(default_factory=dict)
    model_name_rules: str | None = None
    path: str | None = None

    @property
    def present(self) -> bool:
        """Whether a deployment's file is in force (``NO_PROFILE`` is the vocabulary alone)."""
        return self.path is not None

    def allowed_values(self, prefix: str) -> tuple[str, ...] | None:
        """The closed list for ``prefix``: the profile's when it has one, else the core's."""
        return self.values.get(prefix) or CORE_VALUES.get(prefix)

    def required_for(self, source: str) -> tuple[str, ...]:
        """The vocabulary's required set for ``source`` plus the profile's additions."""
        base = REQUIRED_ALWAYS + (REQUIRED_FOR_TRIAL if source == SOURCE_TRIAL else ())
        extra = tuple(self.required.get(source, ()))
        return base + tuple(p for p in extra if p not in base)

    def derive_value(self, prefix: str, given: str) -> str:
        """Run ``[derive.<prefix>]`` over ``given`` (fnmatch patterns, declaration order, first
        match wins)."""
        import fnmatch

        table = self.derive.get(prefix)
        if not table:
            raise TracingProfileError(f"the profile has no [derive.{prefix}] table")
        for pattern, value in table.items():
            if fnmatch.fnmatchcase(given, pattern):
                return value
        raise TracingProfileError(f"[derive.{prefix}]: no pattern matches {given!r}")


NO_PROFILE = TracingProfile(version="none")


def check_environment(value: object, *, where: str = "environment") -> str:
    """One environment value inside the receiver's alphabet."""
    if not isinstance(value, str) or not _ENVIRONMENT_SHAPE.match(value):
        raise TracingProfileError(
            f"{where}: {value!r} is not a valid environment (lowercase letters, digits, '-' and"
            " '_', at most 40 characters, not starting with 'langfuse')"
        )
    return value


def check_tag(tag: object, *, where: str, values: Mapping[str, Sequence[str]] | None = None) -> str:
    """A fixed tag: ``<prefix>:<value>`` under a caller prefix, inside the closed list if any."""
    try:
        prefix, _ = split_tag(tag)
        if prefix in PRODUCER_PREFIXES:
            raise TracingProfileError(
                f"{where}: tag {tag!r}: its prefix is set by the exporter itself"
            )
        return validate_caller_tag(tag, profile_values=values)
    except VocabularyError as exc:
        raise TracingProfileError(f"{where}: {exc}") from exc


def check_caller_inputs(
    profile: TracingProfile,
    tags: Iterable[str],
    *,
    metadata: Iterable[str] = (),
    source: str = SOURCE_TRIAL,
    launcher: bool = True,
) -> None:
    """A launcher's or config's tags and metadata keys against the vocabulary and the profile:
    every tag well-formed under a caller prefix (the receiver's ``project`` admitted when
    ``launcher``), values inside the closed lists, one value per prefix, no contradiction of a
    fixed tag, the required prefixes present when a profile is in force, and the metadata keys
    inside the profile's list when it has one. Raises TracingProfileError naming the tag."""
    values: dict[str, str] = {}
    try:
        for tag in tags:
            validate_caller_tag(tag, profile_values=profile.values, launcher=launcher)
            prefix, value = split_tag(tag)
            if values.get(prefix, value) != value:
                raise TracingProfileError(
                    f"tag prefix {prefix!r} carries two values: {values[prefix]!r} and {value!r}"
                )
            values[prefix] = value
    except VocabularyError as exc:
        raise TracingProfileError(str(exc)) from exc
    for fixed in profile.fixed_tags:
        prefix, value = fixed.partition(":")[0], fixed.partition(":")[2]
        if values.get(prefix, value) != value:
            raise TracingProfileError(
                f"tag {prefix}:{values[prefix]} contradicts the profile's fixed tag {fixed!r}"
            )
        values.setdefault(prefix, value)
    if profile.present:
        missing = [
            p
            for p in profile.required_for(source)
            if p not in PRODUCER_PREFIXES and p not in values
        ]
        if missing:
            raise TracingProfileError(
                f"missing required tag(s) for source {source!r}: {', '.join(missing)}"
                f" (profile {profile.version})"
            )
    if profile.metadata_keys:
        unknown = sorted(set(metadata) - set(profile.metadata_keys) - set(profile.fixed_metadata))
        if unknown:
            raise TracingProfileError(
                f"metadata key(s) {unknown} are not in the profile's [metadata] keys "
                f"{list(profile.metadata_keys)} (profile {profile.version})"
            )


def _prefix(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not _PREFIX_SHAPE.match(value):
        raise TracingProfileError(
            f"{where}: {value!r} is not a tag prefix (lowercase letters, digits and '_')"
        )
    return value


def _caller_prefix(value: object, *, where: str) -> str:
    prefix = _prefix(value, where=where)
    try:
        return check_caller_prefix(prefix, where=where)
    except VocabularyError as exc:
        raise TracingProfileError(str(exc)) from exc


def _table(value: Any, *, where: str, allowed: frozenset[str]) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TracingProfileError(f"{where} must be a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise TracingProfileError(f"{where}: unknown keys {unknown}; allowed: {sorted(allowed)}")
    return value


def _string_list(value: Any, *, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TracingProfileError(f"{where} must be a list of strings")
    return list(value)


def _load_toml(text: str, *, where: str) -> Mapping[str, Any]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10: the package depends on tomli there
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError as exc:
            raise TracingProfileError(
                f"{where}: reading TOML needs Python 3.11+ or the 'tomli' package"
            ) from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TracingProfileError(f"{where}: not valid TOML: {exc}") from exc


def load_tracing_profile(path: str | Path) -> TracingProfile:
    """Parse and validate the profile at ``path``; every error names the offending key."""
    where = f"tracing profile {path}"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TracingProfileError(f"{where}: cannot be read: {exc}") from exc
    return profile_from_mapping(_load_toml(text, where=where), path=str(path))


def _environment_rule(data: Mapping[str, Any], *, where: str) -> EnvironmentRule:
    env_table = _table(
        data.get("environment"), where=f"{where}: [environment]", allowed=_ENVIRONMENT_KEYS
    )
    if not env_table:
        return DEFAULT_ENVIRONMENT_RULE
    literal, from_tag = env_table.get("literal"), env_table.get("from_tag")
    if (literal is None) == (from_tag is None):
        raise TracingProfileError(
            f"{where}: [environment] needs exactly one of 'literal' or 'from_tag'"
        )
    if literal is not None:
        if any(k in env_table for k in ("default", "values")):
            raise TracingProfileError(
                f"{where}: [environment] 'literal' takes no 'default' or 'values'"
            )
        return EnvironmentRule(
            literal=check_environment(literal, where=f"{where}: [environment] literal")
        )
    prefix = _prefix(from_tag, where=f"{where}: [environment] from_tag")
    if "default" not in env_table:
        raise TracingProfileError(f"{where}: [environment] 'from_tag' needs a 'default'")
    default = check_environment(env_table["default"], where=f"{where}: [environment] default")
    raw_values = env_table.get("values")
    if not isinstance(raw_values, Mapping) or not raw_values:
        raise TracingProfileError(
            f"{where}: [environment.values] must be a non-empty table of tag value -> environment"
        )
    values = {
        str(key): check_environment(value, where=f"{where}: [environment.values] {key}")
        for key, value in raw_values.items()
    }
    return EnvironmentRule(from_tag=prefix, default=default, values=values)


def _values(tags_table: Mapping[str, Any], *, where: str) -> dict[str, tuple[str, ...]]:
    values: dict[str, tuple[str, ...]] = {}
    raw = tags_table.get("values")
    if raw is None:
        return values
    if not isinstance(raw, Mapping):
        raise TracingProfileError(f"{where}: [tags.values] must be a table")
    for prefix, listed in raw.items():
        here = f"{where}: [tags.values] {prefix}"
        _caller_prefix(prefix, where=here)
        if (
            not isinstance(listed, list)
            or not listed
            or not all(isinstance(v, str) for v in listed)
        ):
            raise TracingProfileError(f"{here} must be a non-empty list of strings")
        core = CORE_VALUES.get(prefix)
        for value in listed:
            try:
                check_value(prefix, value)
            except VocabularyError as exc:
                raise TracingProfileError(f"{here}: {exc}") from exc
            if core is not None and value not in core:
                raise TracingProfileError(
                    f"{here} lists {value!r}, outside the core list {list(core)} (a profile "
                    "narrows the core, it never widens it)"
                )
        values[prefix] = tuple(listed)
    return values


def _required(tags_table: Mapping[str, Any], *, where: str) -> dict[str, tuple[str, ...]]:
    required: dict[str, tuple[str, ...]] = {}
    raw = tags_table.get("required")
    if raw is None:
        return required
    if not isinstance(raw, Mapping):
        raise TracingProfileError(f"{where}: [tags.required] must be a table of source -> prefixes")
    for source, prefixes in raw.items():
        here = f"{where}: [tags.required] {source}"
        if not isinstance(source, str) or not source or ":" in source:
            raise TracingProfileError(f"{where}: [tags.required] keys are source values (trial)")
        listed = _string_list(prefixes, where=here)
        for prefix in listed:
            _caller_prefix(prefix, where=here)
        required[source] = tuple(listed)
    return required


def _derive(
    data: Mapping[str, Any], *, where: str, values: Mapping[str, tuple[str, ...]]
) -> dict[str, dict[str, str]]:
    derive: dict[str, dict[str, str]] = {}
    raw = data.get("derive")
    if raw is None:
        return derive
    if not isinstance(raw, Mapping):
        raise TracingProfileError(f"{where}: [derive] must be a table of prefix tables")
    for prefix, table in raw.items():
        here = f"{where}: [derive.{prefix}]"
        _caller_prefix(prefix, where=here)
        if not isinstance(table, Mapping) or not table:
            raise TracingProfileError(f"{here} must be a non-empty table of pattern -> value")
        allowed = values.get(prefix) or CORE_VALUES.get(prefix)
        rules: dict[str, str] = {}
        for pattern, value in table.items():
            if not isinstance(value, str):
                raise TracingProfileError(f"{here} {pattern!r} must map to a string")
            try:
                check_value(prefix, value)
            except VocabularyError as exc:
                raise TracingProfileError(f"{here}: {exc}") from exc
            if allowed is not None and value not in allowed:
                raise TracingProfileError(
                    f"{here} {pattern!r} -> {value!r} is outside {list(allowed)}"
                )
            rules[str(pattern)] = value
        derive[prefix] = rules
    return derive


def profile_from_mapping(data: Mapping[str, Any], *, path: str | None = None) -> TracingProfile:
    where = f"tracing profile {path}" if path else "tracing profile"
    if not isinstance(data, Mapping):
        raise TracingProfileError(f"{where}: the document must be a table")
    schema = data.get("schema")
    if schema not in SCHEMA_VERSIONS:
        raise TracingProfileError(
            f"{where}: 'schema' must be one of {sorted(SCHEMA_VERSIONS)}, got {schema!r}"
        )
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise TracingProfileError(f"{where}: unknown top-level keys {unknown}")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip() or "+" in version:
        raise TracingProfileError(f"{where}: 'version' must be a non-empty string without '+'")
    rule = _environment_rule(data, where=where)

    tags_table = _table(data.get("tags"), where=f"{where}: [tags]", allowed=_TAGS_KEYS)
    values = _values(tags_table, where=where)
    fixed_tags = [
        check_tag(tag, where=f"{where}: [tags] fixed", values=values)
        for tag in _string_list(tags_table.get("fixed"), where=f"{where}: [tags] fixed")
    ]
    seen: dict[str, str] = {}
    for tag in fixed_tags:
        prefix, _, value = tag.partition(":")
        if seen.get(prefix, value) != value:
            raise TracingProfileError(
                f"{where}: [tags] fixed carries two values under the prefix {prefix!r}"
            )
        seen[prefix] = value
    derived_groups: frozenset[str] = ALL_DERIVED_GROUPS
    if tags_table.get("derived") is not None:
        listed = _string_list(tags_table.get("derived"), where=f"{where}: [tags] derived")
        unknown_groups = sorted(set(listed) - ALL_DERIVED_GROUPS)
        if unknown_groups:
            raise TracingProfileError(
                f"{where}: [tags] derived names unknown groups {unknown_groups}; the groups are "
                f"{list(DERIVED_GROUPS)}"
            )
        derived_groups = frozenset(listed)
    required = _required(tags_table, where=where)
    derive = _derive(data, where=where, values=values)

    metadata_table = _table(
        data.get("metadata"), where=f"{where}: [metadata]", allowed=_METADATA_KEYS
    )
    fixed_metadata: dict[str, Any] = {}
    raw_fixed = metadata_table.get("fixed")
    if raw_fixed is not None:
        if not isinstance(raw_fixed, Mapping):
            raise TracingProfileError(f"{where}: [metadata.fixed] must be a table")
        for key, value in raw_fixed.items():
            if not isinstance(key, str) or not key.strip():
                raise TracingProfileError(
                    f"{where}: [metadata.fixed] keys must be non-empty strings"
                )
            if not isinstance(value, _SCALARS):
                raise TracingProfileError(
                    f"{where}: [metadata.fixed] {key} must be a string, number or boolean"
                )
            fixed_metadata[key] = value
    metadata_keys: tuple[str, ...] = ()
    if metadata_table.get("keys") is not None:
        keys = metadata_table["keys"]
        if not isinstance(keys, list) or not all(isinstance(k, str) and k for k in keys):
            raise TracingProfileError(f"{where}: [metadata] keys must be a list of strings")
        clashes = sorted(set(keys) & set(CORE_PREFIXES))
        if clashes:
            raise TracingProfileError(
                f"{where}: [metadata] keys {clashes} are tag prefixes, not metadata"
            )
        metadata_keys = tuple(keys)

    models_table = _table(data.get("models"), where=f"{where}: [models]", allowed=_MODELS_KEYS)
    rules: str | None = None
    if models_table.get("rules") is not None:
        raw_rules = models_table["rules"]
        if not isinstance(raw_rules, str) or not raw_rules.strip():
            raise TracingProfileError(f"{where}: [models] rules must be a non-empty path")
        rules_path = Path(raw_rules)
        if not rules_path.is_absolute() and path is not None:
            rules_path = Path(path).parent / rules_path
        if not rules_path.is_file():
            raise TracingProfileError(f"{where}: [models] rules: no such file {rules_path}")
        rules = str(rules_path)
    return TracingProfile(
        version=version,
        schema=int(schema),
        environment=rule,
        fixed_tags=tuple(fixed_tags),
        derived_groups=derived_groups,
        values=values,
        required=required,
        derive=derive,
        metadata_keys=metadata_keys,
        fixed_metadata=fixed_metadata,
        model_name_rules=rules,
        path=path,
    )


def parse_metadata_variable(raw: str | None) -> dict[str, str]:
    """``TOLOKAFORGE_TRACING_METADATA``: ``key=value,key2=value2`` (the per-run metadata the
    offline command receives as ``--metadata``); a value may not contain a comma."""
    metadata: dict[str, str] = {}
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise TracingProfileError(
                f"{METADATA_ENV} expects key=value pairs separated by commas, got {item.strip()!r}"
            )
        metadata[key] = value
    return metadata


def describe(profile: TracingProfile) -> str:
    rule = profile.environment
    environment = (
        f"literal {rule.literal}"
        if rule.literal is not None
        else (
            f"from tag {rule.from_tag} ({dict(rule.values)}, default {rule.default})"
            if rule.from_tag
            else "unset (the receiver's default)"
        )
    )
    return (
        f"schema {profile.schema}, version {profile.version}, environment {environment}, fixed "
        f"tags {list(profile.fixed_tags)}, derived {sorted(profile.derived_groups)}, values for "
        f"{sorted(profile.values)}, required {dict(profile.required)}, derivations for "
        f"{sorted(profile.derive)}, metadata keys {list(profile.metadata_keys)}, fixed metadata "
        f"{dict(profile.fixed_metadata)}, model rules {profile.model_name_rules or 'none'}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m tolokaforge_langfuse.profile <file> [--tags a:b,...] [--metadata k=v,...]``:
    validate a profile, and a launcher's inputs against it (the CI pre-check of a live run)."""
    args = list(sys.argv[1:] if argv is None else argv)
    tags: list[str] = []
    metadata: list[str] = []
    positional: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--tags", "--metadata") and index + 1 < len(args):
            target = tags if arg == "--tags" else metadata
            target.extend(part.strip() for part in args[index + 1].split(",") if part.strip())
            index += 2
            continue
        positional.append(arg)
        index += 1
    if len(positional) != 1:
        print(
            "usage: python -m tolokaforge_langfuse.profile <profile.toml> [--tags a:b,c:d]"
            " [--metadata k=v,k2=v2]",
            file=sys.stderr,
        )
        return 2
    try:
        profile = load_tracing_profile(positional[0])
        if tags or metadata:
            keys = [item.partition("=")[0].strip() for item in metadata]
            check_caller_inputs(profile, tags, metadata=keys)
    except TracingProfileError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print(f"OK {positional[0]}: {describe(profile)}")
    if tags or metadata:
        print(f"OK inputs: tags {tags}, metadata keys {[m.partition('=')[0] for m in metadata]}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the module's command-line entry
    sys.exit(main())
