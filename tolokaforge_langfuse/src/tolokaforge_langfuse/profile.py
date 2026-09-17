"""The deployment profile of the live trace export (ADR-0047, parity amendment).

Everything a deployment decides about its traces and the engine must not know as a value arrives
at run time in one TOML file (``observability.tracing.profile`` or ``TOLOKAFORGE_TRACING_PROFILE``):

- the receiver's native ``environment``: a literal, or a rule over one tag prefix (the value of
  that tag picks the environment from a map, with a default);
- the tags every trace of the deployment carries (``[tags] fixed``);
- the metadata every trace carries (``[metadata.fixed]``);
- the profile ``version`` that joins the native ``version`` field;
- optionally the model-name rules file the ``toloka`` normalizer runs under (``[models] rules``,
  relative to the profile file).

The engine validates the shape and applies the profile mechanically. A profile that does not
load, an environment outside the receiver's alphabet, a fixed tag under a producer-owned prefix,
or a fixed metadata key the projection itself writes is a configuration error at run start.

Example (neutral values; a deployment's file lives in its own repository)::

    schema = 1
    version = "acme-2026.09.17.1"

    [environment]
    from_tag = "run_kind"
    default = "development"
    [environment.values]
    eval = "production"

    [tags]
    fixed = ["team:pilot"]

    [metadata.fixed]
    deployment = "pilot"

    [models]
    rules = "model_name_rules.toml"

``python -m tolokaforge_langfuse.profile <file>`` validates a file and prints what it
carries (exit 2 on the first error).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tolokaforge_langfuse.model_names import RESERVED_TAG_PREFIXES

SCHEMA_VERSION = 1
PROFILE_ENV = "TOLOKAFORGE_TRACING_PROFILE"
ENVIRONMENT_ENV = "LANGFUSE_ENVIRONMENT"
METADATA_ENV = "TOLOKAFORGE_TRACING_METADATA"
# the receiver's alphabet for the native environment field (Langfuse: lowercase letters, digits,
# ``-`` and ``_``, at most 40 characters, never starting with ``langfuse``)
_ENVIRONMENT_SHAPE = re.compile(r"^(?!langfuse)[a-z0-9_-]{1,40}$")
_PREFIX_SHAPE = re.compile(r"^[a-z][a-z0-9_]*$")
_TAG_SHAPE = re.compile(r"^[a-z][a-z0-9_]*:\S+$")
_TOP_KEYS = frozenset({"schema", "version", "environment", "tags", "metadata", "models"})
_ENVIRONMENT_KEYS = frozenset({"literal", "from_tag", "default", "values"})
_TAGS_KEYS = frozenset({"fixed"})
_METADATA_KEYS = frozenset({"fixed"})
_MODELS_KEYS = frozenset({"rules"})
_SCALARS = (str, int, float, bool)


class TracingProfileError(ValueError):
    """The profile cannot be honoured as written; the message names the key, never a value that
    could be a credential."""


@dataclass(frozen=True)
class EnvironmentRule:
    """``literal`` fixes the environment; ``from_tag`` maps the value of one tag prefix through
    ``values`` and falls back to ``default`` (also when the tag is absent)."""

    literal: str | None = None
    from_tag: str | None = None
    default: str | None = None
    values: Mapping[str, str] = field(default_factory=dict)

    def resolve(self, tags: Sequence[str]) -> str | None:
        if self.literal is not None:
            return self.literal
        if self.from_tag is None:
            return None
        for tag in tags:
            prefix, _, value = tag.partition(":")
            if prefix == self.from_tag:
                return self.values.get(value, self.default)
        return self.default


@dataclass(frozen=True)
class TracingProfile:
    version: str
    environment: EnvironmentRule = field(default_factory=EnvironmentRule)
    fixed_tags: tuple[str, ...] = ()
    fixed_metadata: Mapping[str, Any] = field(default_factory=dict)
    model_name_rules: str | None = None
    path: str | None = None


NO_PROFILE = TracingProfile(version="none")


def check_environment(value: object, *, where: str = "environment") -> str:
    """One environment value inside the receiver's alphabet."""
    if not isinstance(value, str) or not _ENVIRONMENT_SHAPE.match(value):
        raise TracingProfileError(
            f"{where}: {value!r} is not a valid environment (lowercase letters, digits, '-' and"
            " '_', at most 40 characters, not starting with 'langfuse')"
        )
    return value


def check_tag(tag: object, *, where: str) -> str:
    """A fixed tag: ``<prefix>:<value>``, not under a prefix the exporter sets itself."""
    if not isinstance(tag, str) or not _TAG_SHAPE.match(tag):
        raise TracingProfileError(f"{where}: tag {tag!r} must look like <prefix>:<value>")
    if tag.partition(":")[0] in RESERVED_TAG_PREFIXES:
        raise TracingProfileError(f"{where}: tag {tag!r}: its prefix is set by the exporter itself")
    return tag


def _prefix(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not _PREFIX_SHAPE.match(value):
        raise TracingProfileError(
            f"{where}: {value!r} is not a tag prefix (lowercase letters, digits and '_')"
        )
    return value


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
    except ImportError:  # pragma: no cover - Python 3.10: the otel extra installs tomli
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


def profile_from_mapping(data: Mapping[str, Any], *, path: str | None = None) -> TracingProfile:
    where = f"tracing profile {path}" if path else "tracing profile"
    if not isinstance(data, Mapping):
        raise TracingProfileError(f"{where}: the document must be a table")
    if data.get("schema") != SCHEMA_VERSION:
        raise TracingProfileError(
            f"{where}: 'schema' must be {SCHEMA_VERSION}, got {data.get('schema')!r}"
        )
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise TracingProfileError(f"{where}: unknown top-level keys {unknown}")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip() or "+" in version:
        raise TracingProfileError(f"{where}: 'version' must be a non-empty string without '+'")
    env_table = _table(
        data.get("environment"), where=f"{where}: [environment]", allowed=_ENVIRONMENT_KEYS
    )
    rule = EnvironmentRule()
    if env_table:
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
            rule = EnvironmentRule(
                literal=check_environment(literal, where=f"{where}: [environment] literal")
            )
        else:
            prefix = _prefix(from_tag, where=f"{where}: [environment] from_tag")
            if "default" not in env_table:
                raise TracingProfileError(f"{where}: [environment] 'from_tag' needs a 'default'")
            default = check_environment(
                env_table["default"], where=f"{where}: [environment] default"
            )
            raw_values = env_table.get("values")
            if not isinstance(raw_values, Mapping) or not raw_values:
                raise TracingProfileError(
                    f"{where}: [environment.values] must be a non-empty table of tag value -> environment"
                )
            values = {
                str(key): check_environment(value, where=f"{where}: [environment.values] {key}")
                for key, value in raw_values.items()
            }
            rule = EnvironmentRule(from_tag=prefix, default=default, values=values)

    tags_table = _table(data.get("tags"), where=f"{where}: [tags]", allowed=_TAGS_KEYS)
    fixed_tags = [
        check_tag(tag, where=f"{where}: [tags] fixed")
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
        environment=rule,
        fixed_tags=tuple(fixed_tags),
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
        f"version {profile.version}, environment {environment}, fixed tags "
        f"{list(profile.fixed_tags)}, fixed metadata {dict(profile.fixed_metadata)}, "
        f"model rules {profile.model_name_rules or 'none'}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m tolokaforge_langfuse.profile <file>``: validate and describe a profile."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m tolokaforge_langfuse.profile <profile.toml>", file=sys.stderr)
        return 2
    try:
        profile = load_tracing_profile(args[0])
    except TracingProfileError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print(f"OK {args[0]}: {describe(profile)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the module's command-line entry
    sys.exit(main())
