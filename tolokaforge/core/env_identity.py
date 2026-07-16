"""Identity views over a resolved :class:`EnvironmentManifest`.

:func:`resolve_environment_identity` returns a ``sha256:<hex>`` digest
over the canonicalised compose file bytes, ``stack_inputs``, the
per-service isolation map, and every referenced seed digest. Two
manifests with matching inputs produce equal identities regardless of
YAML formatting; any change to a compose byte, an input, a service
label, or a seed's digest flips the identity. Emitted for observability
at run start; not persisted.

:func:`describe_environment_identity` returns a human-readable
:class:`EnvironmentIdentity` descriptor — per-service resolved images,
isolation, reset seeds, redacted connection strings, and container mount
targets, plus the run-level network policy and runner-service pointer. A
pure function of the manifest, safe to serialise into a trial artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, Field

from tolokaforge.core.models import EnvironmentManifest
from tolokaforge.runner.models import _FLOATING_IMAGE_TAGS, _image_tag_or_digest


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


class ServiceIdentity(BaseModel):
    """Resolved identity of a single compose service."""

    image: str | None = None
    pinned: bool = False
    isolation: str
    reset_seed: str | None = None
    dsns: list[str] = Field(default_factory=list)
    mounts: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class EnvironmentIdentity(BaseModel):
    """Resolved identity of a whole manifest-driven environment."""

    network_policy: str
    runner_service: str
    services: dict[str, ServiceIdentity]

    model_config = {"extra": "forbid"}


_IMAGE_VAR_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def describe_environment_identity(manifest: EnvironmentManifest) -> EnvironmentIdentity:
    """Return the resolved :class:`EnvironmentIdentity` for ``manifest``.

    A pure function of the manifest's compose content, ``stack_inputs``,
    per-service isolation map, network policy, and runner-service pointer.
    Compose services absent from ``manifest.services`` default to
    ``ephemeral`` isolation, mirroring the resolver's fill rule.
    """
    compose_services = manifest.load_compose()["services"]
    services = {
        name: _describe_service(name, body, manifest)
        for name, body in sorted(compose_services.items())
    }
    return EnvironmentIdentity(
        network_policy=manifest.network_policy.value,
        runner_service=manifest.runner_service,
        services=services,
    )


def _describe_service(
    name: str, body: dict[str, Any], manifest: EnvironmentManifest
) -> ServiceIdentity:
    spec = manifest.services.get(name)
    image = _resolve_image(body.get("image"), manifest.stack_inputs)
    return ServiceIdentity(
        image=image,
        pinned=_is_pinned(image),
        isolation=spec.isolation if spec is not None else "ephemeral",
        reset_seed=spec.reset.seed if spec is not None and spec.reset is not None else None,
        dsns=_service_dsns(body),
        mounts=_service_mounts(body),
    )


def _resolve_image(image: Any, stack_inputs: dict[str, str]) -> str | None:
    """Substitute ``${VAR}`` / ``${VAR:-default}`` slots from ``stack_inputs``.

    A ``${VAR}`` with no default that is absent from ``stack_inputs`` cannot
    be resolved — raise rather than emit a half-substituted reference.
    """
    if image is None:
        return None

    def _repl(match: re.Match[str]) -> str:
        var = match.group("name")
        if var in stack_inputs:
            return stack_inputs[var]
        default = match.group("default")
        if default is not None:
            return default
        raise ValueError(
            f"environment identity: image reference {image!r} uses ${{{var}}} which is "
            "absent from stack_inputs and has no default."
        )

    return _IMAGE_VAR_RE.sub(_repl, image)


def _is_pinned(image: str | None) -> bool:
    """True when ``image`` carries a digest or a non-floating tag."""
    if not image:
        return False
    tag, digest = _image_tag_or_digest(image)
    if digest is not None:
        return True
    if tag is None:
        return False
    return tag.lower() not in _FLOATING_IMAGE_TAGS


def _service_dsns(body: dict[str, Any]) -> list[str]:
    """Redacted connection strings from the service's compose ``environment``.

    Only URI-shaped values (scheme + netloc) are recorded; each has any
    embedded password replaced with ``***``. Non-URI env values (e.g. a bare
    ``POSTGRES_PASSWORD``) are excluded entirely.
    """
    dsns = [_redact_dsn(value) for value in _environment_values(body) if _looks_like_dsn(value)]
    return sorted(dsns)


def _environment_values(body: dict[str, Any]) -> list[str]:
    env = body.get("environment")
    if env is None:
        return []
    if isinstance(env, dict):
        return [str(value) for value in env.values()]
    if isinstance(env, list):
        return [item.split("=", 1)[1] for item in env if isinstance(item, str) and "=" in item]
    raise ValueError(
        f"compose `environment:` must be a mapping or a list; got {type(env).__name__}"
    )


def _looks_like_dsn(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(parsed.scheme and parsed.netloc)


_PASSWORD_QUERY_KEYS = frozenset({"password", "sslpassword", "pgpassword"})


def _redact_dsn(value: str) -> str:
    """Replace an embedded password with ``***`` wherever it lives — the
    netloc userinfo password and any password-bearing query parameter
    (``password``, ``sslpassword``, ``pgpassword``, matched case-insensitively).
    Every other component is preserved; a value carrying no password anywhere
    is returned unchanged."""
    parsed = urlsplit(value)
    redacted_query = _redact_query_passwords(parsed.query)
    if parsed.password is None and redacted_query == parsed.query:
        return value
    netloc = parsed.netloc
    if parsed.password is not None:
        userinfo, _, hostport = netloc.rpartition("@")
        user = userinfo.partition(":")[0]
        netloc = f"{user}:***@{hostport}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, redacted_query, parsed.fragment))


def _redact_query_passwords(query: str) -> str:
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    redacted = [
        (key, "***" if key.lower() in _PASSWORD_QUERY_KEYS else value) for key, value in pairs
    ]
    return urlencode(redacted, safe="*")


def _service_mounts(body: dict[str, Any]) -> list[str]:
    """Container-side mount targets (``"<target>:<mode>"``, mode omitted when
    absent). Host source paths are excluded."""
    volumes = body.get("volumes")
    if not volumes:
        return []
    mounts = [_mount_descriptor(entry) for entry in volumes]
    return [mount for mount in mounts if mount is not None]


def _mount_descriptor(entry: Any) -> str | None:
    if isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) == 1:
            target, mode = parts[0], None
        elif len(parts) == 2:
            target, mode = parts[1], None
        else:
            target, mode = parts[1], parts[2]
    elif isinstance(entry, dict):
        target = entry.get("target")
        if not isinstance(target, str):
            return None
        mode = "ro" if entry.get("read_only") else None
    else:
        return None
    return f"{target}:{mode}" if mode else target


__all__ = [
    "EnvironmentIdentity",
    "ServiceIdentity",
    "describe_environment_identity",
    "resolve_environment_identity",
]
