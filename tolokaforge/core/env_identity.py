"""Content-addressed identity for a resolved :class:`EnvironmentManifest`.

:func:`resolve_environment_identity` returns a ``sha256:<hex>`` digest
over the canonicalised compose file bytes, ``stack_inputs``, the
per-service isolation map, and every referenced seed digest. Two
manifests with matching inputs produce equal identities regardless of
YAML formatting; any change to a compose byte, an input, a service
label, or a seed's digest flips the identity.

Emitted for observability at run start; not persisted, not consumed by
any dedup/materialisation path — those consumers land later per the
public roadmap.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml

from tolokaforge.core.models import EnvironmentManifest


def resolve_environment_identity(
    env: EnvironmentManifest,
    seed_digests: dict[str, str] | None = None,
) -> str:
    """Return a ``sha256:<64 hex chars>`` digest for ``env``.

    ``seed_digests`` maps every seed name referenced by any service's
    ``reset.seed`` to its declared ``digest``. The orchestrator resolves
    this from ``project.assets.seeds`` before calling. When no service
    references a seed, an empty dict is sufficient.

    The digest is stable across environments and orderings: dict keys
    sort before hashing, and the compose file is normalised through a
    ``yaml.safe_load`` / ``yaml.safe_dump`` round-trip with sorted keys
    so an author reformatting the file without changing its content
    does not shift the identity.
    """
    canonical = {
        "compose": _canonical_compose_bytes(env.load_compose()),
        "inputs": dict(env.stack_inputs),
        "services": {
            name: {
                "isolation": spec.isolation,
                "reset_seed": spec.reset.seed if spec.reset is not None else None,
            }
            for name, spec in sorted(env.services.items())
        },
        "seeds": dict(sorted((seed_digests or {}).items())),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    hex_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{hex_digest}"


def _canonical_compose_bytes(compose_content: dict[str, Any]) -> str:
    """Round-trip the compose mapping through YAML with sorted keys so
    the digest input is invariant under formatting-only edits.
    """
    return yaml.safe_dump(
        compose_content,
        sort_keys=True,
        default_flow_style=False,
    )


__all__ = ["resolve_environment_identity"]
