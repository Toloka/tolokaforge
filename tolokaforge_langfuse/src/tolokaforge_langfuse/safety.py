"""Outbound data-safety gate: nothing an uploader sends leaves unchecked.

The last thing between a payload and the receiver. The engine writes bundles with ``NoRedaction``
by default, ``SensitiveKeyRedaction`` reads key names only, and a planted credential can
legitimately sit in a prompt file; an agent transcript is written on a runner that holds provider
keys. So every payload a producer sends, ingestion events and media alike, is scanned as
**serialised bytes** before the send, fail-closed:

- **known secret values**: every credential the process itself holds (environment variables whose
  name looks like a secret; the caller may add the values it resolved another way) and, when the
  caller names one, the values of a ``.env`` file under secret-like keys;
- **shape patterns**: upper-case dotenv lines with secret-like names, ``Authorization`` headers,
  PEM blocks, URL credentials (a redacted ``***`` password is not a hit), well-known key
  prefixes (``pk-lf-``, ``sk-lf-``, ``sk-or-``, ``sk-ant-``, ``ghp_``, ``github_pat_``,
  ``xox?-``, ``AKIA``, ``AIza``, ``glpat-``), JWTs, and secret-named JSON / YAML fields holding a
  long opaque value;

Raw bytes (a file, a media object, a span name: :meth:`SafetyGate.scan`) are read with
:data:`RAW_SHAPES`, where a dotenv line may also start after a ``\\n`` / ``\\r`` escape, the way YAML
and JSON write a line break inside a string. A structured value (a span's attributes, a list of
events) is scanned as its JSON with :data:`SHAPES`, and its raw strings are scanned for the known
values too, which JSON escaping hides (:meth:`SafetyGate.scan_structured`). A known value carries
the name of the variable it came from, so a warning can say which one withheld a span
(``Finding.describe``), a run leaves out the values its own tracing values contain
(:meth:`SafetyGate.drop_ambient`), and a gate can be told to re-read its values
(:attr:`SafetyGate.reload`).

Two shapes have a narrow exemption for committed fixture data that is no credential
(:func:`_exempt`): a database URL whose password is its user name and whose host can only be
local, and the public ``GPG_KEY`` fingerprint the ``python`` Docker image sets. The scan goes on
past an exempt match, so it never hides a reportable one, and the gate counts the exemptions per
rule (:attr:`SafetyGate.exempted`).

**What a hit does is the caller's policy** (:meth:`SafetyGate.screen`, the ``secrets`` option):

- ``reject`` (the default): nothing is rewritten. The caller withholds what would have carried the
  hit (a span, a pass, a file) and names it in its receipt, or stops.
- ``mask``: every match is replaced by :data:`PLACEHOLDER` up to the end of its value (a dotenv or
  field value to its closing quote or the end of its line, a PEM block to its END line, a token
  to its end, a URL password to the last ``@`` of the authority), every known value in every form
  JSON gives it, and every copy of a value of :data:`PROMOTE_MIN` or more characters a shape found
  in the same unit (or, passed as ``also``, elsewhere in the same trial). The gate then runs again
  over the result, the byte after each mask must be one its rule ends at, and a file that parsed
  before must still parse; any of these withholds the unit as ``reject`` does. Masking reads more
  than the ``reject`` view of a structured value (a dotenv line inside a string), and it does not
  see what no shape and no known value describes: an encoded copy (base64, hex) of a secret, or a
  credential with no shape next to a masked one, goes out where ``reject`` would have withheld the
  whole unit. A clean re-scan is not proof.

Findings name the rule and a masked excerpt (first four characters, never the tail), never the
value.

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
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote_to_bytes

import yaml

SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE|SIGNING|COOKIE|SESSION)", re.I
)
# names that look secret-like but never hold a secret value; PWD and OLDPWD are the shell's
# working directories, which an agent's own text names all the time, and the tracing launcher's
# session id rides on every span by design. The one filter for every list of credentials: the
# process environment here and the SecretManager's keys in the plugin
_NOT_SECRET_NAMES = re.compile(
    r"(PUBLIC_KEY_ID|_FILE$|_PATH$|_DIR$|_URL$|_BASE$|_NAME$|_HEADER$|^(?:OLD)?PWD$"
    r"|^TOLOKAFORGE_TRACING_SESSION_ID$)",
    re.I,
)
MIN_SECRET_VALUE = 8
# how deep JSON may be nested in a string a value is found in (an attribute holding JSON text
# whose strings hold JSON text)
JSON_NESTING = 3

# the policies a caller passes to SafetyGate.screen
SECRETS_REJECT = "reject"
SECRETS_MASK = "mask"
SECRETS_POLICIES = (SECRETS_REJECT, SECRETS_MASK)
# what a mask writes: valid wherever it lands in YAML or JSON, and too short for any shape
PLACEHOLDER = b"MASKED"
# a value a shape found that is masked wherever else it stands in the unit or the trial: shorter
# ones are mostly fixtures (``postgres``) that would be masked in every sentence
PROMOTE_MIN = 16
RULE_KNOWN = "known-secret-value"
RULE_COPY = "found-value-copy"
RULE_RESIDUE = "mask-residue"
RULE_UNPARSEABLE = "mask-unparseable"

# an environment variable's name, upper case by convention: an agent's `txn_key = compute(x)` is
# code, not a dotenv line
# (each part bounded: a long run of keywords in one word would otherwise backtrack
# quadratically)
_DOTENV_NAME = rb"[A-Z][A-Z0-9_]{0,63}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD)[A-Z0-9_]{0,63}"

SHAPES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "dotenv-secret",
        re.compile(rb"(?m)^\s*(?:export\s+)?" + _DOTENV_NAME + rb"\s*=\s*['\"]?[^\s'\"#]{8,}"),
    ),
    (
        "authorization-header",
        re.compile(rb"(?i)authorization\W{0,3}\s*(?:bearer|basic)\s+[A-Za-z0-9+/=_\-.]{16,}"),
    ),
    ("pem-private-key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # user:password@host with a real password (a redacted *** is the engine's own DSN masking,
    # MASKED this gate's own placeholder)
    (
        # the scheme is anchored and bounded (a free `[a-z][a-z0-9+.-]*` before `://` scans a long
        # lowercase run quadratically); the password and the host end at a `\n`, `\r` or `\t`
        # escape, the way JSON and YAML write a line break (an escaped backslash is no escape):
        # no URL runs on over one. The user part does not, so `CORP\tom` stays a user name
        "url-credentials",
        re.compile(
            rb"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,15}://[^/\s:@]+:(?!(?:[*]+|MASKED)@)"
            rb"(?:(?!(?<!\\)\\[nrt])[^@\s/]){3,}@(?:(?!(?<!\\)\\[nrt])[^\s/])+"
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

# the shapes over raw bytes (a file, a media object, a span name): YAML and JSON write a line
# break inside a double-quoted string as a `\n` or `\r` escape, so there a dotenv line may also
# start after one and its value ends at one. A structured value keeps SHAPES over its JSON: there
# every tool output's environment listing would otherwise read as dotenv lines
RAW_SHAPES: tuple[tuple[str, re.Pattern[bytes]], ...] = tuple(
    (
        (
            rule,
            re.compile(
                rb"(?m)(?:^|(?<=\\n)|(?<=\\r))\s*(?:export\s+)?"
                + _DOTENV_NAME
                + rb"\s*=\s*['\"]?(?:(?!(?<!\\)\\[nr])[^\s'\"#]){8,}"
            ),
        )
        if rule == "dotenv-secret"
        else (rule, pattern)
    )
    for rule, pattern in SHAPES
)

# a url-credentials match that is a committed fixture DSN: the user name as the password and a
# host that can only be local (one letter-first label such as a compose service or `localhost`, or
# a loopback address); a numeric label such as 134744072 is an IPv4 address, so it is not local
_LOCAL_DSN = re.compile(
    rb"[a-z][a-z0-9+.\-]*://(?P<user>[^:]+):(?P<password>[^@]+)@"
    rb"(?:[A-Za-z][A-Za-z0-9_\-]*|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?"
)
# a byte after the host that would make it a longer host, a port or another userinfo
_HOST_CONTINUES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:%[]-@"
)
# a libpq parameter that names the server another way: host, hostaddr, or a service file entry
_SERVER_PARAMETER = re.compile(rb"(?i)(?:host|hostaddr|service)=")
# the rest of a URL after a url-credentials match, which stops at the first `/`: its path and
# query, up to a space, a quote, a backtick, an angle bracket or an escaped line break or quote
# (an escaped backslash is none), read for at most _URL_TAIL_LIMIT bytes: a longer URL is no
# fixture, and the bound keeps a long line of repeated fixture DSNs from a quadratic scan
_URL_TAIL_LIMIT = 1024
_URL_TAIL = re.compile(rb"(?:(?!(?<!\\)\\[nrt\"'])[^\s\"'`<>]){0,%d}" % _URL_TAIL_LIMIT)
# the same in a string of a structured value, whose backslashes are text
_URL_TAIL_TEXT = re.compile(rb"[^\s\"'`<>]{0,%d}" % _URL_TAIL_LIMIT)
# a character a writer encoded as a backslash-u or backslash-x escape: what it stands for cannot
# be read from the bytes
_ENCODED_CHARACTER = re.compile(rb"(?i)\\(?:u[0-9a-f]{4}|x[0-9a-f]{2})")
# a dotenv-secret match that is the public signing-key fingerprint the python image sets
_GPG_FINGERPRINT = re.compile(rb"\s*(?:export\s+)?GPG_KEY\s*=\s*['\"]?[0-9A-Fa-f]{40}")

# what bounds a mask (see _extent)
_SPACE = frozenset(b" \t\r\n\f\v")
_TOKEN_STOP = _SPACE | frozenset(b"\"'`<>\\")
_AUTHORITY_STOP = _SPACE | frozenset(b"/?#\"'`<>")
_PEM_END = re.compile(rb"-----END [A-Z ]*PRIVATE KEY-----")
_DOTENV_EQUALS = re.compile(rb"=[ \t]*")
_FIELD_SEPARATOR = re.compile(rb"[:=][ \t]*")
_AUTH_SCHEME = re.compile(rb"(?i)(?:bearer|basic)\s+")
_FIRST_WORD = re.compile(rb"\S+")
# a string of a structured value holds its backslashes as text, and its JSON writes each one
# escaped: there `\n` in a password is two characters of it, so the mask reads URLs with no
# escape stop too, as the serialised view does
_TEXT_SHAPES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "url-credentials",
        re.compile(
            rb"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,15}://[^/\s:@]+:(?!(?:[*]+|MASKED)@)"
            rb"[^@\s/]{3,}@[^\s/]+"
        ),
    ),
)
# a PEM block is multi-line and never stands bare elsewhere without the BEGIN line its shape finds
_NOT_PROMOTED = frozenset({"pem-private-key"})
# the shapes a string value is also read for with its mapping key in front of it
_KEYED_RULES = frozenset({"secret-field", "authorization-header"})
_EMPTY: Mapping[str, int] = MappingProxyType({})


class SafetyError(RuntimeError):
    """A payload would carry a secret; nothing was sent."""


@dataclass(frozen=True)
class Finding:
    rule: str
    excerpt: str  # masked: at most the first four characters of the match, then ****
    # the variable a known value came from (its name, never a value), for the warning that names
    # what withheld a span; never part of ``str``, which travels to the receiver in a manifest
    source: str = ""

    def __str__(self) -> str:
        return f"{self.rule} ({self.excerpt})"

    def describe(self) -> str:
        """The rule, and the variable a known value came from: ``known-secret-value from X``."""
        return f"{self.rule} from {self.source}" if self.source else self.rule


@dataclass(frozen=True)
class Screened:
    """What :meth:`SafetyGate.screen` decided about one unit (a span, a pass, a file).

    ``value`` is what may be sent: the input itself under ``reject``, the masked copy under
    ``mask``. ``masked`` counts the masks by rule (read-only; empty under ``reject``). Non-empty
    ``findings`` withhold the unit, whatever the policy."""

    value: Any = field(repr=False)
    masked: Mapping[str, int] = _EMPTY
    findings: tuple[Finding, ...] = ()
    # the variables whose known values were masked (names only, for a warning)
    sources: tuple[str, ...] = ()

    @property
    def withheld(self) -> bool:
        return bool(self.findings)


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    rule: str


@dataclass(frozen=True)
class _Known:
    """The known values as one object, so a gate swaps them in a single assignment while other
    threads scan."""

    values: tuple[bytes, ...]
    sources: Mapping[bytes, str]

    @cached_property
    def forms(self) -> tuple[tuple[bytes, tuple[bytes, ...]], ...]:
        """Each value with the forms JSON gives it: escaped up to ``JSON_NESTING`` times, with
        non-ASCII characters raw and as ``\\uXXXX``."""
        return tuple((known, json_forms(known)) for known in self.values if known)


@dataclass
class SafetyGate:
    """Scans serialised payloads; built once per process with the secrets it must never leak."""

    # never in a repr: a traceback or a debugger would print every value the gate protects
    known_values: tuple[bytes, ...] = field(default=(), repr=False)
    # every rule -> how many hits it produced (for the receipt)
    hits: dict[str, int] = field(default_factory=dict)
    # the variable each known value came from, names only (see ``Finding.source``)
    sources: dict[bytes, str] = field(default_factory=dict, repr=False)
    # re-reads the known values: the new gate when what they came from changed (a secret
    # registered after this one was built), else None; see :meth:`refresh`
    reload: Callable[[], SafetyGate | None] | None = field(default=None, repr=False)
    # every rule -> how many scans met a match it exempts (see _exempt)
    exempted: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._known = _Known(self.known_values, self.sources)
        # trials start in parallel: one of them re-reads, the others wait for the new values
        self._refreshing = threading.Lock()
        # trials scan in parallel: the counters are read, then written
        self._counting = threading.Lock()

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv: Path | None = None,
        *,
        extra: Iterable[str] | Mapping[str, str] = (),
    ) -> SafetyGate:
        """Known secret values from the process environment, a ``.env`` file when given, and
        ``extra``: credential values the caller holds by another route (a secret manager, a
        header), as ``{name: value}`` when their names are to be told in a warning."""
        named: dict[bytes, list[str]] = {}

        def add(value: str, name: str) -> None:
            if value and len(value) >= MIN_SECRET_VALUE:
                names = named.setdefault(value.encode("utf-8", "surrogateescape"), [])
                if name and name not in names:
                    names.append(name)

        for name, value in (env if env is not None else os.environ).items():
            if looks_secret(name):
                add(value, name)
        if dotenv is not None and dotenv.exists():
            for line in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                value = value.strip().strip("'\"")
                if looks_secret(name.strip()):
                    add(value, name.strip())
        if isinstance(extra, Mapping):
            for name, value in extra.items():
                add(value, name)
        else:
            for value in extra:
                add(value, "")
        return cls(
            known_values=tuple(sorted(named, key=len, reverse=True)),
            sources={value: ", ".join(names) for value, names in named.items() if names},
        )

    def scan(self, payload: bytes, *, what: str = "payload") -> list[Finding]:
        """Every finding in raw ``payload`` (empty when the bytes are clean), with the raw-bytes
        shapes (:data:`RAW_SHAPES`)."""
        return self._scan_bytes(payload, RAW_SHAPES)

    def _scan_bytes(
        self,
        payload: bytes,
        shapes: Iterable[tuple[str, re.Pattern[bytes]]],
        *,
        count_exempted: bool = True,
    ) -> list[Finding]:
        known = self._known
        findings: list[Finding] = []
        for value in known.values:
            if value and value in payload:
                # no leading characters for a credential the process holds: the head of an
                # arbitrary password is secret material, unlike a provider key prefix
                findings.append(
                    Finding(
                        "known-secret-value",
                        f"**** ({len(value)} chars)",
                        known.sources.get(value, ""),
                    )
                )
        passed: list[str] = []
        for rule, pattern in shapes:
            match, exempt = _first_reportable(rule, pattern, payload)
            if exempt:
                passed.append(rule)
            if match:
                findings.append(Finding(rule, _mask(match.group(0))))
        self._count(findings, passed if count_exempted else ())
        return findings

    def _count(self, findings: Iterable[Finding], exempted: Iterable[str] = ()) -> None:
        with self._counting:
            for finding in findings:
                self.hits[finding.rule] = self.hits.get(finding.rule, 0) + 1
            for rule in exempted:
                self.exempted[rule] = self.exempted.get(rule, 0) + 1

    def scan_structured(self, value: Any, *, what: str = "payload") -> list[Finding]:
        """Every finding in a JSON-like ``value`` (a span's attributes, a list of events).

        The shapes and the known values run over the value serialised as JSON, which is what a
        receiver is sent. The known values also run over every raw string the value holds, in
        the forms JSON gives them: JSON escaping hides a credential with a quote or a backslash,
        and an attribute that is itself JSON text escapes it once more. The shapes do not run
        over raw strings: a line-anchored one would stop ordinary code (``api_key = os.environ...``)
        and is quadratic on a long line. So a credential this gate does not know by value, inside
        JSON text and without a provider shape, passes."""
        return self._scan_structured(value, count_exempted=True)

    def _scan_structured(self, value: Any, *, count_exempted: bool) -> list[Finding]:
        known = self._known
        serialised = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8", "replace")
        found = self._scan_bytes(serialised, SHAPES, count_exempted=count_exempted)
        if not known.values:
            return found
        raw = "\x00".join(strings_in(value)).encode("utf-8", "replace")
        seen = {(finding.rule, finding.excerpt, finding.source) for finding in found}
        extra: list[Finding] = []
        for known_value, forms in known.forms:
            if any(form in raw for form in forms):
                finding = Finding(
                    "known-secret-value",
                    f"**** ({len(known_value)} chars)",
                    known.sources.get(known_value, ""),
                )
                if (finding.rule, finding.excerpt, finding.source) not in seen:
                    seen.add((finding.rule, finding.excerpt, finding.source))
                    extra.append(finding)
        self._count(extra)
        return found + extra

    # -- the policy ------------------------------------------------------------------------------

    def screen(
        self,
        payload: bytes,
        *,
        policy: str,
        also: Iterable[bytes] = (),
    ) -> Screened:
        """Raw ``payload`` under ``policy``: under ``reject`` what :meth:`scan` finds; under
        ``mask`` the masked copy and what the backstop finds (the gate over the copy, and the
        check of what follows each mask). ``also``: values found elsewhere in the same trial,
        masked here too."""
        _check_policy(policy)
        if policy == SECRETS_REJECT:
            return Screened(payload, _EMPTY, tuple(self.scan(payload)))
        promoted = self.found_values(payload) | frozenset(also)
        exempted: set[str] = set()
        masked, counts, broken = self._mask_view(payload, promoted, exempted, context=None)
        self._count((), exempted)
        findings = self._scan_bytes(masked, RAW_SHAPES, count_exempted=False)
        findings += self._broken(broken)
        return Screened(
            masked,
            MappingProxyType(dict(counts)),
            tuple(findings),
            self._sources_in(payload) if RULE_KNOWN in counts else (),
        )

    def screen_structured(self, value: Any, *, policy: str, also: Iterable[bytes] = ()) -> Screened:
        """A JSON-like ``value`` under ``policy``: under ``reject`` what :meth:`scan_structured`
        finds; under ``mask`` a copy whose string values (never the keys) are masked, each also
        read with its mapping key in front of it (``{"password": "..."}``), and what the backstop
        finds over that copy."""
        _check_policy(policy)
        if policy == SECRETS_REJECT:
            return Screened(value, _EMPTY, tuple(self.scan_structured(value)))
        promoted = self.found_values(value) | frozenset(also)
        exempted: set[str] = set()
        counts: Counter[str] = Counter()
        broken: list[str] = []
        masked = self._mask_tree(value, promoted, exempted, counts, broken)
        self._count((), exempted)
        findings = self._scan_structured(masked, count_exempted=False) + self._broken(broken)
        sources = ()
        if RULE_KNOWN in counts:
            sources = self._sources_in("\x00".join(strings_in(value)).encode("utf-8", "replace"))
        return Screened(masked, MappingProxyType(dict(counts)), tuple(findings), sources)

    def _sources_in(self, view: bytes) -> tuple[str, ...]:
        """The variables whose known values stand in ``view``, in any JSON form (names only)."""
        known = self._known
        return tuple(
            dict.fromkeys(
                known.sources.get(value) or "an unnamed value"
                for value, forms in known.forms
                if any(form in view for form in forms)
            )
        )

    def found_values(self, payload: Any) -> frozenset[bytes]:
        """The values of :data:`PROMOTE_MIN` or more characters a shape finds in ``payload`` (raw
        bytes, or a JSON-like value), not exempt ones and no PEM block: what a trial masks
        wherever else it stands."""
        found: set[bytes] = set()
        if isinstance(payload, (bytes, bytearray)):
            view = bytes(payload)
            for span in self._shape_spans(view, set()):
                found |= _promotable(view, span)
            return frozenset(found)
        for context, text in _entries(payload):
            view = text.encode("utf-8", "surrogateescape")
            for span in self._shape_spans(view, set()) + self._shape_spans(
                view, set(), shapes=_TEXT_SHAPES
            ):
                found |= _promotable(view, span)
            if context is not None:
                keyed = _keyed_view(context, view)
                for span in self._shape_spans(keyed, set(), rules=_KEYED_RULES):
                    found |= _promotable(keyed, span)
        return frozenset(found)

    def _mask_tree(
        self,
        value: Any,
        promoted: frozenset[bytes],
        exempted: set[str],
        counts: Counter[str],
        broken: list[str],
        context: str | None = None,
    ) -> Any:
        if isinstance(value, Mapping):
            return {
                key: self._mask_tree(item, promoted, exempted, counts, broken, str(key))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._mask_tree(item, promoted, exempted, counts, broken) for item in value]
        if isinstance(value, tuple):
            return tuple(
                self._mask_tree(item, promoted, exempted, counts, broken) for item in value
            )
        if isinstance(value, str):
            view = value.encode("utf-8", "surrogateescape")
            masked, found, bad = self._mask_view(
                view, promoted, exempted, context=context, text=True
            )
            if not found and not bad:
                return value
            counts.update(found)
            broken.extend(bad)
            return masked.decode("utf-8", "surrogateescape")
        return value

    def _mask_view(
        self,
        view: bytes,
        promoted: frozenset[bytes],
        exempted: set[str],
        *,
        context: str | None,
        text: bool = False,
    ) -> tuple[bytes, Counter[str], list[str]]:
        return _replace(
            view, self._mask_spans(view, promoted, exempted, context=context, text=text)
        )

    def _mask_spans(
        self,
        view: bytes,
        promoted: frozenset[bytes],
        exempted: set[str],
        *,
        context: str | None,
        text: bool = False,
    ) -> list[tuple[int, int, frozenset[str], str]]:
        """Every byte range of ``view`` a mask replaces, overlaps merged: ``(start, end, the rules
        that fired on it, the rule its end comes from)``. ``text``: ``view`` is a string of a
        structured value, whose backslashes are text."""
        spans = self._shape_spans(view, exempted)
        if text:
            spans += self._shape_spans(view, exempted, shapes=_TEXT_SHAPES)
        if context is not None:
            keyed = _keyed_view(context, view)
            shift = len(keyed) - len(view)
            for span in self._shape_spans(keyed, exempted, rules=_KEYED_RULES):
                if span.end > shift:
                    spans.append(_Span(max(span.start - shift, 0), span.end - shift, span.rule))
        for _, forms in self._known.forms:
            spans += _occurrences(view, forms, RULE_KNOWN)
        for value in promoted:
            spans += _occurrences(view, json_forms(value), RULE_COPY)
        return _merge(spans)

    def _shape_spans(
        self,
        view: bytes,
        exempted: set[str],
        *,
        rules: frozenset[str] | None = None,
        shapes: Iterable[tuple[str, re.Pattern[bytes]]] = RAW_SHAPES,
    ) -> list[_Span]:
        spans: list[_Span] = []
        for rule, pattern in shapes:
            if rules is not None and rule not in rules:
                continue
            position = 0
            while (match := pattern.search(view, position)) is not None:
                if _exempt(rule, match, view, text=shapes is _TEXT_SHAPES):
                    exempted.add(rule)
                    position = match.start() + 1
                    continue
                start, end = _extent(rule, match, view)
                spans.append(_Span(start, end, rule))
                # the greedy host of a URL can hold the start of the next one
                resume = end if rule == "url-credentials" else match.end()
                position = max(resume, match.start() + 1)
        return spans

    def _broken(self, rules: Iterable[str]) -> list[Finding]:
        findings = [Finding(RULE_RESIDUE, f"after a {rule} mask") for rule in dict.fromkeys(rules)]
        self._count(findings)
        return findings

    def drop_ambient(self, ambient: Iterable[str]) -> list[str]:
        """Leave out, in place, every known value one of the ``ambient`` strings contains, and
        name the variables they came from (names only). A value the run's own tracing values
        carry (a tag, the session, the label) rides on every span by design, so a gate that
        knew it would withhold them all."""
        known = self._known
        blob = "\x00".join(text for text in ambient if text).encode("utf-8", "replace")
        dropped = {value for value, forms in known.forms if any(form in blob for form in forms)}
        if not dropped:
            return []
        keep = tuple(value for value in known.values if value not in dropped)
        sources = {value: name for value, name in known.sources.items() if value not in dropped}
        names = [
            known.sources.get(value) or "an unnamed value"
            for value in known.values
            if value in dropped
        ]
        self.known_values, self.sources = keep, sources
        self._known = _Known(keep, sources)
        return list(dict.fromkeys(names))

    def refresh(self, ambient: Iterable[str] = ()) -> tuple[bool, list[str]]:
        """Take the new known values :attr:`reload` offers, in place, with the ``ambient`` ones
        left out before any scan can see them: ``(whether it offered any, the variables left
        out)``."""
        if self.reload is None:
            return False, []
        with self._refreshing:
            fresh = self.reload()
            if fresh is None:
                return False, []
            left_out = fresh.drop_ambient(ambient)
            self.known_values, self.sources, self._known = (
                fresh.known_values,
                fresh.sources,
                fresh._known,
            )
            return True, left_out

    def check(self, payload: bytes, *, what: str) -> None:
        """Raise ``SafetyError`` naming the rules when ``payload`` is not clean."""
        findings = self.scan(payload, what=what)
        if findings:
            raise SafetyError(
                f"{what}: would carry a secret: " + ", ".join(str(f) for f in findings)
            )


def looks_secret(name: str) -> bool:
    """Whether a variable or key name holds a credential: secret-like, and not one of the names
    that never do."""
    return bool(SECRET_NAME.search(name)) and not _NOT_SECRET_NAMES.search(name)


def _mask(value: bytes) -> str:
    text = value.decode("utf-8", "replace").strip()
    head = text[:4] if len(text) > 12 else ""
    return f"{head}**** ({len(text)} chars)"


def _first_reportable(
    rule: str, pattern: re.Pattern[bytes], view: bytes
) -> tuple[re.Match[bytes] | None, bool]:
    """The first match of ``rule`` in ``view`` that is not exempt, and whether the search passed
    an exempt one. After an exempt match the search resumes one byte on, not at its end: the
    greedy host part of a URL runs on to the next ``/`` and can hold the start of the next URL."""
    passed = False
    position = 0
    while (match := pattern.search(view, position)) is not None:
        if not _exempt(rule, match, view):
            return match, passed
        passed = True
        position = match.start() + 1
    return None, passed


def _exempt(rule: str, match: re.Match[bytes], view: bytes, *, text: bool = False) -> bool:
    """A match of ``rule`` in ``view`` known to be committed fixture data, not a credential.
    ``text``: ``view`` is a string of a structured value, whose backslashes are text."""
    if rule == "url-credentials":
        tail = (_URL_TAIL_TEXT if text else _URL_TAIL).match(view, match.end())
        return _local_fixture_dsn(match.group(0), tail.group(0) if tail else b"")
    if rule == "dotenv-secret":
        return _GPG_FINGERPRINT.fullmatch(match.group(0)) is not None
    return False


def _local_fixture_dsn(match: bytes, tail: bytes) -> bool:
    """A DSN whose password is its user name and whose URL can reach no host but a local one.

    ``match`` ends at the first ``/``; ``tail`` is the rest of the URL (its path and query). What
    follows the host and port must not lead anywhere else: no ``@`` before the path (a URL parser
    reads everything before the last one as the user info, so the host would be the part after
    it), no ``,`` there but a closing one (a libpq multi-host list), and no libpq ``host``,
    ``hostaddr`` or ``service`` parameter anywhere, percent-encoded or not; a character encoded as
    ``\\u`` or ``\\x`` refuses the exemption too, and so does a URL longer than the tail's limit.
    A comma may close the match because serialised JSON puts ``", "`` after a string value and
    the match runs on to the next space. The URL alone is read: a ``PGHOST``, ``PGHOSTADDR`` or
    ``PGSERVICE`` set elsewhere can still send the pair to another server."""
    parsed = _LOCAL_DSN.match(match)
    if parsed is None or parsed["user"] != parsed["password"]:
        return False
    rest = match[parsed.end() :]
    if rest and rest[0] in _HOST_CONTINUES:
        return False
    if b"@" in rest or b"," in rest[:-1] or len(tail) >= _URL_TAIL_LIMIT:
        return False
    after = rest + tail
    if _ENCODED_CHARACTER.search(after):
        return False
    return not _SERVER_PARAMETER.search(unquote_to_bytes(after))


def strings_in(value: Any) -> Iterator[str]:
    """Every string in ``value``, the keys included, and every other scalar as text."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from strings_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings_in(item)
    elif value is not None:
        yield str(value)


def json_forms(value: bytes) -> tuple[bytes, ...]:
    """``value`` and the forms JSON gives it: escaped up to ``JSON_NESTING`` times, with
    non-ASCII characters raw and as ``\\uXXXX``."""
    base = value.decode("utf-8", "surrogateescape")
    forms = [value]
    for ascii_only in (False, True):
        text = base
        for _ in range(JSON_NESTING):
            text = json.dumps(text, ensure_ascii=ascii_only)[1:-1]
            form = text.encode("utf-8", "surrogateescape")
            if form not in forms:
                forms.append(form)
    return tuple(forms)


def _check_policy(policy: str) -> None:
    if policy not in SECRETS_POLICIES:
        raise ValueError(f"secrets must be one of {', '.join(SECRETS_POLICIES)}, got {policy!r}")


# -- what a mask covers ----------------------------------------------------------------------------


def _extent(rule: str, match: re.Match[bytes], view: bytes) -> tuple[int, int]:
    """The bytes a mask of ``match`` replaces: a dotenv or field value to its closing quote or the
    end of its line, a PEM block to its END line, a header's token or a key to its end, a URL
    password to the last ``@`` of the authority."""
    if rule == "dotenv-secret":
        equals = _DOTENV_EQUALS.search(view, match.start(), match.end())
        return _value_extent(view, equals.end() if equals else match.end())
    if rule == "secret-field":
        separator = _FIELD_SEPARATOR.search(view, match.start(), match.end())
        return _value_extent(view, separator.end() if separator else match.end())
    if rule == "pem-private-key":
        limit = _string_end(view, match.end())
        end = _PEM_END.search(view, match.end(), limit)
        return match.start(), end.end() if end else limit
    if rule == "url-credentials":
        colon = view.index(b":", view.index(b"://", match.start()) + 3)
        last_at = view.rfind(b"@", colon + 1, _authority_end(view, colon + 1))
        # the match's own `@` when an escape ends the authority before it
        return colon + 1, last_at if last_at > colon else view.index(b"@", colon + 1)
    if rule == "authorization-header":
        scheme = _AUTH_SCHEME.search(view, match.start(), match.end())
        return (scheme.end() if scheme else match.start()), _token_end(view, match.end())
    return match.start(), _token_end(view, match.end())


def _value_extent(view: bytes, start: int) -> tuple[int, int]:
    """A dotenv or field value: inside its quotes when it is quoted and the quote closes in the
    same string, else to the end of its line."""
    for quote in (b'\\"', b"\\'", b'"', b"'"):
        if view.startswith(quote, start):
            body = start + len(quote)
            close = _closing_quote(view, body, quote)
            return (body, close) if close != -1 else (body, _line_end(view, body))
    return start, _line_end(view, start)


def _closing_quote(view: bytes, i: int, quote: bytes) -> int:
    """The next quote of the kind that opened a value just before ``i`` (``"``, ``'``, or an
    escaped ``\\"`` / ``\\'``; YAML's ``''`` is no closing quote); -1 when there is none, or,
    for an escaped opener, when the enclosing string ends first."""
    escaped_opener = len(quote) == 2
    target = quote[-1]
    while i < len(view):
        byte = view[i]
        if byte == 0x5C:  # a backslash and what it escapes
            if escaped_opener and view[i + 1] == target:
                return i
            i += 2
            continue
        if escaped_opener and byte == 0x22:
            return -1
        if not escaped_opener and byte == target:
            if target == 0x27 and view[i + 1 : i + 2] == b"'":
                i += 2  # YAML's '' inside a single-quoted scalar
                continue
            return i
        i += 1
    return -1


def _line_end(view: bytes, i: int) -> int:
    """Where an unquoted value starting at ``i`` ends: a real line break (a backslash right
    before it continues the line, the way YAML folds a double-quoted string), an escaped
    ``\\n`` / ``\\r`` / ``\\"`` / ``\\'``, a quote of either kind, or the end of ``view``. An
    escaped backslash belongs to the value."""
    n = len(view)
    while i < n:
        byte = view[i]
        if byte == 0x5C:
            after = view[i + 1] if i + 1 < n else None
            if after == 0x5C:
                i += 2
                continue
            if after in (0x6E, 0x72, 0x22, 0x27):  # n r " '
                return i
            if after in (0x0A, 0x0D):  # a fold: the logical line goes on
                i += 2
                while i < n and view[i] in b" \t\r\n":
                    i += 1
                if view[i : i + 2] == b"\\ ":
                    i += 1
                continue
            i += 1
            continue
        if byte in (0x0A, 0x0D, 0x22, 0x27):
            return i
        i += 1
    return n


def _string_end(view: bytes, i: int) -> int:
    """The first unescaped double quote at or after ``i`` (where an enclosing JSON or YAML
    double-quoted string ends), else the end of ``view``."""
    while i < len(view):
        if view[i] == 0x5C:
            i += 2
            continue
        if view[i] == 0x22:
            return i
        i += 1
    return len(view)


def _token_end(view: bytes, i: int) -> int:
    while i < len(view):
        if view[i : i + 2] == b"\\/":  # JSON may write a / as \/
            i += 2
            continue
        if view[i] in _TOKEN_STOP:
            return i
        i += 1
    return i


def _authority_end(view: bytes, i: int) -> int:
    while i < len(view):
        if view[i] in _AUTHORITY_STOP:
            return i
        if view[i] == 0x5C and view[i + 1 : i + 2] in (b"n", b"r", b"t", b'"', b"'"):
            return i
        i += 1
    return len(view)


def _ends_cleanly(rule: str, view: bytes, start: int, end: int) -> bool:
    """The check of one mask over ``view[start:end]``: what follows it is where its rule's
    extent may end. A value that stopped at a quote, with a value character right after the
    quote, held the quote (``pass'word``): what follows is the rest of it, so the check
    fails and the unit is withheld."""
    if rule in (RULE_KNOWN, RULE_COPY) or end >= len(view):
        return True
    byte = view[end]
    if rule == "url-credentials":
        return byte == 0x40
    if rule == "pem-private-key":
        return view[:end].endswith(b"KEY-----") or byte == 0x22
    if rule in ("dotenv-secret", "secret-field"):
        opener = _opener_before(view, start)
        if opener is not None:  # a quoted value ends at its own closing quote
            return view.startswith(opener, end) and _after_a_closing_quote(view, end + len(opener))
        if byte in b"\r\n" or view[end : end + 2] in (b"\\n", b"\\r"):
            return True
    elif byte in _SPACE or byte in b"`<>":
        return True
    elif byte == 0x5C:
        return view[end + 1 : end + 2] in (b"n", b"r", b"t", b'"', b"'") and (
            view[end + 1] not in b"\"'" or _after_a_value(view, end + 2)
        )
    if view[end : end + 2] in (b'\\"', b"\\'"):
        return _after_a_value(view, end + 2)
    if byte in b"\"'":
        return _after_a_value(view, end + 1)
    return False


def _opener_before(view: bytes, start: int) -> bytes | None:
    """The quote that opened the value at ``start``, if one did."""
    for quote in (b'\\"', b"\\'", b'"', b"'"):
        if start >= len(quote) and view[start - len(quote) : start] == quote:
            return quote
    return None


def _after_a_closing_quote(view: bytes, i: int) -> bool:
    """Whether ``i`` (just past a quoted value's closing quote) is outside the value: what
    :func:`_outside` accepts, or the quote of an enclosing string followed by that. A value
    character (``"pass"word``) or a quote that opens the next part of a concatenated value
    (``'pass'"'"'word'``) is more of the value."""
    if _outside(view, i):
        return True
    if view[i : i + 2] in (b'\\"', b"\\'"):
        return _outside(view, i + 2)
    if view[i : i + 1] in (b'"', b"'"):
        return _outside(view, i + 1)
    return False


def _outside(view: bytes, i: int) -> bool:
    """Whether ``i`` is past any value: the end, whitespace, a separator or a closing bracket,
    or an escaped line break or tab."""
    if i >= len(view):
        return True
    return view[i] in b" \t\r\n,;:)]}" or view[i : i + 2] in (b"\\n", b"\\r", b"\\t")


def _after_a_value(view: bytes, i: int) -> bool:
    """Whether ``i`` (just past a quote a value stopped at) is outside the value: the end,
    whitespace, a separator or a closing bracket, or an escaped line break or quote."""
    return _outside(view, i) or view[i : i + 2] == b'\\"'


def _promotable(view: bytes, span: _Span) -> set[bytes]:
    if span.rule in _NOT_PROMOTED:
        return set()
    value = view[span.start : span.end]
    out = {value} if len(value) >= PROMOTE_MIN else set()
    word = _FIRST_WORD.match(value)
    if word and len(word.group(0)) >= PROMOTE_MIN:
        out.add(word.group(0))
    return out


def _occurrences(view: bytes, forms: Iterable[bytes], rule: str) -> list[_Span]:
    spans = []
    for form in forms:
        position = view.find(form)
        while position != -1:
            spans.append(_Span(position, position + len(form), rule))
            position = view.find(form, position + 1)
    return spans


def _merge(spans: Iterable[_Span]) -> list[tuple[int, int, frozenset[str], str]]:
    """Overlapping spans as one: ``(start, end, every rule that fired, the rule of the end)``."""
    merged: list[list[Any]] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if span.end <= span.start:
            continue
        if merged and span.start < merged[-1][1]:
            last = merged[-1]
            last[2].add(span.rule)
            if span.end > last[1]:
                last[1], last[3] = span.end, span.rule
            continue
        merged.append([span.start, span.end, {span.rule}, span.rule])
    return [(start, end, frozenset(rules), last) for start, end, rules, last in merged]


def _replace(
    view: bytes, merged: Iterable[tuple[Any, ...]]
) -> tuple[bytes, Counter[str], list[str]]:
    out = bytearray()
    counts: Counter[str] = Counter()
    broken: list[str] = []
    cursor = 0
    for start, end, rules, last, *_ in merged:
        out += view[cursor:start] + PLACEHOLDER
        cursor = end
        # a found value at the place a shape found it is that shape's mask, not a copy
        counts.update(rules - {RULE_COPY} if rules - {RULE_COPY, RULE_KNOWN} else rules)
        if not _ends_cleanly(last, view, start, end):
            broken.append(last)
    out += view[cursor:]
    return bytes(out), counts, broken


def _keyed_view(key: str, view: bytes) -> bytes:
    return key.encode("utf-8", "surrogateescape") + b": " + view


def _entries(value: Any, key: str | None = None) -> Iterator[tuple[str | None, str]]:
    """Every string value in ``value`` with the mapping key it stands under (None in a list)."""
    if isinstance(value, Mapping):
        for item_key, item in value.items():
            yield from _entries(item, str(item_key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _entries(item)
    elif isinstance(value, str):
        yield key, value


def parses(payload: bytes, syntax: str) -> bool:
    """Whether ``payload`` parses as ``syntax`` (``"yaml"`` or ``"json"``)."""
    try:
        if syntax == "json":
            json.loads(payload)
        else:
            list(yaml.safe_load_all(payload))
    except Exception:  # noqa: BLE001 - any parser failure means it does not parse
        return False
    return True
