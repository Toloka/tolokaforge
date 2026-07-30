# 0012. `CheckExecutor` Protocol — the custom-checks extension seam

- **Status:** Accepted
- **Date:** 2026-07-30
- **Deciders:** Tolokaforge maintainers
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Custom Python checks are the deterministic grading extension for task authors: a
task pack declares `custom_checks.enabled: true` in its grading config, ships a
`checks.py` with `@init` + `@check` decorated functions, and the harness runs
those checks against the trial's evidence (initial/final state, transcript, task
metadata) and folds the per-check verdicts into the grade. The authoring API —
the `@init`/`@check` decorators, `CheckContext`, `CheckPassed`/`CheckFailed`/
`CheckSkipped`, `CustomChecksConfig` — is already Pattern B strict Pydantic and
untouched by this ADR.

The **executor** that loads `checks.py`, resolves relative imports, runs the
decorated functions under a timeout, and returns a `CheckResultSet` was a single
concrete class (`CheckRunner`) with no Protocol seam and no `InMemory*` fixture.
Every runtime sibling — `RuntimeBackend` (ADR-0007), `Conductor` (ADR-0008),
`TrialGrader` (ADR-0014), `Judge` (ADR-0020) — already follows Pattern A
(ADR-0011): a `@runtime_checkable` Protocol, a production implementation, an
`InMemory*` fixture with a call-log + failure knobs, and a canonical contract
test pinning the boundary. ADR-0011's Follow-ups called for lifts of exactly
this shape when a second variant becomes realistic.

A second variant is realistic **now**, driven by two follow-ups the runner-side
wire uncovers:

- **#673 (harden custom-checks execution isolation)** — the current in-process
  ThreadPoolExecutor is not a sandbox: a runaway thread cannot be killed by
  `timeout`, checks run with container-network + filesystem access, and there is
  no per-check resource cap. A subprocess/namespace-isolated executor is a
  named second variant, blocked today by the missing seam.
- **Runner-side test injection** — the runner-side wire (issue #669, this PR)
  needs to unit-test `RunnerService.GradeTrial`'s custom-checks path without a
  live checks module: today the only route is a `unittest.mock.MagicMock`
  around `CheckRunner`, which pins mock shape instead of executor behaviour
  (AGENTS Rule 5: test behaviour, not code).

The problem this ADR resolves: **which shape does the executor take so the
subprocess variant lands without forking runner-side wire, and so tests inject
a real fixture instead of a mock — without a public-API break to the 27
`CheckRunner()` instantiation sites?**

## Decision Drivers

- **Pattern A (ADR-0011).** Every runtime seam is `Protocol` + production impl +
  `InMemory*` fixture + canonical contract test. The custom-checks executor is
  the last first-class grading-plane runtime component that pre-dates the
  pattern.
- **Zero public-API break.** `CheckRunner` is a *published class* re-exported at
  27 instantiation sites across 5 files (`combine.py`, `check_runner.py` incl.
  `run_custom_checks`, `tests/unit/grading/test_custom_checks_runner.py`,
  `tests/unit/grading/test_custom_checks.py`, `tests/canonical/
  test_custom_checks_canon.py`). A repoint of the name to a Protocol would turn
  every one into `TypeError` and force a rename migration for aesthetics.
- **The evidence surface is the invariant worth pinning.** An executor must
  never accept the deterministic-oracle fields of `GradingConfig`
  (`golden_actions`, `expected_hash`, `jsonpath_checks`, `grading_config`); the
  Protocol carries only `(checks_file, task_dir, ctx, config)` so those fields
  cannot leak in.
- **A subprocess-isolated variant must not be forced to accept the current
  in-process knobs.** `executor_type` / `max_workers` are meaningful to the
  ThreadPoolExecutor impl only; they cannot sit on the shared contract.
- **Behaviour preservation.** The `CheckRunner.run` output must be byte-for-byte
  the same as the pre-lift class — a structural lift, not a semantic change.

## Considered Options

1. **`CheckExecutor` Protocol capturing the existing `CheckRunner.run` surface;
   `CheckRunner` stays the concrete in-process production impl; new
   `InMemoryCheckExecutor` fixture + canonical contract test.** No rename, no
   public churn.
2. **Repoint `CheckRunner` to a Protocol; rename the concrete class to
   `ThreadedCheckRunner` (or similar) as the production impl.** Names match the
   `Judge`/`LLMJudge` symmetry ADR-0020 established.
3. **Keep the executor as a concrete class — no seam.** The one-impl status quo.

## Decision

We adopt **Option 1**: a `CheckExecutor` Protocol capturing the existing
`CheckRunner.run` surface, with `CheckRunner` unchanged as the production impl
and a new `InMemoryCheckExecutor` fixture.

```python
@runtime_checkable
class CheckExecutor(Protocol):
    def run(
        self,
        checks_file: Path,
        task_dir: Path,
        ctx: CheckContext,
        config: CustomChecksConfig,
    ) -> CheckResultSet: ...
```

**The evidence-only `run()` surface is the load-bearing choice.** How an
executor is *built* — the isolation strategy, worker-pool sizing, an injected
logger — lives on the concrete implementation's constructor. What an executor
*runs* — the per-trial evidence — is the `run()` surface. This mirrors `Judge`
(ADR-0020), `TrialGrader` (ADR-0014), and `Conductor` (ADR-0008): construction
captures the how, the method carries the what. A future subprocess-isolated
`SubprocessCheckExecutor` (#673) satisfies the same Protocol with a completely
different construction surface (subprocess cgroups, syscall filter, resource
caps) and drops in without touching `RunnerService.GradeTrial`.

`InMemoryCheckExecutor` is the real (non-mock) fixture: it records each `run()`
on a `CheckExecutorCallLog` and returns a configurable `CheckResultSet`.
Failure knobs — `raise_on_run=…` and `return_error=…` — exercise the two shapes
the production runner surfaces on trouble (an unexpected top-level exception,
and a `CheckResultSet` whose `error` field carries a module-load or timeout
message). The three knobs (`result_set`, `raise_on_run`, `return_error`) are
mutually exclusive and rejected at construction so tests cannot silently
misconfigure the fixture.

### Naming decision

We do **not** repoint the name `CheckRunner` to the Protocol.

ADR-0011 requires *Protocol + prod impl + `InMemory*` + contract test*; it
does **not** require the Protocol take the "clean" name. Unlike ADR-0020 —
where the concrete surface was a top-level *function* `run_rubric_judge` (not
a class, not on any `__all__`), leaving `Judge` free — the name `CheckRunner`
is an already-published *class* instantiated at 27 sites across 5 files. A
repoint to a Protocol would turn every `CheckRunner()` call into `TypeError`,
force a rename of every site, and create a published-API migration line for
aesthetics.

`CheckExecutor` (Protocol) + `CheckRunner` (in-process production impl)
delivers the full Pattern A boundary at zero public churn. The Protocol name
reads correctly on the seam side (`CheckExecutor.run` is what a grader calls);
the concrete name reads correctly on the production side (`CheckRunner`
constructs a ThreadPoolExecutor and runs a checks module). Future variants
follow the `<mechanism>CheckExecutor` pattern (`SubprocessCheckExecutor`,
`ReplayCheckExecutor`, …) — the Protocol name is the intent, not one of the
mechanisms.

Option 2 (repoint + rename) is rejected: the symmetry with `Judge`/`LLMJudge`
does not justify a published-API break, and the ADR-0011 boundary is fully
satisfied either way.

## Consequences

### Positive

- The executor is a named, swappable component, matching the rest of the
  grading plane. Runner-side tests isolate it with `InMemoryCheckExecutor`
  instead of scripting a mock.
- The narrow-evidence-surface invariant is pinned at the contract: the
  canonical test asserts `CheckExecutor.run` carries only
  `(checks_file, task_dir, ctx, config)` — no `GradingConfig`, no oracle
  fields.
- The subprocess-isolated variant (#673) has a Protocol to slot into without
  touching any caller.
- Zero migration: no rename, no `_legacy_*` alias, no CHANGELOG line — the 27
  `CheckRunner()` sites keep working exactly as before.

### Negative / Trade-offs

- The Protocol name (`CheckExecutor`) does not match the concrete class
  (`CheckRunner`). The distinction is intentional (see Naming decision), but
  contributors browsing the module see two names for what looks like the same
  thing at first read. Mitigated by the module docstring, the `__init__.py`
  re-export block, and this ADR.
- Callers that want a Protocol-typed variable annotate as
  `executor: CheckExecutor = CheckRunner()` — one extra line vs. the concrete
  annotation. Acceptable — that is exactly the shape the seam is for.

### Follow-ups

- Code changes required: none beyond this lift; #673's subprocess executor
  lands as a new `CheckExecutor` implementation.
- Documentation to update: this ADR is the new source of truth for the seam;
  `docs/custom_checks.md` describes the *authoring* surface (unchanged) and
  points at this ADR for the executor boundary (Stage 5 of the enclosing PR).
- Tests to add: `tests/canonical/test_check_executor_contract.py` pins the
  Protocol boundary + `InMemoryCheckExecutor` semantics (added with this
  lift).

## Alternatives considered

**Repoint `CheckRunner` to a Protocol (Option 2).** Rejected: 27 instantiation
sites across 5 files break with `TypeError`, forcing a rename migration + a
CHANGELOG line for a public-API break with no user-visible benefit. The
`Judge` / `LLMJudge` naming symmetry it would deliver is aesthetic; the
ADR-0011 boundary is fully satisfied by Option 1.

**Keep the executor as a concrete class (Option 3).** Rejected: no seam for
the second variant (#673), and runner-side wire tests would grow their own
`MagicMock(CheckRunner)` glue — the exact "test the mock, not the behaviour"
pattern AGENTS Rule 5 rejects.

## Links

- Related ADRs: ADR-0011 (seam + declaration conventions; Pattern A whose
  Follow-ups named this lift), ADR-0020 (`Judge` Protocol — the closest
  structural precedent; note the naming rationale for why `Judge` could take
  the clean name and `CheckExecutor` cannot).
- Related code: `tolokaforge/core/grading/check_runner.py`
  (`CheckExecutor` / `CheckRunner` / `InMemoryCheckExecutor` /
  `CheckExecutorCallLog`), `tolokaforge/core/grading/checks_interface.py`
  (the authoring surface — unchanged);
  `tests/canonical/test_check_executor_contract.py`.
- External references: GH #669 (finish runner-side custom checks — this ADR
  is Stage 1 of that PR), #406 (finish or delete the custom-checks seam),
  #217 (dead-plumbing follow-up), #673 (subprocess-isolated executor —
  first named second variant), #674 (remove dead `run_custom_checks` /
  `result_to_score` convenience helpers).
