"""Model identity for traces as configuration (ADR-0046).

The engine knows a model as the ``(provider, name)`` pair of its ``ModelConfig``. What a trace
should call it is a deployment's choice: by default the raw pair (``vendor/model`` when the name
carries a slash, else ``provider/name``), or, when configured, the identity and descriptive
fields of ``toloka-model-name-normalizer`` under a rules file the deployment owns (the arena keeps
its stems, abbreviations and vendor spellings there). The library is imported lazily and only when
selected, so core carries no dependency on it; selecting it without the package installed is a
configuration error, never a silent fallback.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

NONE = "none"
# prefixes the exporter sets from what it knows (the harness, the model, the trial's task id);
# every other tag is the run config's and only its syntax is checked (PLAN 3.11, R12)
RESERVED_TAG_PREFIXES = frozenset({"harness", "model", "model_vendor", "model_family", "task"})


@dataclass(frozen=True)
class ModelIdentity:
    """What a trace says about a model: the identity string, its facet tags and metadata."""

    canonical: str
    tags: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


class ModelNameResolver(Protocol):
    def resolve(self, provider: str | None, name: str) -> ModelIdentity: ...

    def absent(self) -> dict[str, Any]:
        """The ``model_*`` metadata of a trial that names no model: the same keys as a resolved
        identity, ``none`` values, the rules in force still recorded."""
        ...

    @property
    def description(self) -> str: ...

    @property
    def rules_version(self) -> str: ...


# the descriptive facets every resolver reports as ``model_<facet>`` metadata, in the order
# ``toloka-model-name-normalizer`` names them; the key set is the same whichever resolver runs,
# so a trace's metadata schema never depends on the deployment's choice (the values do)
FACETS = (
    "vendor",
    "family",
    "generation",
    "tier",
    "variant",
    "snapshot",
    "size",
    "stage",
    "capability",
    "api_version",
)


def identity_metadata(
    fields: Mapping[str, Any] | None,
    *,
    route: str | None,
    routes: Sequence[str] | None,
    rules_version: str,
    identity_fingerprint: str,
    fields_fingerprint: str,
    resolver_fingerprint: str | None,
    lookup: bool,
) -> dict[str, Any]:
    """The model's descriptive facets, route and rule provenance, every key with an explicit
    value (``none`` when absent), the same shape the offline bundle uploader writes."""
    metadata: dict[str, Any] = {}
    for facet in FACETS:
        value = (fields or {}).get(facet)
        metadata[f"model_{facet}"] = value if value not in (None, "") else NONE
    metadata["model_route"] = route or NONE
    metadata["model_routes"] = "/".join(routes) if routes else NONE
    metadata["model_rules_version"] = rules_version
    metadata["model_identity_fingerprint"] = identity_fingerprint
    metadata["model_fields_fingerprint"] = fields_fingerprint
    metadata["model_resolver_fingerprint"] = resolver_fingerprint or NONE
    metadata["model_lookup"] = lookup
    return metadata


class RawModelNameResolver:
    """No rules: ``vendor/model`` as spelled, ``provider/name`` for a bare name."""

    description = "raw"
    rules_version = NONE

    def resolve(self, provider: str | None, name: str) -> ModelIdentity:
        canonical = name if "/" in name else (f"{provider}/{name}" if provider else name)
        vendor = canonical.split("/", 1)[0] if "/" in canonical else NONE
        return ModelIdentity(
            canonical=canonical,
            tags=(f"model:{canonical}",),
            metadata=identity_metadata(
                {"vendor": vendor},
                route=provider,
                routes=(provider,) if provider else None,
                rules_version=NONE,
                identity_fingerprint=NONE,
                fields_fingerprint=NONE,
                resolver_fingerprint=None,
                lookup=False,
            ),
        )

    def absent(self) -> dict[str, Any]:
        """The metadata of a trial whose bundle names no model (every key explicit)."""
        return identity_metadata(
            None,
            route=None,
            routes=None,
            rules_version=NONE,
            identity_fingerprint=NONE,
            fields_fingerprint=NONE,
            resolver_fingerprint=None,
            lookup=False,
        )


class NormalizerModelNameResolver:
    """``toloka-model-name-normalizer`` with an optional override rules file."""

    def __init__(self, rules_path: str | None = None) -> None:
        try:
            from toloka_model_name_normalizer import (  # type: ignore[import-not-found]
                Normalizer,
                Rules,
            )
        except ImportError as exc:  # pragma: no cover - exercised through the factory error path
            raise ModelNameResolverError(
                "observability.tracing.model_name_normalizer='toloka' needs the "
                "toloka-model-name-normalizer package installed"
            ) from exc
        try:
            rules = Rules.load(rules_path) if rules_path else Rules.default()
        except (ValueError, OSError) as exc:
            raise ModelNameResolverError(
                f"model-name rules {rules_path!r} cannot be loaded: {exc}"
            ) from exc
        self._normalizer = Normalizer(rules)
        self._rules_path = rules_path

    @property
    def description(self) -> str:
        rules = self._normalizer.rules
        return (
            f"toloka-model-name-normalizer rules {rules.version} ({self._rules_path or 'packaged'})"
        )

    @property
    def rules_version(self) -> str:
        return str(self._normalizer.rules.version)

    def resolve(self, provider: str | None, name: str) -> ModelIdentity:
        try:
            parsed = (
                self._normalizer.parse_pair(provider, name)
                if provider
                else self._normalizer.parse(name)
            )
        except ValueError as exc:  # every ModelNameError is a ValueError
            raise ModelNameResolverError(
                f"model reference ({provider!r}, {name!r}) cannot be read: {exc}"
            ) from exc
        rules = self._normalizer.rules
        provenance = parsed.provenance
        metadata = identity_metadata(
            parsed.fields(),
            route=parsed.route,
            routes=parsed.routes,
            rules_version=rules.version,
            identity_fingerprint=rules.identity_fingerprint,
            fields_fingerprint=rules.fields_fingerprint,
            resolver_fingerprint=getattr(provenance, "resolver_fingerprint", None),
            lookup=bool(getattr(provenance, "lookup", False)),
        )
        return ModelIdentity(
            canonical=parsed.canonical, tags=tuple(parsed.tags()), metadata=metadata
        )

    def absent(self) -> dict[str, Any]:
        rules = self._normalizer.rules
        return identity_metadata(
            None,
            route=None,
            routes=None,
            rules_version=rules.version,
            identity_fingerprint=rules.identity_fingerprint,
            fields_fingerprint=rules.fields_fingerprint,
            resolver_fingerprint=None,
            lookup=False,
        )


class ModelNameResolverError(ValueError):
    """The configured resolver cannot be built or cannot read a model reference."""


def build_model_name_resolver(kind: str, rules_path: str | None) -> ModelNameResolver:
    if kind == "toloka":
        return NormalizerModelNameResolver(rules_path)
    if rules_path:
        raise ModelNameResolverError(
            "observability.tracing.model_name_rules needs model_name_normalizer='toloka'"
        )
    return RawModelNameResolver()
