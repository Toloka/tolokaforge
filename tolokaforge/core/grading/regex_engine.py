"""The engines a pack-authored grading regex is compiled and run by.

The vocabulary is closed and named by guarantee: ``linear`` is RE2 (no
lookaround, no backreferences, a search costs time linear in the text) and
``backtracking`` is Python :mod:`re`. Which engine reads a pattern is part of
what the pattern means — the two accept different syntax and give different
verdicts on some patterns both accept — so the set is a grading-semantics
contract and not an entry-point seam. ADR-0055 records the decision and
tabulates the differences.

A leaf with no first-party imports: the config models in
:mod:`tolokaforge.runner.models` and the evaluators under
:mod:`tolokaforge.core.grading` both read it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Protocol

import re2

_COMPILE_CACHE_SIZE = 4096

_SURROGATE = re.compile("[\ud800-\udfff]")


class RegexEngineKind(str, Enum):
    """Which engine compiles and runs a grading regex."""

    LINEAR = "linear"
    BACKTRACKING = "backtracking"


class UncompilablePattern(re.error):
    """A pattern its engine refuses to compile.

    A ``re.error`` under either engine, so ``except re.error`` sees an RE2
    refusal too; the RE2 error type never leaves this module.
    """

    def __init__(
        self, engine: RegexEngineKind, pattern: str, reason: str, pos: int | None = None
    ) -> None:
        super().__init__(
            f"the {engine.value} regex engine cannot compile {pattern!r}: {reason}", pattern, pos
        )
        self.engine = engine
        self.reason = reason


class CompiledRegex(Protocol):
    """One pattern, compiled by one engine."""

    @property
    def groups(self) -> int:
        """How many capture groups the pattern declares, named ones included."""
        ...

    def search(self, text: str) -> bool:
        """Whether the pattern matches anywhere in ``text``."""
        ...

    def first_groups(self, text: str) -> list[str | None]:
        """Group 1 of every non-overlapping match, in order; ``None`` where it did not take part."""
        ...


class RegexEngine(Protocol):
    """Compiles patterns into :class:`CompiledRegex`; refuses with :class:`UncompilablePattern`."""

    def compile(self, pattern: str, *, ignore_case: bool = False) -> CompiledRegex: ...


class _BacktrackingRegex:
    def __init__(self, compiled: re.Pattern[str]) -> None:
        self._compiled = compiled

    @property
    def groups(self) -> int:
        return self._compiled.groups

    def search(self, text: str) -> bool:
        return self._compiled.search(text) is not None

    def first_groups(self, text: str) -> list[str | None]:
        return [match.group(1) for match in self._compiled.finditer(text)]


class _LinearRegex:
    """RE2 encodes the text as UTF-8, which a lone surrogate (valid in a ``str``
    that ``json.loads`` produced) cannot be. It searches a view with every
    surrogate replaced one-for-one by U+FFFD; RE2 spans count code points, so a
    capture is sliced from the original text at the same span."""

    def __init__(self, compiled: re2._Regexp) -> None:
        self._compiled = compiled

    @property
    def groups(self) -> int:
        return self._compiled.groups

    def search(self, text: str) -> bool:
        return self._compiled.search(_without_surrogates(text)) is not None

    def first_groups(self, text: str) -> list[str | None]:
        spans = (match.span(1) for match in self._compiled.finditer(_without_surrogates(text)))
        return [None if start < 0 else text[start:end] for start, end in spans]


def _without_surrogates(text: str) -> str:
    if text.isascii():
        return text
    return _SURROGATE.sub("\ufffd", text)


@lru_cache(maxsize=_COMPILE_CACHE_SIZE)
def _compile_backtracking(pattern: str, ignore_case: bool) -> CompiledRegex:
    try:
        compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as refusal:
        raise UncompilablePattern(
            RegexEngineKind.BACKTRACKING, pattern, refusal.msg, refusal.pos
        ) from refusal
    return _BacktrackingRegex(compiled)


@lru_cache(maxsize=_COMPILE_CACHE_SIZE)
def _compile_linear(pattern: str, ignore_case: bool) -> CompiledRegex:
    options = re2.Options()
    options.log_errors = False
    options.case_sensitive = not ignore_case
    try:
        compiled = re2.compile(pattern, options)
    except re2.error as refusal:
        raise UncompilablePattern(
            RegexEngineKind.LINEAR, pattern, _decoded_reason(refusal)
        ) from refusal
    return _LinearRegex(compiled)


def _decoded_reason(refusal: re2.error) -> str:
    reason = refusal.args[0] if refusal.args else ""
    if isinstance(reason, bytes):
        return reason.decode("utf-8", errors="backslashreplace")
    return str(reason)


@dataclass(frozen=True)
class _CachedEngine:
    _compile: Callable[[str, bool], CompiledRegex]

    def compile(self, pattern: str, *, ignore_case: bool = False) -> CompiledRegex:
        return self._compile(pattern, ignore_case)


_ENGINES: Mapping[RegexEngineKind, RegexEngine] = MappingProxyType(
    {
        RegexEngineKind.LINEAR: _CachedEngine(_compile_linear),
        RegexEngineKind.BACKTRACKING: _CachedEngine(_compile_backtracking),
    }
)


def regex_engine(kind: RegexEngineKind) -> RegexEngine:
    """The engine registered under ``kind``."""
    return _ENGINES[kind]


@dataclass(frozen=True)
class CompiledPatterns:
    """One or more patterns compiled under one engine, read together."""

    engine: RegexEngineKind
    compiled: tuple[CompiledRegex, ...]

    @classmethod
    def compile(cls, patterns: Sequence[str], engine: RegexEngineKind) -> CompiledPatterns:
        """Compile every pattern under ``engine``; raises :class:`UncompilablePattern` on the first refusal.

        A bare ``str`` is refused rather than read as a sequence of one-character
        patterns, and an empty sequence because every-of-none holds vacuously.
        """
        if isinstance(patterns, str):
            raise TypeError(f"patterns must be a sequence of str, not the str {patterns!r}")
        if not patterns:
            raise ValueError("patterns must name at least one pattern")
        compiler = regex_engine(engine)
        return cls(engine, tuple(compiler.compile(pattern) for pattern in patterns))

    def every_searches(self, text: str) -> bool:
        """Whether every pattern matches somewhere in ``text``."""
        return all(compiled.search(text) for compiled in self.compiled)

    def none_searches(self, text: str) -> bool:
        """Whether no pattern matches anywhere in ``text``."""
        return not any(compiled.search(text) for compiled in self.compiled)
