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

    @property
    def description(self) -> str: ...


class RawModelNameResolver:
    """No rules: ``vendor/model`` as spelled, ``provider/name`` for a bare name."""

    description = "raw"

    def resolve(self, provider: str | None, name: str) -> ModelIdentity:
        canonical = name if "/" in name else (f"{provider}/{name}" if provider else name)
        vendor = canonical.split("/", 1)[0] if "/" in canonical else NONE
        return ModelIdentity(
            canonical=canonical,
            tags=(f"model:{canonical}",),
            metadata={
                "model_vendor": vendor,
                "model_family": NONE,
                "model_route": provider or NONE,
                "model_rules_version": NONE,
                "model_lookup": False,
            },
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
        fields = parsed.fields()
        rules = self._normalizer.rules
        metadata: dict[str, Any] = {
            f"model_{facet}": (value if value not in (None, "") else NONE)
            for facet, value in fields.items()
        }
        metadata.update(
            {
                "model_route": parsed.route or NONE,
                "model_routes": "/".join(parsed.routes) if parsed.routes else NONE,
                "model_rules_version": rules.version,
                "model_identity_fingerprint": rules.identity_fingerprint,
                "model_fields_fingerprint": rules.fields_fingerprint,
                "model_lookup": parsed.provenance.lookup,
            }
        )
        return ModelIdentity(
            canonical=parsed.canonical, tags=tuple(parsed.tags()), metadata=metadata
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
