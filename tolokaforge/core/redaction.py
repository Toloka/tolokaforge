"""Redaction by key name — the credential-naming vocabulary and the policies over it.

:func:`key_is_sensitive` is the engine's single answer to "does this key name a
credential". :meth:`StructuredLogger._sanitize_extra`
(:mod:`tolokaforge.core.logging`) asks it per log-context key; the policies here
ask it per key of every mapping the writer puts on disk — tool-call arguments,
the final environment snapshot, the task snapshot, the tool schemas, the verdict,
each log record — and hand back a mapping whose credential-named values have been
replaced. One vocabulary, so widening it is a single edit rather than two lists
drifting apart.

The log path's walk is deliberately shallow — it reads a context's top-level keys
and does not descend, so a console line stays a cheap thing to emit. Callers do
log mapping-valued context, and the write-time policy is what reaches a credential
nested inside one on its way into a bundle.

The rule reads names, never values, so a credential that travels under an
innocuous key — or back out through a tool's own output — is outside it by
construction. Closing that is the value-based pass on #1157.

**Recursion, and why there is no cycle guard.** ``SensitiveKeyRedaction``
descends into nested mappings and into mappings inside lists, without a depth
cap: a trace-check matcher addresses ``body.query``-style paths, so a shallow
rule would leave a credential one level down in the clear. The inputs are
mappings parsed off the wire (gRPC and YAML), therefore acyclic by construction,
and a guard would only mute a corruption that cannot occur. Nested keys need not
be strings — an adapter's environment snapshot may key records by integer id —
and a key that is not a string names nothing, so the walk descends past it
instead of asking the vocabulary about it.

**Known-inert entries in the exact set**, recorded so a reader does not mistake
them for coverage: :func:`key_is_sensitive` matches exact tokens per
non-alphanumeric-delimited *part*, so a multi-word entry can never match as a
whole. ``access_token`` and ``refresh_token`` are redundant (they already match
via the bare ``token`` part) and ``session_id`` is dead (neither part is in
either set). Repairing that changes what every log line in the process redacts,
so it is #1158's own behaviour change rather than a ride-along here.

:mod:`tolokaforge.secrets.log_filter` keeps its own placeholder constant on
purpose. It is the *value*-based path — a process-global scrub of resolved
secret values — and collapsing the two now would couple this key-based
vocabulary to a mechanism whose design is still #1157's.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

__all__ = [
    "REDACTED_PLACEHOLDER",
    "NoRedaction",
    "RedactionPolicy",
    "RedactionPolicyName",
    "RedactionStamp",
    "SensitiveKeyRedaction",
    "key_is_sensitive",
]

REDACTED_PLACEHOLDER = "***REDACTED***"

#: Substrings identifying keys whose values must not be written out. Matched
#: anywhere in the lowercased key, so `password_hash` matches.
_SENSITIVE_KEY_SUBSTRINGS: frozenset[str] = frozenset(
    {"password", "secret", "api_key", "apikey", "authorization", "credential"}
)

#: Exact token names matched per key part. `token` on its own is a credential;
#: `max_tokens` / `total_tokens` are telemetry and stay readable. Kept separate
#: from the substring set so a future ambiguous name is a discussion, not a
#: silent regression.
_SENSITIVE_KEY_EXACT_TOKENS: frozenset[str] = frozenset(
    {"token", "access_token", "refresh_token", "bearer", "session_id"}
)


def key_is_sensitive(key: str) -> bool:
    """Return whether *key* names a value that must not be written out.

    Substring markers match anywhere in the key (case-insensitive). Exact
    tokens match a whole non-alphanumeric-delimited part — `token` matches
    `bearer_token` and `token`, but not `max_tokens` / `prompt_tokens`.
    """
    lower = key.lower()
    if any(marker in lower for marker in _SENSITIVE_KEY_SUBSTRINGS):
        return True
    parts = re.split(r"[^a-z0-9]+", lower)
    return any(token in _SENSITIVE_KEY_EXACT_TOKENS for token in parts)


class RedactionPolicyName(str, Enum):
    """Closed vocabulary of policy names a bundle's stamp can carry."""

    NONE = "none"
    SENSITIVE_KEYS = "sensitive_keys"


class RedactionPolicy(Protocol):
    """What a writer needs from a redaction policy.

    ``name`` is a class-level constant on each policy, declared read-only here so
    that a policy cannot be constructed claiming a name other than the one it
    implements — a rewriting policy stamping ``none`` is the one shape a bundle's
    reader could not detect.
    """

    @property
    def name(self) -> RedactionPolicyName:
        """The name this policy stamps a bundle with."""
        ...

    def redact_mapping(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        """Return *mapping* with credential-named values replaced."""
        ...


@dataclass(frozen=True)
class NoRedaction:
    """The default: a mapping reaches disk exactly as the run produced it."""

    name: ClassVar[RedactionPolicyName] = RedactionPolicyName.NONE

    def redact_mapping(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        return dict(mapping)


@dataclass(frozen=True)
class SensitiveKeyRedaction:
    """Replace values under credential-named keys, at every nesting level."""

    name: ClassVar[RedactionPolicyName] = RedactionPolicyName.SENSITIVE_KEYS

    def redact_mapping(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        return _redact_mapping(mapping)


def _redact_mapping(mapping: Mapping[Any, Any]) -> dict[Any, Any]:
    return {
        key: (
            REDACTED_PLACEHOLDER
            if isinstance(key, str) and key_is_sensitive(key)
            else _redact_value(value)
        )
        for key, value in mapping.items()
    }


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


class RedactionStamp(BaseModel):
    """A bundle's declaration that a policy rewrote what it carries.

    Absent from ``metrics.yaml`` means the bundle is faithful. Present means an
    offline reader must refuse it: the arguments it would grade are not the
    arguments the agent sent.
    """

    model_config = ConfigDict(extra="forbid")

    policy: RedactionPolicyName
    artifacts: list[str]
    omitted: list[str]
