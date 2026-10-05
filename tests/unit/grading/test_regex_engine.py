"""The grading regex engines: what each refuses, what each reads differently, and
that ``linear`` stays linear-time and quiet."""

from __future__ import annotations

import json
import pickle
import re
import time

import pytest

from tolokaforge.core.grading.regex_engine import (
    CompiledPatterns,
    RegexEngineKind,
    UncompilablePattern,
    engine_for,
)

pytestmark = [pytest.mark.unit, pytest.mark.grading]

LINEAR = RegexEngineKind.LINEAR
BACKTRACKING = RegexEngineKind.BACKTRACKING

LINEAR_REFUSALS = [
    pytest.param("(?=a)", "invalid perl operator", id="lookahead"),
    pytest.param("(?!a)b", "invalid perl operator", id="negative-lookahead"),
    pytest.param(r"(a)\1", "invalid escape sequence", id="backreference"),
    pytest.param("(?P<x>a)(?P=x)", "invalid perl operator", id="named-backreference"),
    pytest.param("(?<=a)b", "invalid perl operator", id="lookbehind"),
    pytest.param("(?<!a)b", "invalid perl operator", id="negative-lookbehind"),
    pytest.param("a*+", "bad repetition operator", id="possessive"),
    pytest.param(r"a\Z", "invalid escape sequence", id="backslash-Z"),
    pytest.param("(?x) a", "invalid perl operator", id="verbose-mode"),
    pytest.param("a{1001}", "invalid repetition size", id="repeat-count-over-1000"),
    pytest.param(r"(\w{1,1000}){1,1000}", "invalid repetition size", id="nested-repetition"),
    pytest.param(r"\pL{1000}" * 5, "pattern too large", id="over-the-memory-budget"),
    pytest.param(r"\N{DIGIT ONE}", "invalid escape sequence", id="named-character"),
    pytest.param("(?a)x", "invalid perl operator", id="ascii-flag"),
    pytest.param("ab\ud800", "a lone surrogate cannot be encoded as UTF-8", id="lone-surrogate"),
]


@pytest.mark.parametrize(("pattern", "reason"), LINEAR_REFUSALS)
def test_linear_refuses_what_re2_cannot_compile(pattern: str, reason: str) -> None:
    with pytest.raises(UncompilablePattern) as refused:
        engine_for(LINEAR).compile(pattern)

    assert isinstance(refused.value, re.error)
    assert refused.value.engine is LINEAR
    assert refused.value.pattern == pattern
    assert isinstance(refused.value.reason, str)
    assert reason in refused.value.reason
    assert reason in str(refused.value)


@pytest.mark.parametrize(("pattern", "_reason"), LINEAR_REFUSALS)
def test_backtracking_compiles_them_unless_re_itself_refuses(pattern: str, _reason: str) -> None:
    try:
        re.compile(pattern)
    except re.error as re_refusal:
        with pytest.raises(UncompilablePattern) as refused:
            engine_for(BACKTRACKING).compile(pattern)
        assert refused.value.engine is BACKTRACKING
        assert refused.value.reason == re_refusal.msg
        return
    engine_for(BACKTRACKING).compile(pattern)


@pytest.mark.parametrize("kind", list(RegexEngineKind))
def test_a_refusal_survives_a_pickle_round_trip(kind: RegexEngineKind) -> None:
    with pytest.raises(UncompilablePattern) as refused:
        engine_for(kind).compile("a(")

    restored = pickle.loads(pickle.dumps(refused.value))

    assert type(restored) is UncompilablePattern
    assert (restored.engine, restored.pattern, restored.reason, restored.pos) == (
        refused.value.engine,
        refused.value.pattern,
        refused.value.reason,
        refused.value.pos,
    )
    assert str(restored) == str(refused.value)


def test_a_linear_refusal_writes_nothing_to_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    capfd.readouterr()
    with pytest.raises(UncompilablePattern):
        engine_for(LINEAR).compile("(?=never-compiled-before)")

    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.parametrize("kind", list(RegexEngineKind))
def test_every_engine_kind_is_registered_and_refuses_under_its_own_name(
    kind: RegexEngineKind,
) -> None:
    engine = engine_for(kind)

    assert engine.compile("a").search("cat")
    with pytest.raises(UncompilablePattern) as refused:
        engine.compile("(")
    assert refused.value.engine is kind


def test_a_compiled_pattern_is_reused_per_engine_pattern_and_case() -> None:
    linear = engine_for(LINEAR)

    assert linear.compile("ab+") is linear.compile("ab+")
    assert linear.compile("ab+") is not linear.compile("ab+", ignore_case=True)
    assert linear.compile("ab+") is not engine_for(BACKTRACKING).compile("ab+")


def _account_listing(size: int) -> str:
    """A JSON array of accounts none of which is the one the issue's pattern names."""
    records: list[str] = []
    while sum(map(len, records)) < size:
        number = len(records) + 1000
        records.append(
            json.dumps({"account_id": f"ACC-{number:08d}", "email": f"user{number}@example.com"})
        )
    return "[" + ", ".join(records) + "]"


@pytest.mark.parametrize(
    "patterns",
    [
        pytest.param([r'"account_id":\s*"ACC-00000006"'], id="one-pattern"),
        pytest.param(
            [r'"account_id":\s*"ACC-00000006"', r'"email":\s*"x@y.z"'],
            id="the-issue-lookahead-as-a-list",
        ),
    ],
)
def test_linear_search_over_a_large_non_matching_value_is_fast(patterns: list[str]) -> None:
    value = _account_listing(200_000)
    compiled = CompiledPatterns.compile(patterns, LINEAR)

    started = time.perf_counter()
    every = compiled.every_searches(value)
    none = compiled.none_searches(value)
    elapsed = time.perf_counter() - started

    assert len(value) >= 200_000
    assert (every, none) == (False, True)
    assert elapsed < 1.0, f"linear search of {len(value)} chars took {elapsed:.3f}s"


# Patterns both engines accept but read differently. docs/GRADING.md § Regex engines
# quotes this table as the semantics of the ``linear`` default.
VERDICT_DIFFERENCES = [
    pytest.param(r"\d", "\u0663", True, False, id="digit-is-ascii-only"),
    pytest.param(r"\w", "\u00e9", True, False, id="word-is-ascii-only"),
    pytest.param(r"\s", "\x1c", True, False, id="space-is-ascii-only"),
    pytest.param(r"\bfoo\b", "\u00e9foo", False, True, id="word-boundary-is-ascii"),
    pytest.param("a$", "a\n", True, False, id="dollar-is-end-of-text"),
    pytest.param("[[:alpha:]]+", "ab:", False, True, id="posix-class"),
    pytest.param("a{,3}", "aaaa", True, False, id="empty-lower-bound-is-literal"),
    pytest.param("\ufffd", "abc \ud800 def", False, True, id="lone-surrogate-reads-as-u-fffd"),
]


@pytest.mark.filterwarnings("ignore:Possible nested set:FutureWarning")
@pytest.mark.parametrize(("pattern", "text", "backtracking", "linear"), VERDICT_DIFFERENCES)
def test_patterns_both_engines_accept_can_get_different_verdicts(
    pattern: str, text: str, backtracking: bool, linear: bool
) -> None:
    assert engine_for(BACKTRACKING).compile(pattern).search(text) is backtracking
    assert engine_for(LINEAR).compile(pattern).search(text) is linear


@pytest.mark.parametrize("kind", list(RegexEngineKind))
@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        pytest.param(r"id=(\d+)", "id=1 id=22 x id=", id="one-group"),
        pytest.param(r"(a)?b", "b ab", id="optional-group"),
        pytest.param(r"(x*)", "axxb", id="zero-width"),
        pytest.param(r"k=(\w*);", "k=; k=v;", id="empty-capture"),
    ],
)
def test_first_groups_reads_group_one_of_every_match_like_re(
    kind: RegexEngineKind, pattern: str, text: str
) -> None:
    expected = [match.group(1) for match in re.finditer(pattern, text)]

    assert engine_for(kind).compile(pattern).first_groups(text) == expected


@pytest.mark.parametrize(
    ("kind", "pattern", "groups"),
    [
        pytest.param(BACKTRACKING, r"(a)(?:b)(?P<c>c)", 2, id="backtracking-named"),
        pytest.param(LINEAR, r"(a)(?:b)(?P<c>c)", 2, id="linear-named"),
        pytest.param(LINEAR, r"\pL+", 0, id="linear-only-unicode-class"),
        pytest.param(LINEAR, r"(?<n>\d+)-(\d+)", 2, id="linear-only-named-group"),
    ],
)
def test_groups_counts_every_capture_group_the_engine_reads(
    kind: RegexEngineKind, pattern: str, groups: int
) -> None:
    assert engine_for(kind).compile(pattern).groups == groups


@pytest.mark.parametrize("kind", list(RegexEngineKind))
@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        pytest.param("abc", "xABCx", id="ascii"),
        pytest.param("\u00e9", "\u00c9", id="latin-accent"),
        pytest.param("k", "\u212a", id="kelvin-sign"),
    ],
)
def test_ignore_case_reads_like_an_inline_i_flag(
    kind: RegexEngineKind, pattern: str, text: str
) -> None:
    engine = engine_for(kind)

    assert engine.compile(pattern, ignore_case=True).search(text)
    assert engine.compile(f"(?i){pattern}").search(text)
    assert not engine.compile(pattern).search(text)


def test_linear_searches_text_with_lone_surrogates_as_if_each_were_u_fffd() -> None:
    text = json.loads('"abc \\ud800 def"')
    engine = engine_for(LINEAR)

    assert engine.compile("def").search(text)
    assert engine.compile("\ufffd").search(text)
    assert not engine.compile("xyz").search(text)


def test_linear_captures_are_slices_of_the_original_text_around_surrogates() -> None:
    text = json.loads('"\\udfff caf\\u00e9 \\ud800id=42\\udbff x"')
    engine = engine_for(LINEAR)

    assert engine.compile(r"id=(\d+)").first_groups(text) == ["42"]
    assert engine.compile(r"(.)id=").first_groups(text) == ["\ud800"]
    assert engine.compile(r"=\d(\d.)").first_groups(text) == ["2\udbff"]


def test_compiled_patterns_reads_every_and_none() -> None:
    compiled = CompiledPatterns.compile(["alpha", "beta"], LINEAR)

    assert compiled.engine is LINEAR
    assert compiled.every_searches("alpha and beta")
    assert not compiled.every_searches("alpha only")
    assert not compiled.none_searches("beta only")
    assert compiled.none_searches("gamma")


def test_compiled_patterns_refuses_the_first_uncompilable_item() -> None:
    with pytest.raises(UncompilablePattern) as refused:
        CompiledPatterns.compile(["fine", "(?=bad)"], LINEAR)

    assert refused.value.pattern == "(?=bad)"


@pytest.mark.parametrize(
    ("patterns", "error"),
    [
        pytest.param("abc", TypeError, id="bare-str"),
        pytest.param([], ValueError, id="empty"),
    ],
)
def test_compiled_patterns_refuses_a_bare_string_or_no_patterns(
    patterns: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        CompiledPatterns.compile(patterns, BACKTRACKING)  # type: ignore[arg-type]
