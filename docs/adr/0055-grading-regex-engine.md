# 0055. A closed, selectable engine for grading regexes, linear by default

- **Status:** Accepted (change 1 of 4 implemented)
- **Date:** 2026-10-05
- **Deciders:** @azorej
- **Supersedes:** none
- **Superseded by:** none

## Context and Problem Statement

A pack writes regular expressions in four grading places:

- `trace_checks` matcher predicates, `regex` and `not_regex`, in shared and
  per-route constraints;
- `trace_checks` binders, `bind.values[*].pattern`, whose group 1 is the bound
  value;
- `transcript_rules.disallow_regex`, read case-insensitively;
- a pack's own `checks.py`, through `checks_helpers.text_matches_pattern`.

Every one of them runs on Python `re`, a backtracking engine. The cost of a
search depends on the pattern as much as on the text. An unanchored lookahead
conjunction — the shape authors write to say "the result mentions this id and
this email" — is quadratic in the text: one such predicate over a 30 KB tool
result takes 1.6 s, and the trace matcher evaluates it on every event of the
trial (#1780). Nothing about the pattern tells the author it is expensive, and
nothing bounds what a trial with large tool results costs to grade.

Rewriting authors' patterns at run time to make them cheap was rejected: it
changes what a pattern means behind the author's back.

## Decision Drivers

- **A guaranteed cost.** The default engine must search in time linear in the
  text for every pattern it accepts.
- **The engine is part of a pattern's meaning.** Engines accept different
  syntax and give different verdicts on some patterns both accept. A pack's
  verdicts must not change because a third party registered something.
- **Loud refusal.** A pattern the effective engine cannot compile is refused
  before tokens are spent, and an uncompilable pattern raises at grade time —
  the evaluators' existing fail-fast contract (`re.error`).
- **No new wire drift.** Older runner images reject unknown keys at
  `RegisterTrial`, and `TrialSpec` is dumped without `exclude_none`, so every
  key on a model is on the wire for every pack that declares the model.

## Considered Options

1. **Keep Python `re`; lint the expensive shapes.** Which shapes backtrack
   badly is not decidable from syntax in general, and the cost of a missed one
   is paid by every trial.
2. **A run-time anchoring rewrite of risky patterns.** Changes semantics
   silently; rejected outright.
3. **A closed engine vocabulary, `linear` (RE2) by default, `backtracking`
   (Python `re`) by explicit opt-in, plus a list form for conjunctions.**
4. **An entry-point registry of engines.** Third-party engines under existing
   names would change verdicts with no pack change.

## Decision

We adopt **option 3**.

### The engines

`RegexEngineKind` is a `str, Enum` with two members, named by guarantee:

| kind | engine | guarantee |
|---|---|---|
| `linear` | RE2 (`google-re2`) | search time linear in the text; no lookaround, no backreferences |
| `backtracking` | Python `re` | the full `re` syntax; cost depends on the pattern |

`tolokaforge/core/grading/regex_engine.py` is the one seam every pack-authored
grading regex is compiled through:

- `RegexEngine` (a Protocol): `compile(pattern, *, ignore_case=False) -> CompiledRegex`.
- `CompiledRegex` (a Protocol): `search(text) -> bool` and
  `first_groups(text) -> list[str | None]` — group 1 of every non-overlapping
  match, in order, the binder's read.
- `regex_engine(kind)` looks the kind up in a closed, module-level mapping that
  covers every member. Compiled patterns are cached per
  `(kind, pattern, ignore_case)` in a bounded cache.
- `UncompilablePattern` subclasses `re.error` and carries `engine`, `pattern`
  and the engine's reason as `str`. Every `except re.error` stays honest under
  either engine; RE2's own error type never leaves the module.
- `CompiledPatterns` (a frozen dataclass) is one or more patterns compiled under
  one engine, with `every_searches` and `none_searches`.

The `linear` engine compiles with RE2's `log_errors` off, so a refusal writes
nothing to stderr; the reason travels in the exception.

RE2 encodes a `str` as UTF-8, which a lone surrogate cannot be, and
`json.loads` produces lone surrogates from tool results and assistant text. The
`linear` engine searches a view of the text with every code point in
U+D800–U+DFFF replaced one-for-one by U+FFFD. RE2 reports spans in code points,
so a capture is sliced from the original text. A failure here would crash the
grade on agent-produced text. The substitution can move only a verdict that
names U+FFFD itself or counts it under `.` or a class, where it stands in for
exactly the character it replaced.

The vocabulary is closed — not an entry-point seam — because the engine decides
what an authored pattern means.

### Where an engine is chosen

- **Section-level default:** `trace_checks.regex_engine` covers every `regex`,
  `not_regex` and `bind.values[*].pattern` in the block, shared and per-route;
  `transcript_rules.regex_engine` covers `disallow_regex`. Each evaluator is
  already handed its own section config, so the key reaches the evaluator on
  both substrates without a new parity surface, and a section key is on the
  wire only for packs that declare the section. A top-level
  `grading.regex_engine` would be on the wire for every pack and make an older
  runner image refuse every trial.
- **Per-site override:** `ValuePredicate.regex_engine` (its `regex` and
  `not_regex`) and `BoundValue.regex_engine` (its `pattern`). It is a modifier,
  not an operator: it asserts nothing on its own. A `regex_engine` on a site
  with no pattern is a load error.
- `checks_helpers.text_matches_pattern` stays on Python `re`: it is a helper a
  pack's own Python calls, with Python's flags.

### The list form

`regex: [p1, p2]` holds when every pattern searches the value; `not_regex:
[p1, p2]` holds when none does. It replaces the lookahead conjunction RE2
cannot compile.

### The default and its refusals

`linear` is the default. A pattern the effective engine cannot compile is
refused by the authoring gate: under `linear` as an advisory (fatal under the
default `fail_on`) that names the list form and the `backtracking` opt-in, under
`backtracking` as an error.

RE2 refuses `(?=…)`, `(?!…)`, `(?<=…)`, backreferences, possessive
quantifiers, `\Z` (RE2 spells it `\z`), repeat counts over 1000, `(?x)` and
`\N{NAME}`.

These patterns both engines accept get a different verdict
(`tests/unit/grading/test_regex_engine.py` pins each row under both engines):

| pattern | text | `backtracking` | `linear` | why |
|---|---|---|---|---|
| `\d` | `٣` (U+0663) | match | no match | RE2 `\d` is ASCII |
| `\w` | `é` | match | no match | RE2 `\w` is ASCII |
| `\s` | U+001C | match | no match | RE2 `\s` is ASCII whitespace |
| `\bfoo\b` | `éfoo` | no match | match | RE2 `\b` is an ASCII word boundary |
| `a$` | `a` + newline | match | no match | RE2 `$` without `(?m)` is end of text only |
| `[[:alpha:]]+` | `ab:` | no match | match | a POSIX class in RE2, a character set in `re` |
| `a{,3}` | `aaaa` | match | no match | `re` reads `{0,3}`; RE2 reads the literal text `{,3}` |

### Changes

1. The engine seam and the `google-re2` dependency, in the root
   `[project].dependencies` and in the runner subset's requirement allowlist.
   **Implemented.**
2. The section-level keys and per-site overrides; every pack-authored site
   compiled through the seam, eagerly, on its effective engine. **Implemented.**
3. The list form of `regex` and `not_regex`. **Implemented.**
4. `linear` becomes the default; the gate's advisory for patterns `linear`
   cannot compile.

## Consequences

### Positive

- Under the default engine, the cost of grading a regex is linear in the text,
  whatever the author wrote.
- An author states the engine a pattern needs, per section or per site, and the
  gate checks each pattern against that engine before a run.
- Lookahead conjunctions have a direct replacement in the list form.

### Negative / Trade-offs

- A new native dependency. `google-re2` ships wheels for CPython 3.10–3.12 on
  manylinux x86_64 / aarch64 (glibc ≥ 2.28) and macOS ≥ 13; other hosts build
  it from source, which needs a C++ toolchain and abseil.
- Packs whose patterns RE2 refuses must change them or opt into
  `backtracking`. Packs whose patterns hit a row of the table above change
  verdict with no refusal.
- The new keys are on the wire for packs that declare `trace_checks` or
  `transcript_rules`; an older runner image refuses those trials at
  `RegisterTrial`.

### Follow-ups

- Structured predicates over a tool result's JSON, so authors stop matching
  serialized JSON with regexes (#1795).

## Links

- Related ADRs:
  - [0011](0011-seam-and-declaration-conventions.md): when a Protocol seam is
    not an entry-point registry;
  - [0025](0025-runner-wheel-split.md): the runner subset's dependency surface.
- Related code:
  - `tolokaforge/core/grading/regex_engine.py`;
  - `scripts/hatch/hatch_runner_subset_builder.py` (`SUBSET_REQUIREMENT_NAMES`).
- Related issues: #1780.
- External references: [RE2 syntax](https://github.com/google/re2/wiki/Syntax).
