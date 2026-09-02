# 0042. Adapter-blind authoring gate — three new `BaseAdapter` hooks + `SkipKind` split

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`tolokaforge validate` and the orchestrator's pre-flight run one shared
authoring gate over five layers of a task's grading declaration —
combine, hash-source, tool-inventory, replay-world, and seeded-tables.
Two of those layers already dispatch through classmethod hooks on
`BaseAdapter`
([`grading_combine_layer`](../../tolokaforge/adapters/base.py) at
`base.py:299-308`, `grading_hash_source_layer` at `base.py:310-327`).
The other three short-circuit inside `_task_loader.py`:
[`tool_inventory_under_adapter`](../../tolokaforge/adapters/_task_loader.py)
(`_task_loader.py:815-829`),
[`replay_world_under_adapter`](../../tolokaforge/adapters/_task_loader.py)
(`_task_loader.py:832-853`), and
[`seeded_tables_under_adapter`](../../tolokaforge/adapters/_task_loader.py)
(`_task_loader.py:902-921`). Each of those three tests
`adapter_type != AdapterType.NATIVE.value` and returns the layer's
`unresolvable()` for every non-native pack, before any adapter code
runs.

The `AuthoringReport` channel that receives an `unresolvable()` layer's
skip is `unchecked` — documented at
[`config_validation.py:400-427`](../../tolokaforge/core/grading/config_validation.py)
as a channel, not a third severity: nothing reads it to decide fatality.
That contract is honest for the case it was designed against — an
environment that has not installed the adapter cannot inspect the pack
and must not refuse it. It is dishonest for the case where the adapter
*is* installed and its hook returned `unresolvable()` on purpose,
because both cases render identically in the report.

The consequence is that adapter-owned packs (`frozen_mcp_core`,
`tlk_mcp_core`, `terminal_bench`, and any future adapter) validate
invisibly for the three hard-gated layers: a mis-spelled tool name, a
golden-replay against a world the adapter does not build, and an
`id_fields` declaration against a table the adapter does not seed all
reach grade time. The user's own framing — "many adapters for different
parts of the system" — is the constraint the interface has to survive:
new adapters must not know about new validators, and old adapters must
not need to move when a validator is added.

The nearest sibling in the ecosystem is
[inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai),
[openai/evals](https://github.com/openai/evals/blob/main/docs/custom-eval.md),
and
[lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/lm_eval/tasks/README.md).
None of the three carry per-backend authoring validation — the stronger
claim tolokaforge makes (validate a pack's grading against the shape
the adapter's runtime presents) is what forces the hook-family
architecture here.

## Decision Drivers

1. **Adapter-owned packs pass validation invisibly for three layers.**
   The tool-name / golden-replay / id-fields rules today downgrade to
   a never-fatal `unchecked` skip for every non-native pack. A
   misspelled tool name or an `id_fields` declaration against an
   unseeded table reaches grade time. Making adapter-owned packs
   catchable is the whole reason to touch this surface.

2. **Three hard-gated NATIVE branches at
   [`_task_loader.py:826-829, 846-849, 917-921`](../../tolokaforge/adapters/_task_loader.py)
   mean no adapter can answer.** The three helpers test
   `adapter_type != AdapterType.NATIVE.value` and return
   `unresolvable()` before any adapter code runs — there is no seam
   for an adapter to speak through today. Adding a seam is a
   prerequisite before any adapter (in-tree or out-of-tree) can
   contribute a fact.

3. **`AuthoringReport.unchecked` conflates two distinct cases.** The
   channel documented at
   [`config_validation.py:400-427`](../../tolokaforge/core/grading/config_validation.py)
   as never-fatal serves the "environment cannot inspect this pack"
   case correctly. It also carries the "adapter is loaded and its
   hook returned `unresolvable()`" case identically, and that case
   deserves a promotable severity for an author who owns the adapter.
   The two cases have to be distinguishable at the wire level so the
   CLI can enforce them differently without changing the report shape.

4. **`unresolvable()` on the two existing hooks
   ([`grading_combine_layer`](../../tolokaforge/adapters/base.py) at
   `base.py:299-308`,
   [`grading_hash_source_layer`](../../tolokaforge/adapters/base.py)
   at `base.py:310-327`) is a fact, not a verdict.** The two shipped
   hooks default to `unresolvable()` on `BaseAdapter` and expect
   adapters to override with facts about their own runtime. Extending
   the family has to preserve that discipline — a hook has to be able
   to say "I cannot answer" without a verdict getting attached to it,
   and the CLI has to be where the verdict is applied.

5. **The API must survive new adapters that do not know about future
   validators.** The user's framing — "many adapters for different
   parts of the system" — is a constraint the interface has to meet:
   an adapter written today has to keep validating under a validator
   added tomorrow. The mixin-defaults shape set by
   [ADR-0039 § "`CodingHarnessAdapterMixin`"](0039-coding-harness-adapter-agnostic.md#codingharnessadaptermixin)
   applies: a `BaseAdapter` default keeps every un-opted-in adapter
   valid, and opt-in is one classmethod at a time.

## Considered Options

1. **Keep the `AdapterType.NATIVE` hard-gate.** Status quo. Rejected —
   adapter-owned packs stay invisible to the three affected rules; a
   growing adapter fraction stays unvalidated as new adapters join.

2. **Collapse the hook family into one `grading_layers()` returning a
   bundle per adapter.** Rejected. Per-layer `unresolvable()` is
   finer-grained than a whole-bundle answer: an adapter that can answer
   for `tool_inventory` but not `seeded_tables` (a live case for
   `frozen_mcp_core`, where the adapter serves a tool set from a bundled
   `_domain/` directory but does not seed JSON-DB state) has no way to
   surface that shape under the bundle-return signature. Merging the
   layers would also break the "adapter opts in one classmethod at a
   time" property that Decision Driver 5 rests on.

3. **Extend the existing `grading_*` hook family with three new
   classmethods, split `unchecked` two ways with `SkipKind`, add
   `--strict-authoring` at the CLI.** Chosen — see § Decision.

## Decision

Adopt Option 3. Three additions, one at each layer:

### Three new `BaseAdapter` classmethod hooks

Beside the existing `grading_combine_layer` and
`grading_hash_source_layer`, `BaseAdapter` grows three classmethods —
`grading_tool_inventory`, `grading_replay_world`, `grading_seeded_tables`
— each with the shape:

```python
@classmethod
def grading_<layer>(cls, task: TaskConfig, task_dir: Path) -> <LayerType>:
    return <LayerType>.unresolvable()
```

The signature matches `grading_hash_source_layer` for the reason its
docstring at `base.py:322-326` names: `tolokaforge validate` holds no
adapter instance and must keep validating packs whose adapter package
is not installed, so every fact reported here is a function of
`task + task_dir` alone. `NativeAdapter` overrides all three, keeping
the shipped native-side logic byte-identical.

### The four `*_under_adapter` helpers dispatch uniformly through the hook family

`_task_loader.py`'s three hard-gated helpers migrate to the four-line
shape `hash_source_layer_under_adapter` already ships at
`_task_loader.py:870-875`: resolve the adapter class; if not installed,
return the layer's `unresolvable()` with `kind=SkipKind.STRUCTURAL`;
otherwise dispatch to the adapter's hook. The `AdapterType.NATIVE`
short-circuit and the local `from tolokaforge.runner.models import
AdapterType` imports at `_task_loader.py:825, 846, 917` are removed —
`NativeAdapter`'s hook overrides answer through the same dispatch.

### `SkipKind` splits `unchecked` two ways

A new `SkipKind(str, Enum)` in `config_validation.py` carries two
values:

- **`STRUCTURAL`** — the environment cannot inspect this pack (adapter
  uninstalled, misspelled type, or a schema this reading cannot
  resolve). Never fatal — the shipped
  [`Skip("grading", _UNRESOLVABLE_REASON)`](../../tolokaforge/core/grading/config_validation.py)
  contract at `config_validation.py:906` survives verbatim for this
  case: a pack whose grading nothing here can interrogate is reported
  rather than refused.
- **`ADAPTER_DECLARED`** — the adapter is loaded and its hook returned
  `unresolvable()`. Reported never-fatal by default; promotable to
  fatal through `tolokaforge validate --strict-authoring`.

`Skip` gains `kind: SkipKind = SkipKind.STRUCTURAL` — matches every
existing call site, which today produces the "environment cannot
inspect" shape. Each layer class (`ToolInventory`, `ReplayWorld`,
`HashSourceLayer`, `SeededTablesLayer`) gains a `skip_kind` field, and
its `unresolvable()` classmethod signature becomes
`unresolvable(kind: SkipKind = SkipKind.ADAPTER_DECLARED) -> Self` —
an `unresolvable()` layer is by construction an adapter-declared
answer, and the four `*_under_adapter` helpers override the default
with `STRUCTURAL` on the adapter-not-installed arm.

### `tolokaforge validate --strict-authoring`

A `--strict-authoring/--no-strict-authoring` Click option on
`tolokaforge validate` (`default=False`). When set, the CLI filters
`report.unchecked` for `skip.kind is SkipKind.ADAPTER_DECLARED` and,
if any are found, marks the task invalid alongside the existing errors
/ advisories path. STRUCTURAL skips render as warnings regardless of
the flag. `AuthoringReport.fatal(fail_on)` is unchanged —
`--strict-authoring` is a caller enforcement decision applied after
`fatal()` returns.

The design mirrors the mixin-defaults shape set by
[ADR-0039](0039-coding-harness-adapter-agnostic.md): safe defaults on
the base, one-classmethod opt-in per opted-in adapter, third-party
adapters compose without an engine edit.

## Consequences

### Positive

- **Adapter-owned packs validated to the same rules as native packs**
  once their adapter implements the hooks. A misspelled tool name or an
  `id_fields` declaration against an unseeded table is now catchable
  under the adapter's own reading of the pack.
- **The seam composes.** `BaseAdapter` defaults to `unresolvable()`,
  so an uninstalled or newly-added adapter inherits the same safe
  answer today's three helpers hand-code. A future validator adds one
  classmethod to `BaseAdapter` with an `unresolvable()` default —
  adapters opt in one at a time without an engine edit.
- **The static-gate default is preserved.** `tolokaforge validate`
  with no `--strict-authoring` flag continues to accept packs whose
  adapter is not installed: STRUCTURAL skips remain never-fatal, and
  `report.fatal(fail_on)` is unchanged. Packs targeting an uninstalled
  adapter validate as they have.
- **The promotion knob has a clear audience.** A task-pack repository
  that owns its adapter and runs `tolokaforge validate --strict-authoring`
  in CI catches the adapter's own `unresolvable()` answers before
  merge — the case the "unchecked is a channel, not a severity"
  contract could not reach.
- **The shape reads as one family.** Five hooks
  (`grading_combine_layer`, `grading_hash_source_layer`,
  `grading_tool_inventory`, `grading_replay_world`,
  `grading_seeded_tables`), one signature convention, one default
  discipline. The reader sees a system, not a "two old + three new"
  split.

### Negative / Trade-offs

- **The adapter interface grows by three classmethods.** Third-party
  adapter authors have three more optional hooks to consider. Mitigated
  by the `BaseAdapter` defaults — an adapter that adds nothing keeps
  today's behaviour. Documented in
  [`docs/ADAPTER_INTERFACE.md`](../ADAPTER_INTERFACE.md) as one family
  of five hooks.
- **`Skip` gains a `kind` field.** Every existing consumer ignores it.
  A grep confirms `Skip` and `AuthoringReport` cross no serialisation
  boundary in the repo — neither is written to YAML or JSON, so the
  field addition is a compile-time-only change for callers.
- **`AuthoringReport.unchecked`'s semantics grow a wrinkle.** The
  "channel, not severity" documentation stays literally true — the
  channel is still not a severity, and the report shape is unchanged
  — but consumers now have a `.kind` on each entry that carries
  meaning at the CLI enforcement layer. The docstring at
  `config_validation.py:400-427` grows one paragraph naming the two
  kinds and where they surface.
- **The flag is opt-in, not on by default.** An adapter-owning
  task-pack CI has to remember to add `--strict-authoring`. Making the
  flag default-`True` would break every external task-pack CI relying
  on `tolokaforge validate` against adapter types the local
  environment has not installed — the shipped default has to preserve
  the "reports never fatal" contract, and the flag is the opt-in
  surface.

### Follow-ups

- **Code changes required.** Add `SkipKind` and thread `kind` through
  the four layer classes; add the three hooks on `BaseAdapter` and
  their `NativeAdapter` overrides; migrate the three helpers in
  `_task_loader.py`; add the CLI flag with its enforcement logic.
- **Documentation to update.**
  [`docs/ADAPTER_INTERFACE.md`](../ADAPTER_INTERFACE.md) — extend the
  "Optional Methods" section with items covering all three new hooks
  in the same shape as the existing `grading_hash_source_layer` entry;
  [`docs/GRADING.md`](../GRADING.md) § "What is validated before a
  run" — name the two `SkipKind` values and the `--strict-authoring`
  promotion; [`docs/CLI.md`](../CLI.md) § "Task validation" — add the
  flag beside `--tasks`; [`CHANGELOG.md`](../../CHANGELOG.md) — one
  line under the current in-flight release entry.
- **Tests to add.** A reproducer unit test that locks the pre-migration
  shape and flips to the post-migration shape at the layer boundary; a
  `SkipKind` unit test for the enum and the layer-default plumbing;
  hook-dispatch unit tests for both the default (`unresolvable()`
  returned by `BaseAdapter`) and the override (`NativeAdapter`); the
  canonical corpus at
  `tests/canonical/test_example_pack_grading_corpus.py` regenerates
  once — adapter-owned packs' three per-layer skips shift to reading
  `kind=SkipKind.ADAPTER_DECLARED`; `click.testing.CliRunner` coverage
  for `--strict-authoring` over a native pack, a synthetic
  adapter-owned pack, and an unregistered adapter type.
- **Out-of-tree adapter hook implementations.** The
  `frozen_mcp_core` adapter (`tolokaforge-adapter-frozen-mcp-core`
  package, tracked as issue #1331) and the `tlk_mcp_core` adapter
  (`tolokaforge-adapter-tlk-mcp-core` package, tracked as issue
  #1332) each ship their own hook implementations in a follow-up PR
  against their respective repositories, after this ADR's engine-side
  contract lands. Both are blocked on this ADR; both are the whole
  point of the hook family — the ADR ships the seams they need.
- **`terminal_bench` inherits the defaults.** The in-tree
  coding-harness adapter at
  [`external_adapters/tolokaforge-adapter-terminal-bench/`](../../external_adapters/tolokaforge-adapter-terminal-bench/)
  serves packs that do not carry `state_checks` / `trace_checks` in the
  JSON-DB sense the three new hooks answer for. Inheriting
  `unresolvable()` on all three keeps its packs validating the way they
  do today; no override ships.

## Non-goals

- **Not adding a new channel on `AuthoringReport`.** `SkipKind` is an
  additive tag on `Skip`. The three existing channels (`errors`,
  `advisories`, `unchecked`) keep their wire shape and their
  documented semantics; the CLI layer reads `kind` for its enforcement
  decision.
- **Not making the gate mandatory-by-default for adapter-owned
  packs.** `--strict-authoring` is opt-in with `default=False` so
  packs whose `adapter_type` names an uninstalled adapter continue to
  validate without their author being present.
- **Not reworking `AuthoringReport.fatal(fail_on)`.** That enforcement
  API stays unchanged. `--strict-authoring` is enforced at the CLI,
  after `fatal()` returns.
- **Not touching `AuthoringReport`'s wire shape.** The report is
  in-process only (grep-confirmed no serialisation); the additive
  `kind` field on `Skip` extends without breaking any consumer.
- **Not migrating `grading_combine_layer` / `grading_hash_source_layer`
  shape.** Both already dispatch through the hook family; only the
  three new hooks are added.
- **Not shipping the two out-of-tree MCP-family adapter hook
  implementations in this ADR.** Those land per-adapter in follow-up
  issues #1331 / #1332 with their own version bumps.

## Links

- Related ADRs:
  - [ADR-0033](0033-external-harness-registry.md) — external harness
    registry; the earlier extensibility pattern this ADR extends into
    the authoring-gate surface.
  - [ADR-0038](0038-grader-detachment.md) — grader detachment; context
    for what an adapter-declared skip means at grade time.
  - [ADR-0039](0039-coding-harness-adapter-agnostic.md) — the closest
    architectural sibling; same mixin-default discipline on the base,
    same one-classmethod opt-in convention for opted-in adapters.
- Related code:
  - [`tolokaforge/adapters/base.py`](../../tolokaforge/adapters/base.py)
    — existing hook family (`grading_combine_layer`,
    `grading_hash_source_layer`) at `:299-327`.
  - [`tolokaforge/adapters/_task_loader.py`](../../tolokaforge/adapters/_task_loader.py)
    — the four `*_under_adapter` helpers at `:815-921`.
  - [`tolokaforge/core/grading/config_validation.py`](../../tolokaforge/core/grading/config_validation.py)
    — `Skip`, `AuthoringReport`, and the `unchecked` channel at
    `:392-432`.
  - [`tolokaforge/adapters/native.py`](../../tolokaforge/adapters/native.py)
    — `NativeAdapter`, the shipped in-tree overrider.
- External references:
  - [inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai)
  - [openai/evals — custom eval docs](https://github.com/openai/evals/blob/main/docs/custom-eval.md)
  - [lm-evaluation-harness — tasks README](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/lm_eval/tasks/README.md)
- Issue: [#1302](https://github.com/Toloka/tolokaforge/issues/1302)
  closes with the merge of the PR that ships the engine-side contract
  this ADR describes.
