"""Resolve ``${secret:NAME}`` references inside a configuration value.

This is the only sanctioned way to make a non-secret configuration string carry a
secret value. See ``docs/LLM_LAYER.md`` § "Values may reference secrets" for the
rationale, the failure rules, and why the reference form is typed rather than a
bare ``$NAME``.

Deliberately NOT part of :meth:`SecretManager.get_secret`: that method is the
universal credential read path, and it also feeds the log-redaction set and the
container serializer, both of which resolve every enumerable key. Expanding there
would run this syntax over values the engine never asked for, where a literal
``$`` in a real credential (argon2, bcrypt, generated passwords) is common.
Expansion is a composition concern, so the caller asks for it explicitly.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tolokaforge.secrets.manager import SecretManager

__all__ = ["UnresolvedReferenceError", "expand_secret_refs"]

#: A well-formed reference. ``NAME`` follows the identifier rule env vars obey.
_REF_RE = re.compile(r"\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}")

#: Anything that opens a reference. Whatever survives substitution is malformed,
#: so an unclosed or misspelled reference is refused instead of being passed
#: through as literal text.
_PARTIAL_RE = re.compile(r"\$\{secret\b")


class UnresolvedReferenceError(Exception):
    """A ``${secret:NAME}`` reference could not be resolved to a value.

    Attributes:
        where: Human-readable location of the offending value, for the message.
        names: Reference names that failed to resolve; empty for a syntax error.
    """

    def __init__(self, message: str, *, where: str, names: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.where = where
        self.names = names


def expand_secret_refs(value: str, secrets: SecretManager, *, where: str) -> str:
    """Replace every ``${secret:NAME}`` in ``value`` with its resolved secret.

    Args:
        value: The configuration string, which may contain zero or more references.
        secrets: Manager used to resolve each referenced name.
        where: What this value is, named in any error (e.g. ``"LLM_PROXY_HEADERS
            value for 'X-Order-Id'"``).

    Returns:
        ``value`` with every reference replaced. A string containing no reference
        is returned unchanged, including one that contains a literal ``$``.

    Raises:
        UnresolvedReferenceError: A referenced name is unset or blank, or the
            string contains a malformed reference. Neither is substituted with an
            empty string: a blank value fails or misattributes far from the
            misconfigured line, and a literal ``${secret:...}`` on the wire is
            worse still.
    """
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = secrets.get_secret(name)
        if resolved is None or not resolved.strip():
            missing.append(name)
            return ""
        return resolved

    expanded = _REF_RE.sub(_sub, value)

    if missing:
        names = tuple(sorted(set(missing)))
        refs = ", ".join(f"${{secret:{name}}}" for name in names)
        raise UnresolvedReferenceError(
            f"{where} references {refs}, which is not set. Set it, or write the value "
            f"literally. It is never substituted as empty: see docs/LLM_LAYER.md.",
            where=where,
            names=names,
        )

    if _PARTIAL_RE.search(expanded):
        raise UnresolvedReferenceError(
            f"{where} contains a malformed secret reference. The only accepted form is "
            f"${{secret:NAME}}, with NAME matching [A-Za-z_][A-Za-z0-9_]*. A partial or "
            f"misspelled reference is refused rather than passed through as literal "
            f"text, which would put ${{secret:...}} on the wire.",
            where=where,
        )

    return expanded
