"""The outbound data-safety gate: its two views and the exemptions for committed fixture data.

``SafetyGate.scan`` reads raw bytes (an attachment, inline media), ``SafetyGate.scan_structured``
a JSON-like value as it is serialised for the receiver. Two shapes exempt fixture data that is no
credential: a database URL whose password is its user name and whose host can only be local, and
the public ``GPG_KEY`` fingerprint the ``python`` image sets.

Every fake secret is assembled at run time: nothing key-shaped is written in this repository.
"""

from __future__ import annotations

import json
import time

import pytest
import yaml
from tolokaforge_langfuse.safety import SafetyGate

pytestmark = pytest.mark.unit

# the fake secrets this module uses, assembled so no literal is key-shaped
TOKENS = {
    "HEX40": "0123456789ABCDEF" * 2 + "01234567",
    "PG": "postgres" + "ql",
}
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
    """``url-credentials`` passes a DSN whose password is its user name and whose host can only
    be local, and nothing else."""

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

    def test_a_url_past_the_tail_limit_is_no_fixture(self) -> None:
        assert "url-credentials" in rules_in(dsn("app", "app", "db/" + "a" * 2000))

    def test_a_long_line_of_fixture_dsns_scans_fast(self) -> None:
        """Every exempt match reads the URL's tail: without a bound a long line without spaces
        that repeats a fixture DSN would be read again from every one of them."""
        line = dsn("aaa", "aaa", "b/") * 20_000
        started = time.perf_counter()
        rules = rules_in(line)
        assert time.perf_counter() - started < 5.0
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
    """An exemption is visible, per rule, next to the gate's hits: once per scan."""

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


class TestUpperCaseDotenv:
    """``dotenv-secret`` reads the environment-variable convention, upper-case names only."""

    @pytest.mark.parametrize("text", AGENT_CODE.values(), ids=AGENT_CODE.keys())
    def test_a_lower_case_assignment_is_not_a_dotenv_line(self, text: str) -> None:
        assert rules_in(text + "\n") == set()

    def test_agent_source_at_raw_line_starts_is_clean(self) -> None:
        assert rules_in(AGENT_SOURCE + "\n") == set()

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
        listing = "PATH=/usr/bin\nOLDPWD=/root/project-dir"
        assert "dotenv-secret" not in structured_rules_in(listing)
        assert "dotenv-secret" in rules_in(json.dumps({"output": listing}))

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
