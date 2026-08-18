"""What a run records about the harness registry it ran on.

The effective registry is composed from three layers — the shipped
``data/harnesses.yaml``, any installed entry-point plugin bundles, and the
operator's ``harness_presets_file`` overlay — so two runs of the same config
on two machines can drive different CLI versions. The fingerprint names the
composition in content-derivable terms: identical layers give an identical
:attr:`HarnessFingerprint.resolved_sha256`, and any drift in any layer gives
a different one, with :attr:`HarnessFingerprint.overlay_file` and
:attr:`HarnessFingerprint.plugin_bundles` naming where the drift came from.

Lives beside the registry it describes because it is a pure function of
registry content.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from ._registry import HARNESSES, HarnessSpec, PluginBundle, ResolvedHarnessRegistry

__all__ = ["HarnessFingerprint", "compute_harness_fingerprint"]

_Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class HarnessFingerprint(BaseModel):
    """The harness-registry facts one run resolved.

    Every field is read off resolved content. A field that cannot be derived
    from the registry the run composed does not belong here.
    """

    model_config = ConfigDict(extra="forbid")

    resolved_sha256: _Sha256Hex
    """Digest over the post-plugin, post-overlay registry."""

    shipped_sha256: _Sha256Hex
    """Digest over the shipped registry alone. Equal to
    :attr:`resolved_sha256` exactly when no layer changed anything."""

    agent_harness: str
    """The harness this run selected."""

    agent_harness_version: str | None
    """The effective spec's pinned version, so an overlay that bumps a pin
    shows the version that installs. ``None`` when the selected harness is
    not a registry key — the engine loop, which runs no CLI."""

    overlay_file: str | None
    """Resolved absolute path of the ``harness_presets_file`` overlay."""

    plugin_bundles: tuple[PluginBundle, ...]
    """The installed registry plugins that contributed, ordered by
    distribution."""


def _digest(registry: Mapping[str, HarnessSpec]) -> str:
    """Digest over *registry*'s parsed content.

    The recipe is the one
    :func:`tolokaforge.core.model_data_fingerprint.compute_models_fingerprint`
    uses, so both fingerprints on a run bundle mean the same thing by the same
    construction. Hashing the parsed specs rather than the YAML bytes is what
    makes ``shipped == resolved`` a precise statement that no layer changed
    anything, and keeps a comment-only registry edit from moving the digest.
    """
    payload = {name: spec.model_dump(mode="json") for name, spec in registry.items()}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_harness_fingerprint(
    resolved: ResolvedHarnessRegistry, agent_harness: str
) -> HarnessFingerprint:
    """The fingerprint of the registry *resolved* composed, as *agent_harness* ran on it.

    ``data/registry_meta.yaml`` is deliberately outside both digests: it is
    shipped-only rather than layerable, so folding it in would blur what
    ``shipped_sha256 == resolved_sha256`` means.
    """
    spec = resolved.harnesses.get(agent_harness)
    return HarnessFingerprint(
        resolved_sha256=_digest(resolved.harnesses),
        shipped_sha256=_digest(HARNESSES),
        agent_harness=agent_harness,
        agent_harness_version=spec.version if spec is not None else None,
        overlay_file=str(resolved.overlay_file) if resolved.overlay_file else None,
        plugin_bundles=resolved.plugin_bundles,
    )
