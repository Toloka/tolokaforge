"""Orchestrator-side compute path for the model-data fingerprint.

Reads the resolved-preset accessor
(:func:`tolokaforge.core.llm.presets.get_resolved_presets`), the runtime
pricing dict (:data:`tolokaforge.core.pricing.MODEL_PRICING`), and the
certificate registry (:data:`tolokaforge.testing.certify.ALL_MODELS`)
— every one of which is already resolved by the CLI startup path before
an orchestrator write site invokes :func:`compute_models_fingerprint`.
The schema types (:class:`ModelsFingerprint`, the three module version
constants) live in the sibling :mod:`tolokaforge.core.model_data`, which
carries no first-party imports and is safe to reach from runner-subset
code.

See ADR-0030 § "Fingerprinting for auditability" for the wheel-split
context.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from tolokaforge.core.llm.presets import get_resolved_presets
from tolokaforge.core.model_data import (
    MODELS_FINGERPRINT_API_VERSION,
    MODELS_MINIMUM_ENGINE_VERSION,
    MODELS_PACKAGE_VERSION,
    ModelsFingerprint,
)
from tolokaforge.core.pricing import MODEL_PRICING
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

__all__ = ["compute_models_fingerprint"]


def _capability_key(capability: Capability) -> str:
    """Return the snake_case string representation of a :class:`Capability`.

    :class:`Capability` is a ``str, Enum`` subclass; ``.value`` is the
    canonical serialisation and the rest of the tree already uses it
    (see ``certificate.py``'s overlap-error message). Using ``.name``
    would create a lone inconsistency a future consumer would eventually
    hit.
    """
    return capability.value


def _certificate_to_dict(cert: ModelCertificate) -> dict[str, Any]:
    """Serialise one :class:`ModelCertificate` to a JSON-safe dict.

    :class:`ModelCertificate` freezes ``known_unsupported_reasons``,
    ``probe_params``, and ``capability_extras`` as
    :class:`types.MappingProxyType` in ``__post_init__``. Those wrappers
    are not deepcopyable, which rules out :func:`dataclasses.asdict`
    (probed live: ``TypeError: cannot pickle 'mappingproxy' object``).
    This helper unwraps each mapping field by hand and expresses every
    :class:`Capability` via ``.value`` (snake_case).

    The ``probe_params`` values must remain JSON-safe (``str`` / ``int``
    / ``float`` / ``bool`` / ``None`` / nested dicts). A future
    certificate that adds a non-JSON-serialisable inner value would
    break :func:`compute_models_fingerprint` at run start with a loud
    ``TypeError`` at ``json.dumps`` — easy to diagnose.
    """
    return {
        "model_id": cert.model_id,
        "provider": cert.provider,
        "name": cert.name,
        "env_key": cert.env_key,
        "required": sorted(_capability_key(c) for c in cert.required),
        "known_unsupported": sorted(_capability_key(c) for c in cert.known_unsupported),
        "excluded_capabilities": sorted(_capability_key(c) for c in cert.excluded_capabilities),
        "known_unsupported_reasons": {
            _capability_key(cap): reason
            for cap, reason in sorted(
                cert.known_unsupported_reasons.items(),
                key=lambda kv: _capability_key(kv[0]),
            )
        },
        "probe_params": {
            _capability_key(cap): dict(params)
            for cap, params in sorted(
                cert.probe_params.items(),
                key=lambda kv: _capability_key(kv[0]),
            )
        },
        "capability_extras": dict(cert.capability_extras),
    }


def compute_models_fingerprint() -> ModelsFingerprint:
    """Compute the resolved model-data fingerprint for the current engine.

    Reads three module-level surfaces — all resolved before any
    orchestrator write site is reached:

    * :func:`~tolokaforge.core.llm.presets.get_resolved_presets` — the
      bundled preset table merged with the operator overlay (if any).
    * :data:`~tolokaforge.core.pricing.MODEL_PRICING` — the pricing
      table with the operator overlay merged in via
      :func:`~tolokaforge.core.pricing.reload_pricing`.
    * :data:`~tolokaforge.testing.certify.ALL_MODELS` — the certificate
      registry.

    The hash is sha256 over the UTF-8 bytes of
    ``json.dumps(payload, sort_keys=True, ensure_ascii=True,
    separators=(",", ":"))`` — deterministic, whitespace-free, and
    order-independent.
    """
    payload: dict[str, Any] = {
        "presets": get_resolved_presets(),
        "pricing": dict(MODEL_PRICING),
        "certificates": [_certificate_to_dict(cert) for cert in ALL_MODELS],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ModelsFingerprint(
        package_version=MODELS_PACKAGE_VERSION,
        content_sha256=digest,
        api_version=MODELS_FINGERPRINT_API_VERSION,
        minimum_engine_version=MODELS_MINIMUM_ENGINE_VERSION,
    )
