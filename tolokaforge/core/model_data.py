"""Public engine seam for model-data fingerprinting.

The fingerprint identifies the resolved model-data surface a completed run
was scored against: the preset table (bundled ⊕ overlay), the pricing
table (bundled ⊕ overlay), and the certificate registry. Consumers store
:class:`ModelsFingerprint` alongside a run's engine state to reproduce
which model-data snapshot was in effect at run start.

The module is a *pure* seam — no I/O, no subprocess, no orchestrator
imports. It reads the resolved-preset accessor
(:func:`tolokaforge.core.llm.presets.get_resolved_presets`), the
runtime pricing dict (:data:`tolokaforge.core.pricing.MODEL_PRICING`),
and the certificate tuple
(:data:`tolokaforge.testing.certify.ALL_MODELS`) — every one of which
is already resolved by the CLI startup path before an orchestrator write
site invokes :func:`compute_models_fingerprint`.

See ADR-0030 § "Fingerprinting for auditability" for the wheel-split
context. Pre-cutover, :data:`MODELS_PACKAGE_VERSION` is the literal
``"in-tree"`` sentinel; #938 moves the three constants
(:data:`MODELS_PACKAGE_VERSION`, :data:`MODELS_MINIMUM_ENGINE_VERSION`,
:data:`MODELS_FINGERPRINT_API_VERSION`) into the ``tolokaforge-models``
wheel's ``__init__``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from tolokaforge.core.llm.presets import get_resolved_presets
from tolokaforge.core.pricing import MODEL_PRICING
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

__all__ = [
    "MODELS_FINGERPRINT_API_VERSION",
    "MODELS_MINIMUM_ENGINE_VERSION",
    "MODELS_PACKAGE_VERSION",
    "ModelsFingerprint",
    "compute_models_fingerprint",
    "decode_models_fingerprint",
]


#: Pre-cutover sentinel — flipped to the ``tolokaforge-models`` wheel's
#: PEP 440 version string when #938 lands.
MODELS_PACKAGE_VERSION: str = "in-tree"

#: PEP 440 specifier naming the engine floor the current bundled model
#: data (with #931's widened :class:`ModelCertificate`) is compatible
#: with. #938 will source this from the wheel.
MODELS_MINIMUM_ENGINE_VERSION: str = ">=0.16,<0.17"

#: Integer version of the fingerprint payload contract; bumped whenever
#: :func:`compute_models_fingerprint` changes the shape of the hashed
#: payload in a way readers must know about.
MODELS_FINGERPRINT_API_VERSION: int = 1


class ModelsFingerprint(BaseModel):
    """Resolved model-data snapshot recorded on ``engine_run_state.json``.

    Fields
    ------
    package_version:
        The ``tolokaforge-models`` PEP 440 version, or the literal
        ``"in-tree"`` sentinel while the data still ships in the engine
        wheel (pre-#938).
    content_sha256:
        Lowercase hex sha256 over the canonicalised
        ``{presets, pricing, certificates}`` triple. Same inputs →
        byte-identical digest; any overlay tweak → different digest.
    api_version:
        Contract version of the hashed payload — see
        :data:`MODELS_FINGERPRINT_API_VERSION`.
    minimum_engine_version:
        PEP 440 specifier the model-data snapshot requires the engine to
        satisfy — see :data:`MODELS_MINIMUM_ENGINE_VERSION`.
    """

    model_config = ConfigDict(extra="forbid")

    package_version: str
    content_sha256: str
    api_version: int
    minimum_engine_version: str


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


def decode_models_fingerprint(state: dict[str, Any]) -> ModelsFingerprint | None:
    """Return the parsed fingerprint from an ``engine_run_state.json`` dict.

    * ``None`` when the ``models_fingerprint`` field is absent (pre-#933
      runs never wrote it).
    * :class:`ModelsFingerprint` when the field is a well-formed dict.
    * Raises :class:`pydantic.ValidationError` when the field is a dict
      but malformed — loud-fail matches the existing malformed-JSON
      behaviour of ``read_engine_run_state``.
    """
    raw = state.get("models_fingerprint")
    if raw is None:
        return None
    return ModelsFingerprint.model_validate(raw)
