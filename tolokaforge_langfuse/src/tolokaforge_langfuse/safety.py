"""Outbound data-safety gate: nothing an uploader sends leaves unchecked.

The last thing between a payload and the receiver. The engine writes bundles with ``NoRedaction``
by default, ``SensitiveKeyRedaction`` reads key names only, and a planted credential can
legitimately sit in a prompt file; an agent transcript is written on a runner that holds provider
keys. So every payload a producer sends, ingestion events and media alike, is scanned as
**serialised bytes** before the send, fail-closed:

- **known secret values**: every credential the process itself holds (environment variables whose
  name looks like a secret) and the values of the repo-root ``.env`` under secret-like keys;
- **shape patterns**: dotenv lines with secret-like keys, ``Authorization`` headers, PEM blocks,
  URL credentials (a redacted ``***`` password is not a hit), well-known key prefixes
  (``pk-lf-``, ``sk-lf-``, ``sk-or-``, ``sk-ant-``, ``ghp_``, ``github_pat_``, ``xox?-``,
  ``AKIA``, ``AIza``, ``glpat-``), JWTs, and secret-named JSON / YAML fields holding a long
  opaque value;
- **file-type allowlist** for attachments (yaml, json, md, log, txt, and gzip of those).

On a hit the bytes are **never rewritten** (an attachment must stay byte-exact): the caller skips
the file and names it in its receipt, or stops; a hit in the structured events blocks the send
outright. Findings name the rule and a masked excerpt (first four characters, never the tail),
never the value.

Engine-free by construction, so both producers ship the same gate: the live path's automation
uploader and the offline uploader scan with one implementation and one set of fixtures.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE|SIGNING|COOKIE|SESSION)", re.I
)
# names that look secret-like but never hold a secret value
_NOT_SECRET_NAMES = re.compile(r"(PUBLIC_KEY_ID|_FILE$|_PATH$|_DIR$|_URL$|_NAME$|_HEADER$)", re.I)
MIN_SECRET_VALUE = 8

ALLOWED_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".md", ".log", ".txt"})
ALLOWED_COMPRESSED = frozenset({".gz"})

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

    known_values: tuple[bytes, ...] = ()
    # every rule -> how many hits it produced (for the receipt)
    hits: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None, dotenv: Path | None = None
    ) -> SafetyGate:
        """Known secret values from the process environment and the repo-root ``.env``."""
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


def allowed_attachment(name: str) -> bool:
    """The attachment file-type allowlist: yaml, json, md, log, txt, and gzip of those."""
    path = Path(name)
    suffixes = [s.lower() for s in path.suffixes[-2:]]
    if not suffixes:
        return False
    if suffixes[-1] in ALLOWED_COMPRESSED:
        return len(suffixes) == 2 and suffixes[0] in ALLOWED_SUFFIXES
    return suffixes[-1] in ALLOWED_SUFFIXES


def scan_all(gate: SafetyGate, payloads: Iterable[tuple[str, bytes]]) -> dict[str, list[Finding]]:
    """Scan several named payloads; returns the findings per name (clean names absent)."""
    out: dict[str, list[Finding]] = {}
    for name, payload in payloads:
        findings = gate.scan(payload, what=name)
        if findings:
            out[name] = findings
    return out
