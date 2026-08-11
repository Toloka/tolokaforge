"""ModelCertificate dataclass — declares one model's capability contract.

Every certificate in :data:`tolokaforge.testing.certify.ALL_MODELS`
is a frozen :class:`ModelCertificate`. The five required fields
(``model_id`` / ``provider`` / ``name`` / ``env_key`` / ``required``)
plus ``known_unsupported`` form the *honesty contract*: for a given
capability the certificate MUST either commit to passing it
(``required``) or explicitly opt out (``known_unsupported``). Overlap
is a construction-time :class:`ValueError`.

The four additional fields — ``excluded_capabilities``,
``known_unsupported_reasons``, ``probe_params``, ``capability_extras``
— widen the certificate to carry per-model exclusions, human-readable
rationales, per-probe parameter overrides, and opaque quirks that live
outside the :class:`Capability` enum. All four default to empty
containers; the existing constructor calls in the registry continue to
work without change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ._capability import Capability

__all__ = ["ModelCertificate"]


@dataclass(frozen=True)
class ModelCertificate:
    """Declares which capabilities a model is contractually bound to
    support (``required``) and which it is known *not* to support
    (``known_unsupported``).

    Fields
    ------
    model_id:
        Stable filesystem-safe slug — MUST equal
        ``tolokaforge.core.output.artifacts.model_id_slug(provider, name)``
        so per-trial ``results/tools_schemas/<task>__<model_id>.json``
        sidecars and the capability tests share the same identifier.
        Validated by the canonical capability-registry test.
    provider:
        litellm-style provider key — e.g. ``"openrouter"``, ``"nova"``.
    name:
        Full litellm model identifier — e.g.
        ``"anthropic/claude-opus-4.7"``, ``"Nova Pro v3"``.
    env_key:
        Environment variable that must be set for this provider's live
        tests to run. The shared ``live_client`` fixture skips the test
        when it's missing.
    required:
        Capabilities that MUST pass — failures block the PR.
    known_unsupported:
        Capabilities that are deliberately out of scope for this model —
        the matching capability test auto-skips with a friendly message.
    excluded_capabilities:
        Per-model exclusion set consumed by shared probe bodies that
        need to opt out for reasons orthogonal to
        ``required`` / ``known_unsupported`` (e.g. an unreliable
        provider-side counter). Empty by default.
    known_unsupported_reasons:
        Human-readable rationale keyed by ``Capability`` — surfaced by
        report generators and dashboards. Empty by default.
    probe_params:
        Per-capability probe-parameter overrides — e.g.
        ``{Capability.PROMPT_CACHING: {"prompt_tokens": 12000}}`` — for
        shared bodies that consult the map. Empty by default.
    capability_extras:
        Opaque per-model quirks that do not fit the ``Capability``
        enum — free-form string values consulted by adapter code paths.
        Empty by default.

    Invariants enforced at construction time
    ----------------------------------------
    ``required`` and ``known_unsupported`` MUST be disjoint; listing the
    same capability in both is dishonest (the test would both be expected
    to pass AND expected to skip) and raises :class:`ValueError`.

    ``required`` and ``excluded_capabilities`` MUST also be disjoint — a
    capability the contract binds the model to pass cannot simultaneously
    opt out of the probe that would verify it.
    """

    model_id: str
    provider: str
    name: str
    env_key: str
    required: frozenset[Capability] = field(default_factory=frozenset)
    known_unsupported: frozenset[Capability] = field(default_factory=frozenset)
    excluded_capabilities: frozenset[Capability] = field(default_factory=frozenset)
    known_unsupported_reasons: Mapping[Capability, str] = field(default_factory=dict)
    probe_params: Mapping[Capability, Mapping[str, Any]] = field(default_factory=dict)
    capability_extras: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        overlap = self.required & self.known_unsupported
        if overlap:
            shared = sorted(c.value for c in overlap)
            raise ValueError(
                f"Certificate {self.model_id!r} lists the same capability in "
                f"both `required` and `known_unsupported`: {shared}. Pick one — "
                "either the model supports the capability (required) or it "
                "doesn't (known_unsupported)."
            )
        excluded_overlap = self.required & self.excluded_capabilities
        if excluded_overlap:
            shared = sorted(c.value for c in excluded_overlap)
            raise ValueError(
                f"Certificate {self.model_id!r} lists the same capability in "
                f"both `required` and `excluded_capabilities`: {shared}. A "
                "required capability cannot opt out of the probe that verifies "
                "it — drop the exclusion or move the capability out of required."
            )
        # Freeze mapping fields so mutation-after-construction can't
        # corrupt a supposedly frozen certificate. MappingProxyType wraps
        # a copy, so mutations to the caller's source dict do not bleed in.
        object.__setattr__(
            self,
            "known_unsupported_reasons",
            MappingProxyType(dict(self.known_unsupported_reasons)),
        )
        object.__setattr__(
            self,
            "probe_params",
            MappingProxyType({k: MappingProxyType(dict(v)) for k, v in self.probe_params.items()}),
        )
        object.__setattr__(
            self,
            "capability_extras",
            MappingProxyType(dict(self.capability_extras)),
        )

    def __hash__(self) -> int:
        # Skip the Mapping fields: MappingProxyType delegates hash to
        # its underlying dict (unhashable). Two certs equal by identity
        # of model_id + hashable sets are the common case; the mapping
        # fields carry rationale / probe overrides that are compared for
        # equality but not part of the hash key.
        return hash(
            (
                self.model_id,
                self.provider,
                self.name,
                self.env_key,
                self.required,
                self.known_unsupported,
                self.excluded_capabilities,
            )
        )
