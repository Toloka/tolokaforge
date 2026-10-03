"""Outbound data-safety gate: nothing an uploader sends leaves unchecked.

The last thing between a payload and the receiver. The engine writes bundles with ``NoRedaction``
by default, ``SensitiveKeyRedaction`` reads key names only, and a planted credential can
legitimately sit in a prompt file; an agent transcript is written on a runner that holds provider
keys. So every payload a producer sends, ingestion events and media alike, is scanned as
**serialised bytes** before the send, fail-closed:

- **known secret values**: every credential the process itself holds (environment variables whose
  name looks like a secret; the caller may add the values it resolved another way) and, when the
  caller names one, the values of a ``.env`` file under secret-like keys;
- **shape patterns**: dotenv lines with secret-like keys, ``Authorization`` headers, PEM blocks,
  URL credentials (a redacted ``***`` password is not a hit), well-known key prefixes
  (``pk-lf-``, ``sk-lf-``, ``sk-or-``, ``sk-ant-``, ``ghp_``, ``github_pat_``, ``xox?-``,
  ``AKIA``, ``AIza``, ``glpat-``), JWTs, and secret-named JSON / YAML fields holding a long
  opaque value;

A structured value (a span's attributes, a list of events) is scanned as its JSON, shapes and
known values, and its raw strings are scanned for the known values too, which JSON escaping hides
(:meth:`SafetyGate.scan_structured`).

On a hit the bytes are **never rewritten** (an attachment must stay byte-exact): the caller skips
the file and names it in its receipt, or stops; a hit in the structured events blocks the send
outright. Findings name the rule and a masked excerpt (first four characters, never the tail),
never the value.

What an ATTACHMENT may be is a different question and lives with the attachments:
:func:`tolokaforge_langfuse.attachments.allowed_attachment`. Two allowlists under one name in
one package is how they drift apart.

Engine-free by construction, so the two transcript uploaders (``automation langfuse-upload`` in the
engine repository and the offline connector), the live observer's spans and the attachment step
(:class:`tolokaforge_langfuse.attachments.SecretScan` wraps this gate) scan with one
implementation and one set of fixtures.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE|SIGNING|COOKIE|SESSION)", re.I
)
# names that look secret-like but never hold a secret value; PWD and OLDPWD are the shell's
# working directories, which an agent's own text names all the time, and the tracing launcher's
# session id rides on every span by design
_NOT_SECRET_NAMES = re.compile(
    r"(PUBLIC_KEY_ID|_FILE$|_PATH$|_DIR$|_URL$|_NAME$|_HEADER$|^(?:OLD)?PWD$"
    r"|^TOLOKAFORGE_TRACING_SESSION_ID$)",
    re.I,
)
MIN_SECRET_VALUE = 8
# how deep JSON may be nested in a string a value is found in (an attribute holding JSON text
# whose strings hold JSON text)
JSON_NESTING = 3

SHAPES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "dotenv-secret",
        re.compile(
            rb"(?im)^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD)"
            rb"[A-Z0-9_]*\s*=\s*['\"]?[^\s'\"#]{8,}"
        ),
    ),
    (
        "authorization-header",
        re.compile(rb"(?i)authorization\W{0,3}\s*(?:bearer|basic)\s+[A-Za-z0-9+/=_\-.]{16,}"),
    ),
    ("pem-private-key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # user:password@host with a real password (a redacted *** is the engine's own DSN masking)
    (
        # the scheme is anchored and bounded (a free `[a-z][a-z0-9+.-]*` before `://` scans a long
        # lowercase run quadratically)
        "url-credentials",
        re.compile(
            rb"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,15}://[^/\s:@]+:(?![*]+@)[^@\s/]{3,}@[^\s/]+"
        ),
    ),
    (
        "langfuse-key",
        re.compile(rb"\b[ps]k-lf-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    ),
    ("openrouter-key", re.compile(rb"\bsk-or-v1-[0-9a-f]{20,}")),
    ("anthropic-key", re.compile(rb"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    (
        "github-token",
        re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    ),
    ("slack-token", re.compile(rb"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("gitlab-token", re.compile(rb"\bglpat-[A-Za-z0-9_\-]{20}")),
    ("jwt", re.compile(rb"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # a secret-named field (JSON / YAML) holding a long opaque token without spaces
    (
        "secret-field",
        re.compile(
            rb"(?i)[\"']?(?:api[_\-]?key|secret(?:[_\-]?key)?|access[_\-]?token|auth[_\-]?token|"
            rb"refresh[_\-]?token|client[_\-]?secret|private[_\-]?key|password|passwd)[\"']?\s*[:=]\s*"
            rb"[\"']?(?![\"']?(?:null|none|true|false|\*+|redacted|<[^>]*>|\$\{[^}]*\}|\[REDACTED\])[\"'\s,}]?)"
            rb"[A-Za-z0-9+/=_\-.]{16,}"
        ),
    ),
)


class SafetyError(RuntimeError):
    """A payload would carry a secret; nothing was sent."""


@dataclass(frozen=True)
class Finding:
    rule: str
    excerpt: str  # masked: at most the first four characters of the match, then ****

    def __str__(self) -> str:
        return f"{self.rule} ({self.excerpt})"


@dataclass
class SafetyGate:
    """Scans serialised payloads; built once per process with the secrets it must never leak."""

    # never in a repr: a traceback or a debugger would print every value the gate protects
    known_values: tuple[bytes, ...] = field(default=(), repr=False)
    # every rule -> how many hits it produced (for the receipt)
    hits: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv: Path | None = None,
        *,
        extra: Iterable[str] = (),
    ) -> SafetyGate:
        """Known secret values from the process environment, a ``.env`` file when given, and
        ``extra``: credential values the caller holds by another route (a secret manager)."""
        values: set[bytes] = set()
        for name, value in (env if env is not None else os.environ).items():
            if _looks_secret(name) and len(value) >= MIN_SECRET_VALUE:
                values.add(value.encode("utf-8", "surrogateescape"))
        if dotenv is not None and dotenv.exists():
            for line in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                value = value.strip().strip("'\"")
                if _looks_secret(name.strip()) and len(value) >= MIN_SECRET_VALUE:
                    values.add(value.encode("utf-8"))
        values.update(
            value.encode("utf-8", "surrogateescape")
            for value in extra
            if value and len(value) >= MIN_SECRET_VALUE
        )
        return cls(known_values=tuple(sorted(values, key=len, reverse=True)))

    def scan(self, payload: bytes, *, what: str = "payload") -> list[Finding]:
        """Every finding in ``payload`` (empty when the bytes are clean)."""
        findings: list[Finding] = []
        for value in self.known_values:
            if value and value in payload:
                # no leading characters for a credential the process holds: the head of an
                # arbitrary password is secret material, unlike a provider key prefix
                findings.append(Finding("known-secret-value", f"**** ({len(value)} chars)"))
        for rule, pattern in SHAPES:
            match = pattern.search(payload)
            if match:
                findings.append(Finding(rule, _mask(match.group(0))))
        for finding in findings:
            self.hits[finding.rule] = self.hits.get(finding.rule, 0) + 1
        return findings

    def scan_structured(self, value: Any, *, what: str = "payload") -> list[Finding]:
        """Every finding in a JSON-like ``value`` (a span's attributes, a list of events).

        The shapes and the known values run over the value serialised as JSON, which is what a
        receiver is sent. The known values also run over every raw string the value holds, in
        the forms JSON gives them: JSON escaping hides a credential with a quote or a backslash,
        and an attribute that is itself JSON text escapes it once more. The shapes do not run
        over raw strings: a line-anchored one would stop ordinary code (``api_key = os.environ...``)
        and is quadratic on a long line."""
        serialised = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8", "replace")
        found = self.scan(serialised, what=what)
        if not self.known_values:
            return found
        raw = "\x00".join(_strings(value)).encode("utf-8", "replace")
        seen = {(finding.rule, finding.excerpt) for finding in found}
        for known, forms in self._forms:
            if any(form in raw for form in forms):
                finding = Finding("known-secret-value", f"**** ({len(known)} chars)")
                if (finding.rule, finding.excerpt) not in seen:
                    seen.add((finding.rule, finding.excerpt))
                    found.append(finding)
                    self.hits[finding.rule] = self.hits.get(finding.rule, 0) + 1
        return found

    @cached_property
    def _forms(self) -> tuple[tuple[bytes, tuple[bytes, ...]], ...]:
        """Each known value with the forms JSON gives it, escaped up to ``JSON_NESTING`` times."""
        pairs = []
        for known in self.known_values:
            if not known:
                continue
            forms, text = [known], known.decode("utf-8", "surrogateescape")
            for _ in range(JSON_NESTING):
                text = json.dumps(text, ensure_ascii=False)[1:-1]
                form = text.encode("utf-8", "surrogateescape")
                if form == forms[-1]:
                    break
                forms.append(form)
            pairs.append((known, tuple(forms)))
        return tuple(pairs)

    def check(self, payload: bytes, *, what: str) -> None:
        """Raise ``SafetyError`` naming the rules when ``payload`` is not clean."""
        findings = self.scan(payload, what=what)
        if findings:
            raise SafetyError(
                f"{what}: would carry a secret: " + ", ".join(str(f) for f in findings)
            )


def _looks_secret(name: str) -> bool:
    return bool(SECRET_NAME.search(name)) and not _NOT_SECRET_NAMES.search(name)


def _mask(value: bytes) -> str:
    text = value.decode("utf-8", "replace").strip()
    head = text[:4] if len(text) > 12 else ""
    return f"{head}**** ({len(text)} chars)"


def _strings(value: Any) -> Iterator[str]:
    """Every string in ``value``, the keys included, and every other scalar as text."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)
