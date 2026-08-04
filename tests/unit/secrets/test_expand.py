"""``${secret:NAME}`` expansion: the sanctioned way for a non-secret string to
carry a secret value.

The reference form is typed rather than a bare ``$NAME`` so it cannot collide with
a literal dollar sign in real credential text (argon2 and bcrypt hashes, generated
passwords). Those collision cases are pinned below, because they are the reason
this syntax was chosen over the shell-familiar one.
"""

from __future__ import annotations

import pytest

from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets.expand import UnresolvedReferenceError, expand_secret_refs

pytestmark = pytest.mark.unit


def _expand(value: str, **secrets: str) -> str:
    return expand_secret_refs(value, SecretManager([DictProvider(secrets)]), where="TEST_VALUE")


class TestResolution:
    def test_a_reference_is_replaced_by_its_value(self) -> None:
        assert _expand("${secret:ORDER_ID}", ORDER_ID="9000123") == "9000123"

    def test_it_composes_with_surrounding_text(self) -> None:
        assert _expand("run-${secret:RUN_ID}-end", RUN_ID="42") == "run-42-end"

    def test_several_references_in_one_value(self) -> None:
        assert _expand("${secret:L}/${secret:R}", L="a", R="b") == "a/b"

    def test_a_value_with_no_reference_is_returned_unchanged(self) -> None:
        assert _expand("research") == "research"


class TestLiteralDollarsSurvive:
    """The whole point of the typed form: real credentials keep their dollars.

    A bare ``$NAME`` syntax would have matched inside every one of these, either
    erroring on a valid credential or silently corrupting it.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "Pa$$w0rd",
            "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ",
            "$2b$12$KIXQ4Ea1eKu0Xr9wCkFmXe",
            "pa$word9",
            "cost$100",
            "$",
            "${notsecret:X}",
        ],
    )
    def test_untouched(self, value: str) -> None:
        assert _expand(value) == value

    def test_the_old_bare_form_is_now_literal_text(self) -> None:
        """Pins the syntax change: ``$NAME`` is no longer special."""
        assert _expand("$ORDER_ID", ORDER_ID="9000123") == "$ORDER_ID"


class TestFailsLoud:
    """Never an empty substitution, and never a reference left on the wire."""

    @pytest.mark.parametrize(
        ("secrets", "case"),
        [({}, "not set at all"), ({"ORDER_ID": "   "}, "set but blank")],
    )
    def test_an_unresolved_name_raises(self, secrets: dict[str, str], case: str) -> None:
        with pytest.raises(UnresolvedReferenceError) as excinfo:
            _expand("${secret:ORDER_ID}", **secrets)
        message = str(excinfo.value)
        # Both the location and the name: "something is unset" is not actionable.
        assert "TEST_VALUE" in message, case
        assert "ORDER_ID" in message, case
        assert excinfo.value.names == ("ORDER_ID",), case
        # The negation, pinned: an earlier revision said "which IS set" and then told
        # the operator to set it, which is the opposite of the truth in the one message
        # this whole feature depends on being clear.
        assert "which is not set" in message, case

    def test_every_missing_name_is_reported_at_once(self) -> None:
        with pytest.raises(UnresolvedReferenceError) as excinfo:
            _expand("${secret:A}/${secret:B}")
        assert excinfo.value.names == ("A", "B")
        assert "none of which are set" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("value", "case"),
        [
            ("${secret:ORDER_ID", "unclosed brace"),
            ("${secret:}", "empty name"),
            ("${secret:bad-name}", "invalid character in the name"),
            ("${secret}", "no name at all"),
            ("prefix ${secret:X", "unclosed after valid text"),
        ],
    )
    def test_a_malformed_reference_raises_rather_than_passing_through(
        self, value: str, case: str
    ) -> None:
        """A malformed reference used to survive as literal text.

        That put ``${secret:...}`` on the wire, where a gateway either bills the
        literal string as an account id or rejects the request in a way that reads
        as network trouble.
        """
        with pytest.raises(UnresolvedReferenceError, match="malformed"):
            _expand(value, ORDER_ID="9000123", X="v")

    @pytest.mark.parametrize("value", ["${SECRET:X}", "${Secret:X}"])
    def test_a_case_variant_is_treated_as_a_typo_and_refused(self, value: str) -> None:
        """Letting it through would put the literal text on the wire."""
        with pytest.raises(UnresolvedReferenceError, match="malformed"):
            _expand(value, X="v")

    @pytest.mark.parametrize("value", ["${notsecret:X}", "${secrets:X}"])
    def test_a_different_word_is_not_a_typo_of_this_syntax(self, value: str) -> None:
        """The word boundary keeps unrelated ``${...}`` text passing through."""
        assert _expand(value, X="v") == value

    def test_a_resolved_value_is_not_rescanned(self) -> None:
        """Expansion is single-level, and a nested reference is refused.

        Not silently passed through: if a referenced value itself contains a
        reference, that is a configuration mistake worth surfacing, and refusing
        keeps the "no reference reaches the wire" guarantee absolute.
        """
        with pytest.raises(UnresolvedReferenceError, match="malformed"):
            _expand("${secret:OUTER}", OUTER="${secret:INNER}", INNER="v")


class TestWhereIsReportedVerbatim:
    def test_the_caller_supplied_location_names_the_offending_value(self) -> None:
        secrets = SecretManager([DictProvider({})])
        with pytest.raises(UnresolvedReferenceError) as excinfo:
            expand_secret_refs(
                "${secret:X}", secrets, where="LLM_PROXY_HEADERS value for 'X-Order-Id'"
            )
        assert excinfo.value.where == "LLM_PROXY_HEADERS value for 'X-Order-Id'"
        assert "X-Order-Id" in str(excinfo.value)
