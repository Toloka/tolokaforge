"""The outbound data-safety gate: its two views, the exemptions for committed fixture data, and
the two secrets policies.

``SafetyGate.scan`` reads raw bytes (an attachment, inline media), ``SafetyGate.scan_structured``
a JSON-like value as it is serialised for the receiver. Two shapes exempt fixture data that is no
credential: a database URL to a loopback address, ``db`` or ``postgres`` whose password is its
user name, short and of one character class, and the public ``GPG_KEY`` fingerprint the
``python`` image sets. A dotenv line passes when its name holds no secret (a path, a URL, the
shell's working directories), or when its name is not in upper case and its value is code.
``SafetyGate.screen`` and ``SafetyGate.screen_structured`` apply a policy: ``reject`` reports what
the scan reports, ``mask`` replaces every match with ``MASKED`` and withholds only what is left
over.

Every fake secret is assembled at run time, here or from the ``{TOKEN}`` placeholders of the
shared vector file: nothing key-shaped is written in this repository.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from tolokaforge_langfuse.safety import SafetyGate, strings_in

from tolokaforge_langfuse import safety

pytestmark = pytest.mark.unit

PLACEHOLDER = "MASKED"
VECTORS = Path(__file__).parent / "fixtures" / "mask_vectors.json"
# the file is copied byte for byte wherever another gate is held to the same rows, and each copy
# pins this digest in its own tests; nothing compares the copies across repositories, so a change
# goes into every copy and its pin by hand
VECTORS_SHA256 = "28df8c4f6ee92a97e430636b6a1d8ff79e8d1073f28a0f9cb5bfb78af6b832cc"

# the fake secrets behind the vector file's placeholders, assembled so no literal is key-shaped
TOKENS = {
    "AT_PW": "Pass" + "@" + "word1",
    "EARLY_AT_PW": "P" + "@" + "ssw0rd-2024",
    "GH_PAT": "ghp" + "_" + "FakeTokenForUnitTests" + "0123456789ABCDEF",
    "GLPAT": "glpat" + "-" + "FakeGitlabTokenAbcde",
    "HEX40": "0123456789ABCDEF" * 2 + "01234567",
    "LEAD_AT_PW": "@" + "dmin-2024",
    "PEM_BEGIN": "-----BEGIN " + "RSA PRIVATE KEY" + "-----",
    "PEM_END": "-----END " + "RSA PRIVATE KEY" + "-----",
    "PG": "postgres" + "ql",
    "SK_ANT": "sk-" + "ant-" + "api03-" + "FakeAnthropicKeyForTests" + "0123456789ab",
    "SK_OR": "sk-" + "or-" + "v1-" + "0123456789abcdef" * 4,
    "SLACK": "xox" + "b-" + "000000000000-FakeSlackTok",
    "URL_PW": "Fake" + "S3cret" + "Pw",
}
GH_PAT = TOKENS["GH_PAT"]
PROVIDER_KEY = TOKENS["SK_OR"]
HEX40 = TOKENS["HEX40"]
# the value of a secret line
SECRET_VALUE = "placeholder-" + "value-for-tests"
# what JSON and a YAML double-quoted string write for a line break: two characters, no whitespace
ESCAPED_LF, ESCAPED_CR = "\\" + "n", "\\" + "r"

# text a coding or terminal trial carries and no credential
ORDINARY_TEXT = {
    "a usage counter": "total_tokens = response.usage.total_tokens",
    "an environment read": "api_key = os.environ.get('X')",
    "a column definition": "primary_key = Column(Integer, nullable=False)",
    "an env listing": "PATH=/usr/bin\nGPG_KEY=" + HEX40 + "\nHOME=/root",
}
# lower-case assignments in an agent's own code, which an attachment holds at a raw line start
AGENT_CODE = {
    "an indented key": "    txn_key = compute_key(row)",
    "a hash key": "content_key = hashlib.sha256(x)",
    "a plural": "before_keys = sorted(rows)",
    "an attribute path": "total_tokens = response.usage.total_tokens",
    "a settings attribute": "api_key = self.api_key",
    "an index": "claim_key = row_keys[3]",
}
AGENT_SOURCE = "def settle(rows):\n    for row in rows:\n" + "\n".join(
    "        " + line.strip() for line in AGENT_CODE.values()
)
# an env listing that yaml.safe_dump writes double-quoted (the tab forces it) and folds at its
# width, so that the GPG_KEY line starts a raw line and runs on into the next escaped line break
FOLDED_LISTING = "\n".join(
    [
        "HOSTNAME=c0ffee000001",
        "PYTHON_VERSION=3.12.7",
        "LANG=C.UTF-8",
        "PATH=/usr/local/bin:/usr/sbin:/usr/bin",
        f"GPG_KEY={HEX40}",
        "HOME=/root",
        "TERM=xterm",
        "exit_status:\t0",
    ]
)


def dsn(user: str, password: str, host: str, scheme: str = TOKENS["PG"]) -> str:
    """A URL with credentials, assembled at run time."""
    return scheme + "://" + user + ":" + password + "@" + host


def events_saying(text: str) -> list[dict]:
    return [{"id": "e1", "type": "trace-create", "body": {"id": "t1", "output": text}}]


def rules_in(text: str) -> set[str]:
    """The rules the raw-bytes scan reports."""
    return {f.rule for f in SafetyGate().scan(text.encode())}


def structured_rules_in(text: str) -> set[str]:
    """The rules the structured scan reports for ``text`` as a trace's output."""
    return {f.rule for f in SafetyGate().scan_structured(events_saying(text))}


def escaped(*lines: str, separator: str = ESCAPED_LF) -> str:
    """One YAML double-quoted line holding ``lines`` joined by an escaped line break."""
    return 'content: "' + separator.join(lines) + '"\n'


def web_app(separator: str) -> str:
    """A web app's source as a tool output: a base URL with a port, then a decorator on a later
    line; serialised, the line break is no whitespace, so ``localhost:8000...@app`` once read as
    user info."""
    return 'BASE = "http://localhost:8000"' + separator + '@app.post("/run")\ndef run(): ...'


def assert_gpg_key_starts_a_raw_line(raw: str) -> None:
    lines = raw.splitlines()
    assert any(line.lstrip().startswith(f"GPG_KEY={HEX40}{ESCAPED_LF}") for line in lines), raw


# --- the two narrowed shapes -------------------------------------------------------------------

LOCAL_HOSTS = {
    "a service label with a path": "postgres:5432/appdb",
    "a service label": "db:5432",
    "localhost": "localhost",
    "IPv4 loopback": "127.0.0.1:5432/x",
    "IPv6 loopback": "[::1]:5432",
}
ENDINGS = {
    "end of input": "",
    "a double quote": '"',
    "a single quote": "'",
    "a backtick": "`",
    "a query": "?sslmode=disable",
}
# url-credentials matches that are no fixture: (text, the password a mask must remove)
STILL_FIRES = {
    "password differs": (dsn("app", "not-the-user", "db:5432/x"), "not-the-user"),
    "password differs in case": (dsn("app", "App", "db:5432/x"), "App"),
    "dotted host": (dsn("app", "app", "db.internal.example.com:5432/x"), "app"),
    "placeholder host": (dsn("app", "app", "%s:5432/db"), "app"),
    "private address": (dsn("app", "app", "10.0.0.5:5432/x"), "app"),
    "numeric host": (dsn("app", "app", "134744072:5432/x"), "app"),
    "a second at sign": (dsn("app", "app", "db@evil"), "app@db"),
    "trailing host characters": (dsn("app", "app", "db:5432.evil.com/x"), "app"),
    "loopback prefix": (dsn("app", "app", "127.0.0.1.example.com/x"), "app"),
    "a later at sign": (
        dsn("admin", "admin", "corp!x9@prod.example.com:5432/app"),
        "admin@corp!x9",
    ),
    "multi-host list": (dsn("app", "app", "db,prod.example.com:5432/x"), "app"),
    "host parameter": (dsn("app", "app", "db?host=prod.example.com"), "app"),
    "host parameter, caps": (dsn("app", "app", "db?HOST=prod.example.com"), "app"),
    "hostaddr parameter": (dsn("app", "app", "db?hostaddr=10.1.2.3"), "app"),
    "service parameter": (dsn("app", "app", "db?service=prod"), "app"),
    "host parameter after a path": (
        dsn("admin", "admin", "db:5432/app?host=prod-db.corp.example.com"),
        "admin",
    ),
    "hostaddr parameter after a path": (dsn("app", "app", "db/app?hostaddr=10.1.2.3"), "app"),
    "encoded host name": (dsn("app", "app", "db?h%6Fst=prod.example.com"), "app"),
    "escaped later at sign": (dsn("app", "app", "db\\u0040prod.example.com/x"), "app"),
    "a domain user name": (
        dsn("CORP\\tom", "not-the-user", "prod.example.com/db", scheme="mssql"),
        "not-the-user",
    ),
    # a single label is completed by the reader's resolver: only db and postgres are known local
    "an unknown label": (dsn("replica", "replica", "prod-db:5432/main"), "replica"),
    "another label": (dsn("svcacct", "svcacct", "vaultdb:5432/x"), "svcacct"),
    "a dashed label": (dsn("admin", "admin", "db-prod-01:5432/x"), "admin"),
    # a shared value that is long or mixed may be a real credential
    "a high-entropy shared value": (
        dsn("A7f93kd2Lm0qZxTT", "A7f93kd2Lm0qZxTT", "db/v1", scheme="https"),
        "A7f93kd2Lm0qZxTT",
    ),
    "a high-entropy value on a vault host": (
        dsn("A7f93kd2Lm0qZxTT", "A7f93kd2Lm0qZxTT", "vault/v1/secret/data/prod", scheme="https"),
        "A7f93kd2Lm0qZxTT",
    ),
    "letters and a digit": (dsn("airflowadmin1", "airflowadmin1", "db:5432/x"), "airflowadmin1"),
    "thirteen letters": (dsn("airflowadminx", "airflowadminx", "db:5432/x"), "airflowadminx"),
    # an at sign anywhere in the password
    "an early at sign": (dsn("admin", "P@ssw0rd", "db.example.com:5432/app"), "P@ssw0rd"),
    "a leading at sign": (dsn("admin", "@secret123", "db.example.com:5432/app"), "@secret123"),
}
# a backslash and an n in a URL: in serialised events the backslash is doubled, so no line break
ESCAPED_BACKSLASH_URLS = {
    "a backslash and n in the password": (
        dsn("svc", "C0rp\\new-Pw", "prod.example.com/db"),
        "C0rp\\new-Pw",
    ),
    "a backslash and n before a host parameter": (
        dsn("app", "app", "db/x\\n?host=prod.example.com"),
        "app",
    ),
}


class TestLocalFixtureDsn:
    """``url-credentials`` passes a DSN to a loopback address, ``db`` or ``postgres`` whose
    password is its user name, short and of one character class, and nothing else."""

    @pytest.mark.parametrize("where", ["raw bytes", "a structured value"])
    @pytest.mark.parametrize("ending", ENDINGS.values(), ids=ENDINGS.keys())
    @pytest.mark.parametrize("host", LOCAL_HOSTS.values(), ids=LOCAL_HOSTS.keys())
    def test_is_not_a_credential(self, host: str, ending: str, where: str) -> None:
        text = dsn("app", "app", host) + ending
        if where == "raw bytes":
            assert SafetyGate().scan(text.encode()) == []
        else:
            assert SafetyGate().scan_structured(events_saying(text)) == []

    @pytest.mark.parametrize("host", LOCAL_HOSTS.values(), ids=LOCAL_HOSTS.keys())
    def test_is_not_a_credential_before_another_json_field(self, host: str) -> None:
        """The serialised value puts ``", "`` right after a string, and the shape's host part runs
        on to the next space: the match ends in ``",``."""
        body = {"output": dsn("app", "app", host), "level": "DEFAULT"}
        events = [{"id": "e1", "type": "trace-create", "body": body}]
        assert SafetyGate().scan_structured(events) == []

    @pytest.mark.parametrize(
        "text", [case for case, _ in STILL_FIRES.values()], ids=list(STILL_FIRES)
    )
    def test_still_fires(self, text: str) -> None:
        assert "url-credentials" in rules_in(text)
        assert "url-credentials" in structured_rules_in(text)

    @pytest.mark.parametrize(
        "text",
        [case for case, _ in ESCAPED_BACKSLASH_URLS.values()],
        ids=list(ESCAPED_BACKSLASH_URLS),
    )
    def test_an_escaped_backslash_is_no_line_break(self, text: str) -> None:
        """The serialised value writes a literal backslash as two: neither the URL nor its tail
        ends there, as they do at an escaped line break."""
        assert "url-credentials" in structured_rules_in(text)

    @pytest.mark.parametrize(
        "value", ["abcdefghijkl", "ABCDEFGHIJKL", "123456789012"], ids=["lower", "upper", "digits"]
    )
    def test_a_shared_value_of_twelve_characters_of_one_class_is_a_fixture(
        self, value: str
    ) -> None:
        assert rules_in(dsn(value, value, "db:5432/x")) == set()

    def test_a_url_past_the_tail_limit_is_no_fixture(self) -> None:
        assert "url-credentials" in rules_in(dsn("app", "app", "db/" + "a" * 2000))

    def test_a_long_line_of_fixture_dsns_scans_in_linear_time(self) -> None:
        """Every exempt match reads the URL's tail: without a bound a long line without spaces
        that repeats a fixture DSN would be read again from every one of them. A ratio, not a
        wall-clock bound, so a busy runner cannot flake."""

        def seconds(repeats: int) -> tuple[float, set[str]]:
            line = dsn("aaa", "aaa", "db/") * repeats
            started = time.perf_counter()
            rules = rules_in(line)
            return time.perf_counter() - started, rules

        (small, _), (large, rules) = seconds(5_000), seconds(20_000)
        assert large < 8 * small + 0.05
        assert "url-credentials" in rules


class TestGpgFingerprint:
    """The ``python`` image's public GPG_KEY fingerprint in an env listing is not a secret; the
    exemption is that one name with that one value shape."""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(f"GPG_KEY={HEX40}", id="a bare line"),
            pytest.param(f"PATH=/usr/bin\nGPG_KEY={HEX40}\nHOME=/root\n", id="an env listing"),
            pytest.param(f'export GPG_KEY="{HEX40}"', id="exported and quoted"),
            pytest.param(f"env: |\n  PATH=/usr/bin\n  GPG_KEY={HEX40}\n", id="a YAML block"),
            pytest.param(f"PATH=/usr/bin\n\nGPG_KEY={HEX40}\n", id="after a blank line"),
            pytest.param(
                yaml.safe_dump({"content": ORDINARY_TEXT["an env listing"]}),
                id="as yaml.safe_dump writes a tool output",
            ),
        ],
    )
    def test_is_not_a_credential(self, text: str) -> None:
        assert SafetyGate().scan(text.encode()) == []

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(f"GPG_KEY={HEX40}8", id="41 hex"),
            pytest.param(f"GPG_KEY={HEX40[:-1]}", id="39 hex"),
            pytest.param(f"GPG_KEY={HEX40}x", id="40 hex and a tail"),
            pytest.param("GPG_KEY=" + "ghijklmnopqrstuvwxyz" * 2, id="40 non-hex"),
            pytest.param(f"MY_GPG_KEY={HEX40}", id="a longer name"),
            pytest.param(f"GPG_KEY_PASSWORD={HEX40}", id="a name with a suffix"),
        ],
    )
    def test_still_fires(self, text: str) -> None:
        assert "dotenv-secret" in rules_in(text)


class TestExemptionsHideNothing:
    """The scan goes past an exempt match to the next one, never past a reportable one; an
    overlapping match is found too. Each case first shows that its exempt part alone is clean."""

    def test_a_real_dsn_after_a_fixture_dsn_is_reported(self) -> None:
        fixture = dsn("app", "app", "postgres:5432/appdb")
        real = dsn("app", "not-the-user", "prod.example.com:5432/appdb")
        assert rules_in(fixture) == set()
        assert "url-credentials" in rules_in(f"{fixture}\n{real}\n")
        assert "url-credentials" in structured_rules_in(f"{fixture}\n{real}\n")

    def test_an_overlapping_url_is_reported(self) -> None:
        # the fixture's host part runs on to the next `/`, over the start of the second URL
        head = dsn("app", "app", "db;")
        overlap = head + dsn("root", "pw-not-user", "prod.example.com/x", scheme="mysql")
        assert rules_in(head) == set()
        assert "url-credentials" in rules_in(overlap)

    def test_a_secret_line_after_a_gpg_line_is_reported(self) -> None:
        gpg = f"GPG_KEY={HEX40}\n"
        assert rules_in(gpg) == set()
        assert "dotenv-secret" in rules_in(gpg + f"API_KEY={SECRET_VALUE}\n")


class TestExemptionsAreCounted:
    """An exemption is visible, per rule, next to the gate's hits: once per scan or screen."""

    @staticmethod
    def payload() -> str:
        return "\n".join(
            [
                dsn("app", "app", "postgres:5432/appdb"),
                dsn("root", "root", "localhost:3306/x", scheme="mysql"),
                f"GPG_KEY={HEX40}",
                "",
            ]
        )

    def test_one_scan_counts_each_rule_once(self) -> None:
        gate = SafetyGate()
        assert gate.scan(self.payload().encode()) == []
        assert gate.exempted == {"url-credentials": 1, "dotenv-secret": 1}
        assert gate.hits == {}

    def test_an_exempt_and_a_reportable_match_of_one_rule_count_both(self) -> None:
        gate = SafetyGate()
        fixture = dsn("app", "app", "postgres:5432/appdb")
        real = dsn("app", "not-the-user", "prod.example.com:5432/appdb")
        assert "url-credentials" in {f.rule for f in gate.scan(f"{fixture}\n{real}\n".encode())}
        assert gate.hits == {"url-credentials": 1}
        assert gate.exempted == {"url-credentials": 1}

    def test_a_structured_scan_counts_too(self) -> None:
        gate = SafetyGate()
        assert gate.scan_structured(events_saying(dsn("app", "app", "db:5432/x"))) == []
        assert gate.exempted == {"url-credentials": 1}

    @pytest.mark.parametrize("policy", ["reject", "mask"])
    def test_one_screen_counts_each_rule_once(self, policy: str) -> None:
        """Under mask the backstop scans the masked bytes again; that scan counts nothing."""
        gate = SafetyGate()
        screened = gate.screen(self.payload().encode(), policy=policy)
        assert screened.findings == ()
        assert gate.exempted == {"url-credentials": 1, "dotenv-secret": 1}


class TestDotenvNames:
    """``dotenv-secret`` reads a secret-like name in any case. Under a name not in upper case a
    value that is code (a call, an index, a literal, an attribute path) is no secret; a name that
    holds none (a path, a URL, the shell's working directories) is passed whatever its case."""

    @pytest.mark.parametrize("text", AGENT_CODE.values(), ids=AGENT_CODE.keys())
    def test_a_lower_case_assignment_of_code_is_not_a_dotenv_line(self, text: str) -> None:
        assert rules_in(text + "\n") == set()

    def test_agent_source_at_raw_line_starts_is_clean(self) -> None:
        assert rules_in(AGENT_SOURCE + "\n") == set()

    @pytest.mark.parametrize("text", ORDINARY_TEXT.values(), ids=ORDINARY_TEXT.keys())
    def test_ordinary_text_is_clean(self, text: str) -> None:
        assert rules_in(text + "\n") == set()
        assert structured_rules_in(text) == set()

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(f"vault_token={SECRET_VALUE}", id="lower case"),
            pytest.param(f"gh_token={SECRET_VALUE}", id="a short name"),
            pytest.param(f"npm_token={SECRET_VALUE}", id="another short name"),
            pytest.param(f"signing_key={SECRET_VALUE}", id="a key"),
            pytest.param(f"AWS_secret_access_key={SECRET_VALUE}", id="mixed case"),
            pytest.param("my_password=8charval", id="eight characters"),
            pytest.param("registry_password=Ab3dEf9h", id="eight mixed characters"),
            pytest.param("Api_Key=8charval", id="title case"),
        ],
    )
    def test_a_secret_line_in_any_case_fires(self, text: str) -> None:
        assert "dotenv-secret" in rules_in(text + "\n")

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("API_KEY = compute_key(row)", id="a call"),
            pytest.param("DB_TOKEN=settings.DB_TOKEN", id="an attribute path"),
        ],
    )
    def test_code_under_an_upper_case_name_still_fires(self, text: str) -> None:
        """Only a name not in upper case reads its value as code: an environment variable's value
        can be anything."""
        assert "dotenv-secret" in rules_in(text + "\n")

    @pytest.mark.parametrize(
        "text",
        [
            "OLDPWD=/root/project-dir",
            "SECRET_KEY_FILE=/run/secrets/app_key",
            "API_KEY_PATH=/etc/app/api-key.txt",
            "SERVICE_TOKEN_URL=https://auth.example.com/token",
            "AUTH_TOKEN_BASE=https://auth.example.com/v1",
            "API_KEY_HEADER=X-Api-Key-Header",
            "api_key_path=/etc/app/api-key.txt",
        ],
    )
    def test_a_name_that_holds_no_secret_is_passed(self, text: str) -> None:
        assert rules_in(text + "\n") == set()

    def test_scanning_a_long_run_of_keywords_grows_linearly(self) -> None:
        """Each part of the name is bounded: a long run of keywords in one word would otherwise
        backtrack quadratically. A ratio, not a wall-clock bound, so a busy runner cannot flake."""

        def seconds(n: int) -> float:
            payload = ("KEY" * n).encode()
            started = time.perf_counter()
            SafetyGate().scan(payload)
            return time.perf_counter() - started

        small, large = seconds(4_000), seconds(16_000)
        assert large < 8 * small + 0.05

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(f"DB_PASSWORD={SECRET_VALUE}", id="a bare line"),
            pytest.param(f"export SERVICE_TOKEN={SECRET_VALUE}", id="exported"),
            pytest.param(f"API_KEY = {SECRET_VALUE}", id="spaces around the equals sign"),
            pytest.param(f"    DB_PASSWORD = {SECRET_VALUE}", id="indented"),
        ],
    )
    def test_an_upper_case_secret_line_still_fires(self, text: str) -> None:
        assert "dotenv-secret" in rules_in(text + "\n")


class TestEscapedLineBreaks:
    """In the raw-bytes scan a ``\\n`` / ``\\r`` escape is a line break for ``dotenv-secret``; the
    structured scan is unchanged. ``url-credentials`` ends its password and host at a ``\\n`` /
    ``\\r`` / ``\\t`` escape, in both scans."""

    SEPARATORS = pytest.mark.parametrize(
        "separator", [ESCAPED_LF, ESCAPED_CR + ESCAPED_LF, ESCAPED_CR], ids=["LF", "CRLF", "CR"]
    )

    @SEPARATORS
    def test_an_escaped_env_listing_is_clean(self, separator: str) -> None:
        text = escaped("PATH=/usr/bin", f"GPG_KEY={HEX40}", "HOME=/root", separator=separator)
        assert SafetyGate().scan(text.encode()) == []

    def test_an_env_listing_as_yaml_safe_dump_folds_it_is_clean(self) -> None:
        text = yaml.safe_dump({"content": FOLDED_LISTING})
        assert_gpg_key_starts_a_raw_line(text)
        assert SafetyGate().scan(text.encode()) == []

    @SEPARATORS
    def test_an_escaped_secret_line_after_the_gpg_line_is_a_hit(self, separator: str) -> None:
        lines = ("PATH=/usr/bin", f"GPG_KEY={HEX40}", f"API_KEY={SECRET_VALUE}")
        assert "dotenv-secret" in rules_in(escaped(*lines, separator=separator))

    def test_an_escaped_secret_line_is_a_hit(self) -> None:
        """A new detection: the line was not at a raw line start before."""
        assert "dotenv-secret" in rules_in(escaped("PATH=/usr/bin", f"DB_PASSWORD={SECRET_VALUE}"))

    @pytest.mark.parametrize(
        "listing",
        [ORDINARY_TEXT["an env listing"], FOLDED_LISTING],
        ids=["an env listing", "a long env listing"],
    )
    def test_the_structured_scan_still_passes_an_env_listing(self, listing: str) -> None:
        assert SafetyGate().scan_structured(events_saying(listing)) == []

    def test_only_the_raw_scan_reads_an_escaped_line_break(self) -> None:
        """The structured scan keeps its dotenv shape on purpose: over a serialised value every
        tool output's env listing would otherwise be withheld. The same text as a JSON file is a
        hit, because there the escaped line break starts a line."""
        listing = "PATH=/usr/bin\nDB_PASS" + "WORD=not-a-real-password-1"
        assert "dotenv-secret" not in structured_rules_in(listing)
        assert "dotenv-secret" in rules_in(json.dumps({"output": listing}))

    def test_a_short_value_cut_by_an_escaped_break_is_read_through_it(self) -> None:
        """Fewer than eight characters before the escape: the value is read on through it, as a
        ``.env`` file's own bytes would be, instead of the line passing the scan."""
        assert "dotenv-secret" in rules_in(escaped("A=1", "DB_PASSWORD=Xk3", "Qp9mZt2Lw8Rv"))

    @pytest.mark.parametrize(
        "unit",
        ["\\na_key=f(", "\\nAPI_KEY_FILE=ab"],
        ids=["code under a lower-case name", "a name that holds no secret"],
    )
    def test_reading_through_escaped_breaks_grows_linearly(self, unit: str) -> None:
        """Every line start of the run is read on through its escape, for eight characters only:
        reading to the end of the run from each would be quadratic. Each case is exempt, so the
        scan goes on to the next line start. A ratio, not a wall-clock bound."""

        def seconds(n: int) -> float:
            payload = (unit * n).encode()
            started = time.perf_counter()
            SafetyGate().scan(payload)
            return time.perf_counter() - started

        small, large = seconds(4_000), seconds(16_000)
        assert large < 8 * small + 0.05

    def test_a_url_password_holding_an_escape_still_fires(self) -> None:
        """An escape inside the password is part of it; only one that ends it ends the URL."""
        assert "url-credentials" in rules_in(dsn("u", "pa\\nss", "evilhost/x"))

    @pytest.mark.parametrize("separator", ["\n\n", "\r\n", "\t"], ids=["LF", "CRLF", "tab"])
    @pytest.mark.parametrize("where", ["a structured value", "a JSON file"])
    def test_a_base_url_before_a_decorator_is_no_credential(
        self, separator: str, where: str
    ) -> None:
        text = web_app(separator)
        if where == "a structured value":
            assert "url-credentials" not in structured_rules_in(text)
        else:
            assert "url-credentials" not in rules_in(json.dumps({"output": text}))

    def test_a_fixture_dsn_before_a_decorator_is_no_credential(self) -> None:
        """The host part stops at the escape too, so the later at sign is not inside the URL."""
        text = f'DATABASE_URL = "{dsn("app", "app", "postgres:5432")}"\n\n@app.get("/")'
        assert SafetyGate().scan_structured(events_saying(text)) == []

    @pytest.mark.parametrize(
        "after", ["\nnext line", '\n\n@app.post("/run")'], ids=["a line", "a decorator"]
    )
    @pytest.mark.parametrize("where", ["a structured value", "a JSON file"])
    def test_a_real_dsn_before_a_line_break_still_fires(self, after: str, where: str) -> None:
        text = dsn("app", "not-the-user", "prod.example.com:5432") + after
        if where == "a structured value":
            assert "url-credentials" in structured_rules_in(text)
        else:
            assert "url-credentials" in rules_in(json.dumps({"output": text}))


class TestLongLines:
    def test_a_long_alphanumeric_run_scans_in_linear_time(self) -> None:
        """A line-anchored shape run over a long alphanumeric line backtracks from every keyword
        to the line's end, which is quadratic in the line."""
        gate = SafetyGate()
        started = time.perf_counter()
        gate.scan_structured(events_saying("a" * 2_400_000))
        baseline = time.perf_counter() - started
        started = time.perf_counter()
        gate.scan_structured(events_saying(("a" * 1997 + "KEY") * 1200))
        assert time.perf_counter() - started < 4 * baseline + 1.0

    def test_masking_a_long_line_grows_linearly(self) -> None:
        """Fixture DSNs (each read to its tail) and long runs ending in a keyword on one line. A
        ratio, not a wall-clock bound, so a busy runner cannot flake."""

        def seconds(repeats: int) -> float:
            line = (dsn("aaa", "aaa", "db/") + " " + "a" * 1997 + "KEY ") * repeats
            started = time.perf_counter()
            SafetyGate().screen(line.encode(), policy="mask")
            return time.perf_counter() - started

        small, large = seconds(250), seconds(1000)
        assert large < 8 * small + 0.05


# --- the policies --------------------------------------------------------------------------------


class TestScreen:
    """``screen`` / ``screen_structured``: the policy-free gate applied under a policy, returning
    the value to send, the counts of what was masked and the findings that withhold it."""

    def test_the_policies_are_one_enumeration(self) -> None:
        assert [policy.value for policy in safety.SecretsPolicy] == ["reject", "mask"]
        assert safety.SecretsPolicy("mask") is safety.SecretsPolicy.MASK

    @pytest.mark.parametrize("policy", ["drop", "", "REJECT"])
    def test_an_unknown_policy_is_refused(self, policy: str) -> None:
        with pytest.raises(ValueError, match="SecretsPolicy"):
            SafetyGate().screen(b"x\n", policy=policy)
        with pytest.raises(ValueError, match="SecretsPolicy"):
            SafetyGate().screen_structured({"x": 1}, policy=policy)

    def test_reject_returns_the_input_and_the_scans_findings(self) -> None:
        payload = f"use {PROVIDER_KEY} for the call\n".encode()
        screened = SafetyGate().screen(payload, policy="reject")
        assert screened.value == payload
        assert dict(screened.masked) == {}
        assert [str(f) for f in screened.findings] == [str(f) for f in SafetyGate().scan(payload)]
        assert screened.findings, "a provider key is a finding under reject"

    def test_reject_of_a_structured_value_returns_it_and_the_scans_findings(self) -> None:
        value = events_saying(f"use {PROVIDER_KEY}")
        screened = SafetyGate().screen_structured(value, policy="reject")
        assert screened.value == value
        assert dict(screened.masked) == {}
        assert [str(f) for f in screened.findings] == [
            str(f) for f in SafetyGate().scan_structured(value)
        ]

    def test_mask_returns_a_frozen_result_with_read_only_counts(self) -> None:
        screened = SafetyGate().screen(f"use {PROVIDER_KEY}\n".encode(), policy="mask")
        assert screened.value == b"use MASKED\n"
        assert screened.findings == ()
        with pytest.raises(dataclasses.FrozenInstanceError):
            screened.value = b"other"  # type: ignore[misc]
        with pytest.raises(TypeError):
            screened.masked["openrouter-key"] = 9  # type: ignore[index]

    def test_the_gates_hits_count_findings_only(self) -> None:
        """A masked match is no finding: the gate's hits count what was withheld."""
        gate = SafetyGate()
        gate.screen(f"use {PROVIDER_KEY}\n".encode(), policy="mask")
        assert gate.hits == {}
        gate.screen(f"use {PROVIDER_KEY}\n".encode(), policy="reject")
        assert gate.hits.get("openrouter-key") == 1

    def test_mask_leaves_its_structured_input_alone(self) -> None:
        value = {"body": {"output": f"use {GH_PAT}", "tags": [f"t:{GH_PAT}"]}}
        before = json.dumps(value)
        screened = SafetyGate().screen_structured(value, policy="mask")
        assert json.dumps(value) == before
        assert screened.value == {"body": {"output": "use MASKED", "tags": ["t:MASKED"]}}
        assert dict(screened.masked) == {"github-token": 2}

    def test_mask_rewrites_values_never_keys(self) -> None:
        value = {"cmd": f"echo {GH_PAT}", "nested": [{"note": GH_PAT}]}
        screened = SafetyGate().screen_structured(value, policy="mask")
        assert screened.value == {"cmd": "echo MASKED", "nested": [{"note": "MASKED"}]}
        assert screened.findings == ()

    def test_clean_input_is_returned_as_it_is_under_mask(self) -> None:
        payload = b"messages:\n- role: user\n  content: please book seat 12A\n"
        screened = SafetyGate().screen(payload, policy="mask")
        assert (screened.value, dict(screened.masked), screened.findings) == (payload, {}, ())


class TestFoundValues:
    """``found_values``: what a shape found in a unit that is long enough to be masked across
    the trial."""

    @staticmethod
    def unit() -> str:
        return "\n".join(
            [
                "DB_PASSWORD=Fake8chrTailSecret01",
                "PGPASSWORD=postgres",
                dsn("postgres", "postgres", "db:5432/x"),
                dsn("app", "FakeUrlPassword-0001", "db.example.com/x"),
                f"GPG_KEY={HEX40}",
                TOKENS["PEM_BEGIN"],
                "FAKEbodyLineOneFAKEbodyLineOne",
                TOKENS["PEM_END"],
                f"token {GH_PAT}",
                "",
            ]
        )

    def test_shape_values_of_sixteen_or_more_characters_only(self) -> None:
        found = SafetyGate().found_values(self.unit().encode())
        assert found == frozenset(
            {b"Fake8chrTailSecret01", b"FakeUrlPassword-0001", GH_PAT.encode()}
        )

    def test_a_structured_unit_gives_the_same_values(self) -> None:
        found = SafetyGate().found_values(events_saying(self.unit()))
        assert {b"Fake8chrTailSecret01", GH_PAT.encode()} <= set(found)
        assert b"postgres" not in found and HEX40.encode() not in found

    def test_a_clean_unit_gives_none(self) -> None:
        assert SafetyGate().found_values(b"nothing to see here\n") == frozenset()


# --- the shared vectors ----------------------------------------------------------------------------


def expand(value: Any) -> Any:
    """``value`` with every ``{TOKEN}`` placeholder replaced by its fake secret, keys included."""
    if isinstance(value, str):
        for name, secret in TOKENS.items():
            value = value.replace("{" + name + "}", secret)
        return value
    if isinstance(value, list):
        return [expand(item) for item in value]
    if isinstance(value, dict):
        return {expand(key): expand(item) for key, item in value.items()}
    return value


DOCUMENT = json.loads(VECTORS.read_text(encoding="utf-8"))
ROWS = {row["id"]: row for row in DOCUMENT["rows"]}
EACH_ROW = pytest.mark.parametrize("row", ROWS.values(), ids=ROWS.keys())


def gate_for(row: dict) -> SafetyGate:
    return SafetyGate(known_values=tuple(value.encode() for value in row["known"]))


def screen_row(gate: SafetyGate, row: dict, value: Any, policy: str):
    also = frozenset(item.encode() for item in row["also"])
    if row["kind"] == "bytes":
        return gate.screen(value.encode(), policy=policy, also=also)
    return gate.screen_structured(value, policy=policy, also=also)


def as_text(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


class TestTheVectorFile:
    def test_it_is_the_pinned_file(self) -> None:
        assert hashlib.sha256(VECTORS.read_bytes()).hexdigest() == VECTORS_SHA256

    def test_its_placeholders_are_the_ones_this_module_expands(self) -> None:
        assert sorted(DOCUMENT["tokens"]) == sorted(TOKENS)
        assert DOCUMENT["placeholder"] == PLACEHOLDER
        left = re.compile(r"\{[A-Z0-9_]+\}")
        for row in ROWS.values():
            expanded = json.dumps(expand([row["input"], row["expected"]]))
            assert not left.search(expanded), row["id"]


class TestMask:
    """Under ``mask`` every match is replaced by ``MASKED`` and the unit is sent; a backstop
    withholds it when anything is left over."""

    @EACH_ROW
    def test_a_vector(self, row: dict) -> None:
        """What masking makes of the row; a row with ``withheld`` is withheld with that rule."""
        screened = screen_row(gate_for(row), row, expand(row["input"]), "mask")
        assert as_text(screened.value) == expand(row["expected"])
        assert dict(screened.masked) == row["counts"]
        if "withheld" in row:
            assert row["withheld"] in {f.rule for f in screened.findings}
        else:
            assert screened.findings == ()

    @EACH_ROW
    def test_a_vector_masked_again_is_unchanged(self, row: dict) -> None:
        if "withheld" in row:
            pytest.skip("a withheld unit is never sent, so never masked again")
        gate = gate_for(row)
        once = as_text(screen_row(gate, row, expand(row["input"]), "mask").value)
        again = screen_row(gate, row, once, "mask")
        assert as_text(again.value) == once
        assert again.findings == ()

    @EACH_ROW
    def test_a_vector_under_reject_is_sent_as_it_is_or_withheld(self, row: dict) -> None:
        value = expand(row["input"])
        screened = screen_row(gate_for(row), row, value, "reject")
        assert as_text(screened.value) == value
        assert dict(screened.masked) == {}

    def test_the_yaml_rows_still_parse(self) -> None:
        for name in ("dotenv-yaml-fold", "dotenv-yaml-fold-escaped"):
            row = ROWS[name]
            masked = as_text(screen_row(gate_for(row), row, expand(row["input"]), "mask").value)
            assert yaml.safe_load(masked) == yaml.safe_load(expand(row["expected"])), name

    def test_the_json_rows_still_parse(self) -> None:
        for name in ("dotenv-last-in-json-string", "pem-no-end-in-json", "field-escaped-quote"):
            row = ROWS[name]
            masked = as_text(screen_row(gate_for(row), row, expand(row["input"]), "mask").value)
            assert json.loads(masked) == json.loads(expand(row["expected"])), name

    # -- a URL's user name that is its password

    @pytest.mark.parametrize("where", ["raw bytes", "a structured value"])
    def test_a_user_name_that_is_the_password_is_masked_with_it(self, where: str) -> None:
        """A shared value too short to be masked as a copy elsewhere (fewer than sixteen
        characters) would still show in the user name the mask leaves."""
        text = dsn("airflowadmin1", "airflowadmin1", "db:5432/x")
        if where == "raw bytes":
            screened = SafetyGate().screen(text.encode(), policy="mask")
            sent = screened.value.decode()
        else:
            screened = SafetyGate().screen_structured({"output": text}, policy="mask")
            sent = screened.value["output"]
        assert sent == TOKENS["PG"] + "://MASKED:MASKED@db:5432/x"
        assert dict(screened.masked) == {"url-credentials": 1, "found-value-copy": 1}
        assert screened.findings == ()

    def test_a_user_name_other_than_the_password_is_left(self) -> None:
        screened = SafetyGate().screen(
            dsn("app", "other-pw-1", "db:5432/x").encode(), policy="mask"
        )
        assert screened.value.decode() == TOKENS["PG"] + "://app:MASKED@db:5432/x"

    # -- an escaped line break ends a value only before what reads as the next line

    @pytest.mark.parametrize(
        "payload",
        [
            b'content: "A=1\\nAPI_KEY=abcdefghij\\nNEXT=1"\n',
            b'content: "A=1\\nAPI_KEY=abcdefghij\\n# a comment"\n',
            b'content: "A=1\\nAPI_KEY=abcdefghij\\n"\n',
            b'{"out": "A=1\\nAPI_KEY=abcdefghij\\n\\nexport NEXT=1"}\n',
        ],
        ids=["an assignment", "a comment", "the end of the string", "a blank line and an export"],
    )
    def test_an_escaped_break_before_the_next_line_ends_the_value(self, payload: bytes) -> None:
        screened = SafetyGate().screen(payload, policy="mask")
        assert screened.findings == () and screened.masked
        assert b"abcdefghij" not in screened.value

    @pytest.mark.parametrize(
        "payload",
        [
            b"API_KEY=abcdefgh\\nS3cr3tV4lue9bX7qZ2mNpQ\n",
            b"API_KEY=abcdefgh\\nlower_case_rest=1\n",
        ],
        ids=["more of the value", "no upper-case assignment"],
    )
    def test_residue_an_escaped_break_before_more_of_the_value_withholds_the_unit(
        self, payload: bytes
    ) -> None:
        """In a ``.env`` file's own bytes a backslash and an ``n`` are two characters of the
        value: unless what follows reads as the next line, the mask may have stopped inside it."""
        screened = SafetyGate().screen(payload, policy="mask")
        assert "mask-residue" in {f.rule for f in screened.findings}

    def test_a_closing_quote_search_ending_in_a_lone_backslash_finds_none(self) -> None:
        """An escaped opener's search reads the byte after a backslash: at the end of the view
        there is none."""
        assert safety._closing_quote(b"abc\\", 0, b'\\"') == -1

    # -- residue: what masking cannot remove withholds the unit, as reject does

    def test_residue_a_key_that_carries_a_secret_withholds_the_unit(self) -> None:
        """Keys are never rewritten, so a key holding a token is left for the backstop."""
        screened = SafetyGate().screen_structured({GH_PAT: "x", "ok": "y"}, policy="mask")
        assert "github-token" in {f.rule for f in screened.findings}

    def test_residue_a_known_value_the_masking_produced_withholds_the_unit(self) -> None:
        """The backstop scans the masked bytes for the known values too."""
        gate = SafetyGate(known_values=(b"API_KEY=MASKED",))
        screened = gate.screen(f"API_KEY={SECRET_VALUE}\n".encode(), policy="mask")
        assert "known-secret-value" in {f.rule for f in screened.findings}

    def test_residue_a_masker_that_leaves_the_value_withholds_the_unit(self) -> None:
        gate = _MaskerThat(lambda spans: [])
        screened = gate.screen(f"DB_PASSWORD={SECRET_VALUE}\n".encode(), policy="mask")
        assert "dotenv-secret" in {f.rule for f in screened.findings}

    def test_residue_a_placeholder_with_a_tail_withholds_the_unit(self) -> None:
        """A masker that stops one byte short leaves ``MASKED9``: too short for the dotenv shape,
        so only the check of the byte after each placeholder sees it."""
        payload = b"PGPASSWORD=FakeVal9\nNEXT=1\n"
        assert {f.rule for f in SafetyGate().scan(b"PGPASSWORD=MASKED9\nNEXT=1\n")} == set()
        gate = _MaskerThat(lambda spans: [(start, end - 1, *rest) for start, end, *rest in spans])
        screened = gate.screen(payload, policy="mask")
        assert screened.findings, "a placeholder followed by a byte its rule never ends at"
        assert b"FakeVal9" not in json.dumps([str(f) for f in screened.findings]).encode()

    def test_an_exception_inside_masking_propagates(self) -> None:
        """Every caller treats an exception at its gate as a failed unit: nothing is sent."""

        def broken(spans):
            raise RuntimeError("masker failed")

        with pytest.raises(RuntimeError, match="masker failed"):
            _MaskerThat(broken).screen(f"DB_PASSWORD={SECRET_VALUE}\n".encode(), policy="mask")

    def test_findings_name_no_value(self) -> None:
        screened = SafetyGate().screen_structured({GH_PAT: "x"}, policy="mask")
        text = " ".join(str(f) for f in screened.findings)
        assert GH_PAT not in text and GH_PAT[8:] not in text

    # -- a value that holds a quote: its part after the quote is no part of any mask

    @pytest.mark.parametrize(
        "payload",
        [
            b"DB_PASSWORD=abcdefgh'ijklmnop\n",
            b'DB_PASSWORD=abcdefgh"ijklmnop\n',
            b"password: abcdefghijklmnop'qrstuv\n",
            b'DB_PASSWORD="abcdefgh"ijklmnop\n',
            b"DB_PASSWORD='abcdefgh'ijklmnop\n",
            b"export DB_PASSWORD='p4ssw0rd'\"'\"'tail-1234'\n",
            b"content: \"A=1\\nDB_PASSWORD='p4ssw0rd'\\\"'\\\"'tail-1234'\"\n",
        ],
        ids=[
            "dotenv apostrophe",
            "dotenv double quote",
            "field apostrophe",
            "text after a closing double quote",
            "text after a closing apostrophe",
            "a shell-concatenated value",
            "a shell-concatenated value in a YAML string",
        ],
    )
    def test_residue_a_value_that_goes_on_past_a_quote_withholds_raw_bytes(
        self, payload: bytes
    ) -> None:
        screened = SafetyGate().screen(payload, policy="mask")
        assert "mask-residue" in {f.rule for f in screened.findings}

    @pytest.mark.parametrize(
        "text",
        ["DB_PASSWORD='abcdefgh'ijklmnop\n", "export DB_PASSWORD='p4ssw0rd'\"'\"'tail-1234'\n"],
        ids=["text after the closing quote", "a shell-concatenated value"],
    )
    def test_residue_a_quoted_value_that_goes_on_withholds_the_unit(self, text: str) -> None:
        screened = SafetyGate().screen_structured({"output": text}, policy="mask")
        assert "mask-residue" in {f.rule for f in screened.findings}

    @pytest.mark.parametrize(
        "payload",
        [
            b"DB_PASSWORD='abcdefghij'\n",
            b'DB_PASSWORD="abcdefghij" # a comment\n',
            b'{"content": "A=1\\nDB_PASSWORD=\'abcdefghij\'\\nNEXT=1"}\n',
            b"content: \"A=1\\nDB_PASSWORD='abcdefghij'\"\n",
        ],
        ids=["own line", "a comment after it", "inside a JSON string", "last in a YAML string"],
    )
    def test_a_quoted_value_that_ends_at_its_quote_is_masked_cleanly(self, payload: bytes) -> None:
        screened = SafetyGate().screen(payload, policy="mask")
        assert screened.findings == () and screened.masked
        assert b"abcdefghij" not in screened.value

    @pytest.mark.parametrize("quote", ["'", '"'], ids=["apostrophe", "double quote"])
    def test_residue_a_quote_inside_a_field_value_withholds_the_unit(self, quote: str) -> None:
        screened = SafetyGate().screen_structured(
            {"password": "abcdefghijklmnop" + quote + "qrst"}, policy="mask"
        )
        assert "mask-residue" in {f.rule for f in screened.findings}

    def test_an_env_listing_yaml_ends_with_its_closing_quote_is_masked_cleanly(self) -> None:
        """yaml.safe_dump closes a single-quoted scalar right after the last value: that quote
        ends the value, the file still parses."""
        payload = yaml.safe_dump({"content": "A=1\nPGPASSWORD=postgres-fake-01"}).encode()
        screened = SafetyGate().screen(payload, policy="mask")
        assert screened.findings == ()
        assert yaml.safe_load(screened.value) == {"content": "A=1\nPGPASSWORD=MASKED"}

    def test_masking_a_long_run_of_keywords_grows_linearly(self) -> None:
        """A ratio, not a wall-clock bound, so a busy runner cannot flake."""

        def seconds(n: int) -> float:
            started = time.perf_counter()
            SafetyGate().screen_structured({"output": "KEY" * n}, policy="mask")
            SafetyGate().screen(("KEY" * n).encode(), policy="mask")
            return time.perf_counter() - started

        small, large = seconds(5_000), seconds(20_000)
        assert large < 8 * small + 0.05


class _MaskerThat(SafetyGate):
    """A gate whose masker is changed by ``adjust``. The seam is the masker's span step,
    ``SafetyGate._mask_spans``, which returns ``(start, end, rule, ...)`` for every byte range the
    masker replaces; ``adjust`` receives that list and returns the one the masker uses."""

    def __init__(self, adjust) -> None:
        super().__init__()
        self.adjust = adjust

    def _mask_spans(self, *args, **kwargs):
        return self.adjust(list(super()._mask_spans(*args, **kwargs)))


# --- invariants over the cases this module already holds ------------------------------------------


def _positive_cases() -> dict[str, tuple[str, str]]:
    """Every case a shape reports, with the secret a mask must remove: ``{id: (text, secret)}``."""
    cases: dict[str, tuple[str, str]] = {f"url: {name}": case for name, case in STILL_FIRES.items()}
    gpg = {
        "41 hex": (f"GPG_KEY={HEX40}8", f"{HEX40}8"),
        "39 hex": (f"GPG_KEY={HEX40[:-1]}", HEX40[:-1]),
        "40 hex and a tail": (f"GPG_KEY={HEX40}x", f"{HEX40}x"),
        "40 non-hex": ("GPG_KEY=" + "ghijklmnopqrstuvwxyz" * 2, "ghijklmnopqrstuvwxyz" * 2),
        "a longer name": (f"MY_GPG_KEY={HEX40}", HEX40),
        "a name with a suffix": (f"GPG_KEY_PASSWORD={HEX40}", HEX40),
    }
    cases.update({f"gpg: {name}": case for name, case in gpg.items()})
    dotenv = {
        "a bare line": f"DB_PASSWORD={SECRET_VALUE}",
        "exported": f"export SERVICE_TOKEN={SECRET_VALUE}",
        "spaces around the equals sign": f"API_KEY = {SECRET_VALUE}",
        "indented": f"    DB_PASSWORD = {SECRET_VALUE}",
        "escaped after a gpg line": escaped(
            "PATH=/usr/bin", f"GPG_KEY={HEX40}", f"API_KEY={SECRET_VALUE}"
        ),
        "escaped": escaped("PATH=/usr/bin", f"DB_PASSWORD={SECRET_VALUE}"),
    }
    cases.update({f"dotenv: {name}": (text + "\n", SECRET_VALUE) for name, text in dotenv.items()})
    shapes = {
        "openrouter": (f"OPENROUTER_API_KEY={PROVIDER_KEY}", PROVIDER_KEY),
        "authorization": (
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
            "abcdefghijklmnopqrstuvwxyz0123",
        ),
        "pem without an end": (TOKENS["PEM_BEGIN"] + "\nFAKEbodyLineOne", "FAKEbodyLineOne"),
        "url": (dsn("app", "hunter22", "db:5432/x"), "hunter22"),
        "langfuse": (
            "pk-" + "lf-12345678-1234-1234-1234-123456789abc",
            "pk-" + "lf-12345678-1234-1234-1234-123456789abc",
        ),
        "github": (GH_PAT, GH_PAT),
        "anthropic": (TOKENS["SK_ANT"], TOKENS["SK_ANT"]),
        "slack": (TOKENS["SLACK"], TOKENS["SLACK"]),
        "gitlab": (TOKENS["GLPAT"], TOKENS["GLPAT"]),
    }
    cases.update({f"shape: {name}": case for name, case in shapes.items()})
    return cases


POSITIVE_CASES = _positive_cases()


def tail_runs(secret: str) -> set[str]:
    """Every run of four characters of the secret past its first four (the part an excerpt never
    shows)."""
    tail = secret[4:]
    return {tail[i : i + 4] for i in range(len(tail) - 3)}


def false_positive_texts(*, structured: bool = False) -> dict[str, str]:
    """The false-positive sets as raw bytes hold them, or (``structured``) as a string of a
    structured value holds them: there the web app is the tool output itself, since its JSON text
    as a string is serialised with doubled backslashes, which no longer read as line breaks."""
    texts = {f"ordinary: {name}": text for name, text in ORDINARY_TEXT.items()}
    texts.update({f"agent code: {name}": text + "\n" for name, text in AGENT_CODE.items()})
    texts["agent source"] = AGENT_SOURCE + "\n"
    for host_name, host in LOCAL_HOSTS.items():
        for ending_name, ending in ENDINGS.items():
            texts[f"fixture dsn: {host_name}, {ending_name}"] = dsn("app", "app", host) + ending
    texts["gpg: escaped listing"] = escaped("PATH=/usr/bin", f"GPG_KEY={HEX40}", "HOME=/root")
    texts["gpg: folded listing"] = yaml.safe_dump({"content": FOLDED_LISTING})
    for name, separator in (("LF", "\n\n"), ("CRLF", "\r\n"), ("tab", "\t")):
        app = web_app(separator)
        texts[f"web app: {name}"] = app if structured else json.dumps({"output": app})
    return texts


FALSE_POSITIVES = false_positive_texts()
STRUCTURED_FALSE_POSITIVES = false_positive_texts(structured=True)


class TestMaskInvariants:
    """Over every positive case the module holds: the backstop of the masked unit is clean, no
    run of four characters of the secret's tail survives, and masking again changes nothing.
    Every false-positive set stays as it is under mask."""

    @pytest.mark.parametrize(("text", "secret"), POSITIVE_CASES.values(), ids=list(POSITIVE_CASES))
    def test_a_positive_case_is_masked_cleanly(self, text: str, secret: str) -> None:
        gate = SafetyGate()
        screened = gate.screen(text.encode(), policy="mask")
        assert screened.findings == ()
        assert dict(screened.masked), "something was masked"
        masked = screened.value.decode()
        assert PLACEHOLDER in masked
        assert not [run for run in tail_runs(secret) if run in masked], masked
        assert gate.screen(screened.value, policy="mask").value == screened.value

    @pytest.mark.parametrize(
        ("text", "secret"),
        [*STILL_FIRES.values(), *ESCAPED_BACKSLASH_URLS.values()],
        ids=[*STILL_FIRES, *ESCAPED_BACKSLASH_URLS],
    )
    def test_a_url_in_a_structured_value_is_masked_cleanly(self, text: str, secret: str) -> None:
        gate = SafetyGate()
        screened = gate.screen_structured(events_saying(text), policy="mask")
        assert screened.findings == ()
        masked = "\n".join(strings_in(screened.value))
        assert PLACEHOLDER in masked
        assert not [run for run in tail_runs(secret) if run in masked], masked
        assert gate.screen_structured(screened.value, policy="mask").value == screened.value

    @pytest.mark.parametrize("text", FALSE_POSITIVES.values(), ids=list(FALSE_POSITIVES))
    def test_a_false_positive_stays_as_it_is_in_raw_bytes(self, text: str) -> None:
        screened = SafetyGate().screen(text.encode(), policy="mask")
        assert (screened.value.decode(), dict(screened.masked), screened.findings) == (
            text,
            {},
            (),
        )

    @pytest.mark.parametrize(
        "text", STRUCTURED_FALSE_POSITIVES.values(), ids=list(STRUCTURED_FALSE_POSITIVES)
    )
    def test_a_false_positive_stays_as_it_is_in_a_structured_value(self, text: str) -> None:
        value = events_saying(text)
        screened = SafetyGate().screen_structured(value, policy="mask")
        assert (screened.value, dict(screened.masked), screened.findings) == (value, {}, ())
