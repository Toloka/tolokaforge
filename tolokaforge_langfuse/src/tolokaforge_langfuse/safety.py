"""Outbound data-safety gate: nothing an uploader sends leaves unchecked.

The last thing between a payload and the receiver. The engine writes bundles with ``NoRedaction``
by default, ``SensitiveKeyRedaction`` reads key names only, and a planted credential can
legitimately sit in a prompt file; an agent transcript is written on a runner that holds provider
keys. So every payload a producer sends, ingestion events and media alike, is scanned as
**serialised bytes** before the send, fail-closed:

- **known secret values**: every credential the process itself holds (environment variables whose
  name looks like a secret; the caller may add the values it resolved another way) and, when the
  caller names one, the values of a ``.env`` file under secret-like keys;
- **shape patterns**: dotenv lines with secret-like names in any case, ``Authorization``
  headers, PEM blocks, URL credentials (a redacted ``***`` password is not a hit), well-known key
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
(:func:`_exempt`): a database URL to a loopback address or a known compose service (``db``,
``postgres``) whose password is its user name, short and of one character class, and the public
``GPG_KEY`` fingerprint the ``python`` Docker image sets. A dotenv line passes when its name holds
no secret (a path, a URL, the shell's working directories), or when its name is not in upper case
and its value is code (``txn_key = compute(x)``). The scan goes on past an exempt match, so it
never hides a reportable one, and the gate counts the exemptions per rule
(:attr:`SafetyGate.exempted`).

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
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes

SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE|SIGNING|COOKIE|SESSION)", re.I
)
# names that look secret-like but never hold a secret value; PWD and OLDPWD are the shell's
# working directories, which an agent's own text names all the time, and the tracing launcher's
# session id rides on every span by design. The one filter for every list of credentials (the
# process environment here, the SecretManager's keys in the plugin) and for the dotenv shape's names
_NOT_SECRET_NAMES = re.compile(
    r"(PUBLIC_KEY_ID|_FILE$|_PATH$|_DIR$|_URL$|_BASE$|_NAME$|_HEADER$|^(?:OLD)?PWD$"
    r"|^TOLOKAFORGE_TRACING_SESSION_ID$)",
    re.I,
)
MIN_SECRET_VALUE = 8
# how deep JSON may be nested in a string a value is found in (an attribute holding JSON text
# whose strings hold JSON text)
JSON_NESTING = 3

# an environment variable's name in any case (an agent's `txn_key = compute(x)` is told apart by
# its value, see _exempt); each part is bounded, since a long run of keywords in one word would
# otherwise backtrack quadratically
_DOTENV_NAME = (
    rb"[A-Za-z][A-Za-z0-9_]{0,63}(?i:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD)[A-Za-z0-9_]{0,63}"
)

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
    # user:password@host with a real password (a redacted *** is the engine's own DSN masking)
    (
        # the scheme is anchored and bounded (a free `[a-z][a-z0-9+.-]*` before `://` scans a long
        # lowercase run quadratically); the password and the host end at a `\n`, `\r` or `\t`
        # escape, the way JSON and YAML write a line break (an escaped backslash is no escape):
        # no URL runs on over one. The user part does not, so `CORP\tom` stays a user name
        "url-credentials",
        re.compile(
            rb"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,15}://[^/\s:@]+:(?![*]+@)"
            rb"(?:(?<!\\)\\[nrt](?=[^@\s/])|(?!(?<!\\)\\[nrt])[^\s/]){3,}"
            rb"@(?:(?!(?<!\\)\\[nrt])[^\s/])+"
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
# start after one and its value ends at one; a value shorter than eight characters before one is
# read on through it, as a `.env` file's own bytes would be. A structured value keeps SHAPES over
# its JSON: there every tool output's environment listing would otherwise read as dotenv lines
RAW_SHAPES: tuple[tuple[str, re.Pattern[bytes]], ...] = tuple(
    (
        (
            rule,
            re.compile(
                rb"(?m)(?:^|(?<=\\n)|(?<=\\r))\s*(?:export\s+)?"
                + _DOTENV_NAME
                + rb"\s*=\s*['\"]?(?:(?:(?!(?<!\\)\\[nr])[^\s'\"#]){8,}|[^\s'\"#]{8,})"
            ),
        )
        if rule == "dotenv-secret"
        else (rule, pattern)
    )
    for rule, pattern in SHAPES
)

# a url-credentials match that is a committed fixture DSN: the user name as the password, a host
# that is a loopback address or a compose service the task sets name (a single label is completed
# by the reader's resolver, so no other label is known to be local), and a password of at most
# twelve characters of one class (a long or mixed shared value may be a real credential)
_LOCAL_DSN = re.compile(
    rb"[a-z][a-z0-9+.\-]*://(?P<user>[^:]+):(?P<password>[^@]+)@"
    rb"(?:localhost|db|postgres|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?"
)
_FIXTURE_PASSWORD = re.compile(rb"[a-z]{1,12}|[A-Z]{1,12}|[0-9]{1,12}")
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
# a character a writer encoded as a backslash-u or backslash-x escape: what it stands for cannot
# be read from the bytes
_ENCODED_CHARACTER = re.compile(rb"(?i)\\(?:u[0-9a-f]{4}|x[0-9a-f]{2})")
# a dotenv-secret match split into its name and its value
_ASSIGNMENT = re.compile(
    rb"\s*(?:export\s+)?(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*=\s*['\"]?(?P<value>.*)", re.S
)
# a value that is code, not a secret: it holds a call, an index or a literal, or it is an
# attribute path (`response.usage.total_tokens`, `settings.SECRET_KEY`)
_CODE_VALUE = re.compile(
    rb"[^\s'\"#]*[()\[\]{},]|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+(?![^\s'\"#])"
)
# a dotenv-secret match that is the public signing-key fingerprint the python image sets
_GPG_FINGERPRINT = re.compile(rb"\s*(?:export\s+)?GPG_KEY\s*=\s*['\"]?[0-9A-Fa-f]{40}")


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
class _Known:
    """The known values as one object, so a gate swaps them in a single assignment while other
    threads scan."""

    values: tuple[bytes, ...]
    sources: Mapping[bytes, str]

    @cached_property
    def forms(self) -> tuple[tuple[bytes, tuple[bytes, ...]], ...]:
        """Each value with the forms JSON gives it: escaped up to ``JSON_NESTING`` times, with
        non-ASCII characters raw and as ``\\uXXXX``."""
        pairs = []
        for known in self.values:
            if not known:
                continue
            base = known.decode("utf-8", "surrogateescape")
            forms = [known]
            for ascii_only in (False, True):
                text = base
                for _ in range(JSON_NESTING):
                    text = json.dumps(text, ensure_ascii=ascii_only)[1:-1]
                    form = text.encode("utf-8", "surrogateescape")
                    if form not in forms:
                        forms.append(form)
            pairs.append((known, tuple(forms)))
        return tuple(pairs)


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


def _exempt(rule: str, match: re.Match[bytes], view: bytes) -> bool:
    """A match of ``rule`` in ``view`` known to be committed fixture data, not a credential."""
    if rule == "url-credentials":
        tail = _URL_TAIL.match(view, match.end())
        return _local_fixture_dsn(match.group(0), tail.group(0) if tail else b"")
    if rule == "dotenv-secret":
        return _GPG_FINGERPRINT.fullmatch(match.group(0)) is not None or _names_no_secret(
            match.group(0)
        )
    return False


def _names_no_secret(match: bytes) -> bool:
    """A dotenv-secret match that names no secret: a name the gate knows holds none (a path, a URL,
    the shell's working directories), or a name not in upper case whose value is code (a call, an
    index, a literal or an attribute path: ``txn_key = compute(x)``)."""
    parsed = _ASSIGNMENT.match(match)
    if parsed is None:
        return False
    name = parsed["name"].decode("ascii")
    if _NOT_SECRET_NAMES.search(name):
        return True
    return name != name.upper() and _CODE_VALUE.match(parsed["value"]) is not None


def _local_fixture_dsn(match: bytes, tail: bytes) -> bool:
    """A DSN to a loopback address or a known compose service whose password is its user name,
    short and of one character class, and whose URL can reach no other host.

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
    if not _FIXTURE_PASSWORD.fullmatch(parsed["password"]):
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
