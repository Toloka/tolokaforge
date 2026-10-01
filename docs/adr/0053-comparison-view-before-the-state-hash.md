# 0053. A one-sided comparison view before the state hash

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** @CiroGamboa, @rsmtnn
- **Supersedes:** none
- **Realizes:** [ADR-0011](0011-seam-and-declaration-conventions.md) § Pattern A (the `tolokaforge.comparison_view_rules` seam, see [The seam](#the-seam-tolokaforgecomparison_view_rules)) and § Pattern B (the `comparison_view` declaration).
- **Related issues:** #1472 (row order as a per-table hint), #1670 (unstable fields before `compare_columns` on both substrates, fixed by #1671), #612 (two legal final-state shapes — not addressed here), #1444 (hash / diff verdict parity)

## Context and Problem Statement

`state_checks.hash` passes a trial when its final database and the state a
golden replay produces hash equal. Before hashing, the engine applies the same
masks and folds to both sides:

- the pack's `unstable_fields` drop named top-level fields in every row;
- `compare_columns` sorts a table's rows (`order: unordered`), folds equivalent
  values (`treat_null_as_empty_collection`, `treat_empty_string_as_null`,
  `normalize_timezone_suffix`), and with `mode: subset` reads the golden to allow
  extra keys;
- `auto_mask_clock_columns` and `auto_normalize_nullables` mask and fold named
  columns.

Each of these answers one question about the pair: are these two values the same
value? They are two-sided rules over columns.

The cases that fail today ask a different question, of each state on its own:
which records of this state count, and what are they keyed by?

- **Non-binding records.** A proposal the agent drafted and then superseded, a
  hold it released, a zero allocation it created and never used: rows that a
  correct trajectory may or may not leave behind.
- **Generated identifiers together with the references to them.** A sequentially
  numbered document whose id another table cites.

The answer depends on the state itself and on the initial state (a record is
new if the initial state does not have it), never on the other side. No
two-sided mask can express it. Masking a generated id as `unstable` loses the
references to it, and no existing step removes a row. What is missing is a
**one-sided pre-hash transform**: a function of one state, its initial state and
a declared configuration, applied to each side independently before the
existing steps.

The benchmark we are porting shows the scale of the gap. Each of its ten sample
cases overrides the state hash with a hand-written projection in Python. We
measured those projections with the engine's own `filter_unstable_fields`,
`apply_compare_columns_pipeline` and `compute_stable_hash` at `f766b3e3`, on the
benchmark's 40 recorded trajectories (16 graded pass by the projections, 24
fail):

| Comparison | Passes reproduced |
|---|---|
| Raw state hash | 1 of 16 |
| + row order (`order: unordered`) and the bookkeeping tables | 4 of 16 |
| + top-level `unstable_fields` | 5 of 16 |
| + the two v1 rules below | 8 of 16 |
| + their nested forms (`exclude_records.path`, a nested field in `unstable_fields`) | 11 of 16 |
| The remaining 5, from three cases | need computed rules |

None of these comparisons turned any of the 24 recorded fails into a pass.

The rules carry different weight in those numbers:

- **`exclude_records`** is the one nothing else replaces: one pass directly,
  two more in its nested form.
- **`normalize_ids`** adds no measured pass. Where no other table references the
  ids, dropping the id through `unstable_fields` gives the same verdict. Its case
  rests on referenced ids, and the one case that has them fails all four of its
  recordings for other reasons.
- **`exclude_tables`** is a convenience. Masking every column of a table through
  `unstable_fields` gives the same verdict but still compares the row count.

The per-task alternative exists: `custom_checks` with a pack's `checks.py`. It
is arbitrary code, and the grade records only `CHECKS_INTERFACE_VERSION`, not
which logic ran or with which configuration.

## Decision Drivers

- **The default must not move.** A pack without `comparison_view` hashes
  byte-identically to today: same `compute_stable_hash`, same `state_digest`, same
  canonical snapshots, same db-service ETags.
- **One-sided by construction.** The function's inputs are the state being
  viewed, its initial state and the configuration. The golden is not an input.
- **First, on the full state.** A generated id is typically declared
  `unstable(auto_id)`. If the unstable filter ran first, the references to it
  could not be rewritten.
- **One function on both substrates, locked by a parity test.** The runner
  (`core/hash.py`) and the core grading engine (`state_checks.py`) call the same
  pure function at the same point.
- **One vocabulary, reached incrementally.** v1 is its first slice, and no v1
  rule duplicates a mechanism that exists. The existing two-sided mechanisms move
  into it one step at a time, each step retiring what it replaces.
- **A real seam, with its trust boundary stated.** A rule's `kind` resolves
  through an entry-point group, and the built-in rules register there like any
  other: a registry the built-ins bypass is not a seam. A rule decides which
  states hash equal, so the seam states what the engine holds a registered rule
  to.
- **Versioned.** Every grade records which composition version and which rules
  (their names, versions and settings, as a sha256) produced the view.
- **Every `grading.yaml` key is accounted for.** One new key,
  `state_checks.comparison_view`, gets a `key_manifest` entry and a differential
  in `test_grading_substrate_parity`. Rule kinds are values under it, not keys.

## Considered Options

1. **v1 = `exclude_records` (with `exclude_tables` as a convenience) and
   `normalize_ids`, resolved through a `tolokaforge.comparison_view_rules`
   entry-point group the built-ins register in.** **Chosen.** A computed rule,
   the first draft's `custom` kind, is any rule a distribution registers.
2. **The same rules dispatched through a built-in table in the registry's
   shape, the group deferred.** Rejected: registry ceremony with no registry
   benefit. The Protocol, the per-rule validation and the resolution function
   exist either way, and the table defers only the part that lets a rule from
   outside the engine be one. Getting a distribution into the runner image is a
   delivery question every runner-reachable group shares (a judge kind's
   distribution must be installed there too), not a reason to give this seam
   another shape.
3. **The first draft's seven kinds.** Rejected.
   - `drop_fields` duplicates `unstable_fields`.
   - `unordered` duplicates `order: unordered`, which already sorts a table's
     rows.
   - `fold_values` duplicates the equivalence flags for null. Its other
     transforms (`round`, `sorted_set`, `equivalences`, `date_only`) are new, but
     no measured case needs them.

   Shipping them would make the view a parallel silo.
4. **Golden variants** (#612): N golden action lists, pass if any matches.
   Complementary, not an alternative. It answers "two legal shapes", not "the
   same shape recorded with different drafts or ids".
5. **A per-task comparator** (a `checks.py`-style hook). Rejected: arbitrary
   code, no version of the logic in the grade, no validation of its
   configuration.
6. **Grow `compare_columns`.** Rejected: it is a per-column, two-sided map.
   Removing a row and re-keying it together with its references do not fit a
   `(table, column) → rule` entry.

## Decision

Adopt **option 1**, as three reviewable changes: the module with
`exclude_records` (including its nested `path` form) and `exclude_tables`,
resolved through the entry-point group;
`normalize_ids` with property tests; the wiring into both substrates with the
parity test. The wire carries the view's diff and record in one field,
`Grade.comparison_view_json`.

### The function

`tolokaforge/core/grading/comparison_view.py`. It depends on stdlib and
pydantic, and reaches `tolokaforge.core.plugin_registry` only where a kind
resolves. The wiring makes it runner-reachable, so it must not import
`state_checks.py` or `combine.py`, which the subset wheel excludes. Until the
wiring lands, `RUNNER_SUBSET_EXCLUDED_FILES` lists it: the partition lock refuses
a subset file the runner never reaches.

```python
class ComparisonViewError(ValueError): ...

@dataclass(frozen=True)
class ComparisonViewResult:
    state: dict[str, list[dict[str, Any]]]      # the view, a new object
    record: ComparisonViewRecord                 # see Versioning; applied: kind, table, path, ...

def apply_comparison_view(
    state: Mapping[str, list[dict[str, Any]]], *,
    initial: Mapping[str, list[dict[str, Any]]] | None,
    view: ComparisonViewConfig,
    id_fields: Mapping[str, str | list[str]],     # state_checks.id_fields; absent table → "id"
) -> ComparisonViewResult
```

Each invariant is locked by a test:

- the input is never mutated;
- the same inputs give the same view;
- the golden is not a parameter;
- a rule that fails raises `ComparisonViewError`, never a partial or an
  unchanged state;
- a pack without the block leaves every existing digest unchanged.

### The order

```
full state (unstable fields present)
  → 1. comparison view, rules in list order
  → 2. unstable_fields
  → 3. compare_columns pipeline
  → 4. clock / nullable masks
  → 5. compute_stable_hash | state_digest
```

- **Runner.** When a view is declared, the runner fetches the full state of both
  sides (`DBServiceClient.get_state`) and runs steps 2–4 on the client, with the
  db-service's own table-name resolution for `unstable_fields` moved into a
  shared function. A pack without a view keeps the server-side `get_stable_hash`
  / `get_stable_state` path.
- **Where the view runs in the runner.** After the trial's database is restored
  (`restore_snapshot`), when both raw states are already in memory. A failing
  view then cannot leave the golden state in the trial's database.
- **Both substrates run steps 2 and 3 in this order** (#1671), so `order:
  unordered` never sorts by a generated id the unstable filter drops (#1670); a
  both-substrate test locks it. The parity test below covers steps 2–5 as well.

### The declaration

```yaml
state_checks:
  hash: { enabled: true, golden_actions: [...] }
  id_fields: { client_documents: id }
  comparison_view:
    version: 1
    rules:
      - kind: exclude_records
        table: transfer_holds
        where: { status: released }
        unless_referenced_by: [{ table: transfer_equipment, field: dispatch_id }]
        reason: a released hold binds nothing
      - kind: exclude_records
        table: recovery_expense_decisions
        path: purchase_allocations        # items of a nested list in each row
        where: { all_zero: [amount, tax, fee] }
      - kind: exclude_tables
        tables: [agent_discoverable_tools]
        reason: written by read tools; not business state
      - kind: normalize_ids
        table: client_documents
        key: [source_id]
        references: [{ table: correction_requests, field: evidence_ref }]
        scope: new_records                # records absent from the initial state
```

- **`kind` resolves through the `tolokaforge.comparison_view_rules` group**, not
  through a static discriminated union, and the rule validates its own entry
  into its `extra="forbid"` config model. A built-in and a registered rule take
  the same path.
- **Tables a rule names must exist** in `initial_state`; fields are checked only
  against a declared schema. Seeded records are not a schema: agents write
  fields no seeded row carries. Under `relaxed_validation` a missing table is a
  warning, as for `id_fields`.
- **An unknown `version` is refused at load.** So is an unknown `kind`, and so
  is an empty `rules` list: a view without rules is no view.
- **With `hash` disabled** the block gets a `config validate` warning, not a
  refusal.
- **No profiles in v1.** The first draft's shared `profiles` files are left out
  until a second pack needs one.

### The vocabulary (v1)

| kind | Effect | Guarantees |
|---|---|---|
| `exclude_records` | Drops the rows of `table` — or, with `path`, the items of the nested list at that path in each row — that match `where`, unless `unless_referenced_by` finds a reference to them in another row. `where` is a conjunction of equality, `in`, `is_null`, `starts_with` and `all_zero`; a missing field reads as null. | `where` is non-empty; no rule drops a whole table because some of its rows are optional. |
| `exclude_tables` | Drops the named tables whole, key included, so a table present on one side only stops counting too; `reason` is required. | Refused for a table another rule names. |
| `normalize_ids` | Rewrites the key of the records of `table` in `scope` (`new_records`, the default: ids the initial state's table lacks; or `all`) to a deterministic key, built from `key` fields or from an `ordinal_by` group (the whole scope when absent) and an ordinal ranked by `rank_by`, and every exact reference to it named in `references` (a top-level field or a dotted path). | Bijective: distinct records stay distinct. A key two records share, a key a kept record holds, a rank tie and a reference that already holds a new key raise. A dangling reference stays as it is. Records of the initial state keep their keys under `scope: new_records`, which needs the initial state. |

**The rendered key** of `normalize_ids` is `<table>:<canonical JSON of its key
fields>`, for example `fee_credit_journal:{"account_id":"A1","delta":-5,"fee_id":"F2"}`;
the ordinal form appends `#<n>`, counting from 1 among the records in scope, as in
`fee_credit_journal:{"account_id":"A1"}#2`. It is a JSON string, stable, injective,
and readable in a diff, and an integral float renders as the int it equals so `5`
and `5.0` keep one key. No prefix is impossible for a real id, so the collision
checks above, not the form, keep it apart from the ids it does not replace. A table
is re-keyed by one rule, and `key`, `ordinal_by` and `rank_by` may not name the id
field itself.

Left out of v1, and where the need goes instead:

| First draft | Instead |
|---|---|
| `drop_fields` | `unstable_fields`. One measured case needs a field nested in a dict column (1 of 16). We propose a separate change that lets `unstable_fields.field_name` take a dotted path into a dict column, and refuses a dotted name that matches nothing (today it silently does nothing). |
| `unordered` | `compare_columns.<table>.<column>.order: unordered`, which already sorts the table's rows, until the first consolidation step below. |
| `fold_values` | The existing equivalence flags. No measured case needs `round`, `sorted_set`, `equivalences` or `date_only`. |
| `custom` | Any registered rule (see [The seam](#the-seam-tolokaforgecomparison_view_rules)). The five remaining measured passes need computed rules of that kind; none ships in the engine. |

### The seam: `tolokaforge.comparison_view_rules`

```python
@runtime_checkable
class ComparisonViewRule(Protocol):
    NAME: ClassVar[str]                                     # the entry-point name
    VERSION: ClassVar[int]                                  # what the rule computes; hashed
    config_model: ClassVar[type[ComparisonViewRuleConfig]]  # extra="forbid"
    def apply(self, state: dict[str, list[dict]], *, initial: Mapping | None,
              id_fields: Mapping[str, str | list[str]],
              config: ComparisonViewRuleConfig) -> RuleOutcome: ...
```

```toml
[project.entry-points."tolokaforge.comparison_view_rules"]
exclude_records = "tolokaforge.core.grading.comparison_view:ExcludeRecords"
exclude_tables = "tolokaforge.core.grading.comparison_view:ExcludeTables"
normalize_ids = "tolokaforge.core.grading.comparison_view:NormalizeIds"
```

- **The registry.** `load_comparison_view_rule(kind)` and
  `available_comparison_view_rules()` in `plugin_registry` resolve the group
  with the shared fail-loud discovery. An unknown kind raises
  `UnknownImplementationError` naming the registered kinds, and a name two
  distributions register raises `DuplicateRegistrationError` for every lookup
  into the group. The loader returns the class, as `load_judge_kind` does; the
  view instantiates it per entry.
- **The contract.** `resolve_comparison_view_rule` holds a registration to its
  declared parts: `NAME` is the entry-point name, `VERSION` a positive int,
  `config_model` derives from `ComparisonViewRuleConfig` and keeps
  `extra="forbid"`, and `apply` exists. `apply_comparison_view` refuses an
  outcome that is not a `RuleOutcome`, or that records an application under
  another rule's name.
- **Validation per rule.** The block is a list of `{kind, ...}` entries: `kind`
  picks the rule, and the rule's config model validates the rest. Every config
  model derives from `ComparisonViewRuleConfig`, which carries `kind` and
  `names()`, the tables the entry names (the `exclude_tables` guard reads it).
- **The built-ins** register in `pyproject.toml` like any other rule. Nothing in
  the engine branches on a built-in's name.
- **Where it resolves.** The wiring makes the runner resolve rules:
  `RegisterTrial` validates the trial spec, and grading applies the view. The
  group is therefore runner-reachable, and the runner-subset wheel carries its
  rows. A rule from another distribution grades on the runner where that
  distribution is installed in the runner image; an image without it refuses
  the trial at `RegisterTrial`, naming the kinds it has. The db-service neither
  validates nor applies a view: the runner reads its full states and applies the
  view itself, so the wheel-less db-service image needs no registry.

#### The trust boundary

A rule decides which two states hash equal, so a registered rule can turn a
failing trial into a passing one: a rule that drops every table passes
anything. That is a stronger grant than a judge kind's or a search backend's. A
rule rewrites the evidence the deterministic hash verdict is computed from,
before every mask, and only its identity in the grade shows that it did. The
engine holds a rule to four things:

1. **It runs only where it is named.** A rule runs for a task whose
   `comparison_view` names its kind, so installing a distribution changes the
   grade of no other task.
2. **A name has one registration.** A distribution registering a built-in's
   name, or another distribution's, fails every lookup into the group instead
   of shadowing it.
3. **It sees one side.** The other state is never an input, and the view hands
   its rules deep copies of the state, the initial state and `id_fields`.
4. **Its identity is in the grade.** `NAME` and `VERSION` are hashed into
   `config_sha256`, and the record lists what it did under its `NAME`.

The engine cannot tell a sound rule from an unsound one. A rule is part of the
answer key: a distribution that registers one is reviewed and pinned as a
task's golden actions are. The Protocol's docstring and
`docs/GRADER_SERVICE.md` § Extension points state the same boundary.

### One vocabulary over time

v1 is the first slice. The existing mechanisms move in one step at a time. Each
step is its own amendment and PR, with its own parity differential and a
`warn_deprecated` transition, since the models it replaces are
`extra="forbid"`.

1. **v1 (this ADR):** `exclude_records`, `exclude_tables`, `normalize_ids`.
2. **First consolidation: `unordered`.**
   - A view rule sorts the rows of the listed tables (and, with `path`, a nested
     list).
   - It retires `compare_columns.<table>.<column>.order: unordered`, and #1472's
     `ordered_tables` is expressed as its complement.
   - The loader translates the old flag into the rule, with a deprecation warning.
   - The sort key leaves out the table's unstable fields: the view runs before the
     unstable filter, so sorting on the full row would key on generated ids. This
     is exactly the core-side difference above.
3. **`unstable_fields` as a view rule** (`drop_fields`, nested paths included).
   The fixture stays the authoring surface, and the loader expands it into rules.
4. **The equivalence flags as value-fold kinds.** `mode: subset` stays outside
   the view: it reads the golden, so it is two-sided by nature.

The end state is one declarative place for pre-hash state shaping,
`state_checks.comparison_view`. The older keys become input forms that the
loader translates into it.

### Versioning

`ComparisonViewRecord` is recorded on the grade and in the bundle:

- `version` — the block's schema version;
- `function_version` — a module constant, the version of the composition: the
  order the rules run in, what each is handed and what the record holds;
- `config_sha256` — sha256 of what the rules do: per rule, its `kind`, its
  `VERSION` and its settings that differ from their defaults, `reason` left
  out, as canonical JSON the way `ModelsFingerprint` hashes model data;
- `applied` — per rule and table touched (`exclude_tables` gives one entry per listed table): kind, table, path, rows removed, ids rewritten and references rewritten.

The sha changes when a declaration asks for something else, or when a rule it
names changes what it computes. A new optional field whose default keeps a
rule's behaviour keeps every recorded sha; a change to what a rule computes
bumps the rule's `VERSION`, and a change to the composition bumps
`function_version`; `reason` is prose, not behaviour, and is not hashed. A
registered rule versions itself the same way, so the record of a view built
with it names the implementation that built it.

A grade then says which transform produced the digest it compares. A later
engine can tell whether it would compute the same view. An unknown major
`function_version` in a bundle is refused, as bundle versions are. This is what
a `checks.py` hook cannot provide.

### Wiring

- **Runner.**
  - `RunnerStateChecksConfig.comparison_view`.
  - `_execute_hash_grading` takes the full state of both sides and the initial
    state it already holds (`trial_context.task_description.initial_state`).
  - After `restore_snapshot` it applies the view to each side, then steps 2–5.
  - On a mismatch it computes the raw `state_diff` and a `view_diff`.
  - `HashGradingResult` carries `view_diff` and the record. The wire gains
    `Grade.comparison_view_json`, which carries both, and the bundle records it.
  - A `ComparisonViewError` reaches `GradeTrialResponse(success=False)` →
    `grading_error`.
- **Core.**
  - `StateChecksConfig.comparison_view`.
  - `check_hash` and `check_hash_against_golden_replay` apply the view first,
    with a fresh copy of the initial state. The golden replay mutates the loaded
    initial state in place, so the view needs its own copy.
  - `check_hash` stops folding a `ComparisonViewError` into
    `0.0, "Error computing hash"` and lets it propagate.
- **Accounting.**
  - `key_manifest`: `state_checks.comparison_view`, `CONFIG_INPUT`,
    `BOTH_SCORE_PARITY`, `DIFFERENTIAL_CANONICAL`, listed in the differentials
    outside lock 3.
  - The `_WireKey` row and its doc lock, a version-lock row in `GRADING.md`, and
    the native adapter's translation of the key.
- **The hash / diff parity invariant (#1444)** holds on the view pair: the digest
  is of the view, so the diff that must agree with it is the view diff.

### Tests

- **Both-substrate parity**, in `tests/canonical/test_expected_state_hash_is_not_portable.py`
  beside `test_both_substrates_induce_the_same_equivalence_relation`.
  - For each case, the view applied through the runner's composition and through
    `state_digest` gives the same verdict for the pair, while the two digests keep
    their different algebras.
  - The runner side runs as lock 19 of `test_grading_substrate_parity.py` does,
    with the in-process db-service, or through the composition pulled out into a
    pure function.
- **`normalize_ids` property tests** (hypothesis is a dev dependency;
  `tests/unit/grading/test_hash_verdict_parity.py` is the precedent):
  - distinct records stay distinct;
  - every listed reference follows its record;
  - dangling references are untouched;
  - collisions raise;
  - the view is idempotent and independent of row order;
  - under `scope: new_records` the initial state's records keep their keys;
  - a trial that differs from the golden only in generated ids and their
    references gets the golden's digest, and one that differs anywhere else does
    not.
- **`exclude_records`**: every predicate, `path`, `unless_referenced_by`, and the
  guards.
- **The seam** (`tests/canonical/test_comparison_view_rule_registry.py`): a rule
  a throwaway distribution registers through real entry-point metadata
  resolves, validates its entry, applies, and moves the sha with its `VERSION`;
  an unknown kind is refused naming the registered kinds; a distribution
  registering a built-in's name fails every lookup; each breach of the contract
  is refused.
- **The invariants of the function**: no mutation, determinism, no golden input,
  no digest change without the block.

## Consequences

### Positive

- A pack says once, in data, which records count and what keys them. The
  declaration is versioned with the case, and the grade shows which rules fired
  and both diffs.
- A generated id stops forcing a choice between "mask it and lose the references"
  and "fail the trial".
- The engine gets one place where pre-hash state shaping can converge.
- Both substrates call one function, and a test holds them to it.

### Negative / Trade-offs

- A view is one more thing a pack can get wrong. The cost is bounded: the block
  is validated at load, and every application is in the grade.
- A declared view moves the runner from the server-side stable hash to a
  client-side fetch of the full state, the path `compare_columns` already takes.
- Until the consolidation steps land, a pack can shape the state in two places:
  the view and the older keys. The documented order says which runs first.
- A registered rule is trusted with the verdict. The seam states the boundary
  and records the rule's identity; it cannot judge whether a rule is sound.
- Five measured passes need computed rules. A pack's distribution can register
  them; on the runner they grade only where that distribution is installed in
  the runner image.

### Follow-ups

- A nested path in `unstable_fields.field_name` (1 of the 16 measured passes),
  and refusal of a dotted name that matches nothing — a change to that mechanism,
  in its own PR.
- The consolidation steps 2–4.
- Golden variants (#612).

## Links

- Realizes: [ADR-0011](0011-seam-and-declaration-conventions.md).
- The same entry-point idiom: [0049](0049-judgekind-registry-consolidation.md)
  (judge kinds), [0050](0050-agent-loop-protocol-and-registry.md),
  [0051](0051-user-simulator-protocol-and-registry.md),
  [0052](0052-search-backend-protocol-and-registry.md) (in review, #1672).
- Related code:
  - `tolokaforge/core/hash.py`: `compute_stable_hash`,
    `apply_compare_columns_pipeline`, `filter_unstable_fields`;
  - `tolokaforge/core/grading/state_checks.py`: `state_digest`, `check_hash*`;
  - `tolokaforge/runner/service.py`: `_execute_hash_grading`;
  - `tolokaforge/runner/models.py`: `RunnerStateChecksConfig`, `HashGradingResult`;
  - `tolokaforge/core/models/task_config.py`: `StateChecksConfig`;
  - `tolokaforge/core/grading/key_manifest.py`;
  - `tolokaforge/core/plugin_registry.py`: `load_comparison_view_rule`,
    `available_comparison_view_rules`;
  - `tolokaforge/core/grading/checks_interface.py`, the `checks.py` hook.
- Tests: `tests/canonical/test_expected_state_hash_is_not_portable.py`,
  `tests/canonical/test_grading_substrate_parity.py`,
  `tests/unit/grading/test_hash_verdict_parity.py`.
- Docs: `docs/GRADING.md` § Hash-Based Grading, § Hash / diff verdict parity;
  `docs/CONFIG.md`; `docs/TASKS.md`.
