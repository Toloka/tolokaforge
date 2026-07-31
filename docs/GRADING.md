# Grading System

Tolokaforge evaluates agent performance across four dimensions:

1. **State Checks** - Final environment state verification (hash-based or JSONPath)
2. **Transcript Rules** - Process constraints (required phrases, tool usage, turn limits)
3. **LLM Judge** - Per-criterion rubric grading by a read-only agentic judge
4. **Custom Checks** - Author-written Python `@check` functions for the
   deterministic-Python gap the other three don't express (arithmetic
   over final state, transcript patterns tied to computed values). See
   [custom_checks.md](custom_checks.md).

Scores are weighted and combined into a final score. See [REFERENCE.md](REFERENCE.md) for `grading.yaml` schema.

---

## Substrate Parity

Two substrates grade a trial. The **core** substrate is the in-process
`GradingEngine` (`tolokaforge/core/grading/combine.py`) used by `validate`, the
`NativeAdapter` helpers and the test suite. The **runner** substrate is the gRPC
`GradeTrial` path (`tolokaforge/runner/service.py`), which is what production
runs use. A key an author writes in `grading.yaml` reaches the runner only
through `NativeAdapter.to_task_description`, so a key the translation misses is
a key that silently scores nothing in production while still grading locally.

[`tolokaforge/core/grading/key_manifest.py`](../tolokaforge/core/grading/key_manifest.py)
is the single source of truth for which substrate consumes which key. Every
author-facing key is one `GradingKey` entry declaring three axes:

- **`kind`** — `SCORED_CHECK` (produces a component score), `CONFIG_INPUT`
  (shapes another check; no score of its own), `AGGREGATION` (combines component
  scores). Only `SCORED_CHECK` keys have a violating trajectory, so only they can
  be differentially tested.
- **`coverage`** — `BOTH_SCORE_PARITY` (both substrates consume it and produce the
  same component score), `BOTH_SIGNAL_PARITY` (both consume it and both
  discriminate; the magnitudes differ because the two substrates aggregate
  differently), `CORE_ONLY`, `RUNNER_ONLY`. Anything other than a `BOTH_*` value
  requires a written `reason`.
- **`enforcement`** — how strongly the coverage claim is proven.
  `DIFFERENTIAL_CANONICAL`: a satisfying/violating pair moves both substrates'
  scores in-process. `DIFFERENTIAL_INTEGRATION`: the differential needs real
  services, and `enforcing_test` names the integration test that runs it.
  `FIELD_RESOLUTION_ONLY`: only "the field exists and resolves" is proven.

[`tests/canonical/test_grading_substrate_parity.py`](../tests/canonical/test_grading_substrate_parity.py)
makes the manifest load-bearing. Adding a grading field to either substrate's
config model without a manifest entry fails that suite naming the field; a scored
key that claims both substrates at `DIFFERENTIAL_CANONICAL` must move both
substrates' component scores against
[`tests/data/grading_parity/`](../tests/data/grading_parity/) fixtures; every key
both substrates declare must survive adapter translation non-default; and every key
the runtime ledger checks must resolve to a field on the runner config **and** be
claimed by one of the ledger's recording sites, so a key no site records fails the
suite instead of failing every `GradeTrial` that carries it.

### The runtime ledger

The canonical suite guards the *config models*; the runner guards each individual
request. Through the component phase `GradeTrial` records, at every point an
evaluator is invoked or deliberately skipped, which author key that call accounts
for. Each record is a `KeyAccountingRecord` — an outcome of `EVALUATED` or
`SKIPPED` plus, for a skip, the `detail` a task author reads. It then subtracts
those records from the scored keys the request's grading config actually populated
([`tolokaforge/runner/grading_ledger.py`](../tolokaforge/runner/grading_ledger.py)).
A non-empty remainder means a key would have scored nothing, so the RPC returns
`success=False` naming each key and the runner evaluator its manifest entry
expects — never a grade, and never a `0.0` folded into the combine. A key the
manifest declares `CORE_ONLY` that nonetheless arrives populated fails the same
way, quoting that entry's `reason` — unless a recording site claims it as a
standing skip, per **Every skip is recorded, not silent** below.

Three properties keep the ledger from rejecting configs that grade correctly:

- **It covers `kind: SCORED_CHECK` only.** `CONFIG_INPUT` keys (`id_fields`,
  `relaxed_validation`, `numeric_string_fields`) shape another check rather than
  producing a component, and `AGGREGATION` keys are the combine itself, so neither
  is ever evaluated in the component phase.
- **A key counts as populated only when it is non-empty.** An explicitly written
  `disallowed_tools: []` is indistinguishable from unset, and either way has
  nothing to evaluate.
- **Every skip is recorded, not silent.** `transcript_rules` is skipped when a
  trial has neither messages nor tool history, `llm_judge` when it has no
  messages, `custom_checks` when the pack wrote the block but left
  `enabled` off, and the `state_checks.hash` members the runner's hash evaluator
  reads when `hash.enabled` is not set. `state_checks.hash.expected_state_hash` is a
  standing skip: it is declared `CORE_ONLY` because no runner path reads it (#693),
  so it is recorded as such whether or not hash grading ran — folding it into the
  family's outcome would report a silently dead key as scored. Each skip records
  its reason, which appears in `grade.reasons` whenever the skipped key was
  populated: a degenerate trial scores badly rather than erroring the RPC, but the
  reason it scored badly is visible.

`grading_method: test_execution` returns before the component phase, so the ledger
does not apply to that dispatch mode — recorded as the `grading_method` entry's
declared `reason`.

### Single-substrate keys

| Key | kind | coverage | enforcement | Why only one substrate | Tracked |
|---|---|---|---|---|---|
| `combine.method` | `AGGREGATION` | `RUNNER_ONLY` | field resolution | the core engine always computes a weighted average and never reads the key, so `method: all_pass` scores 0.5 core-side and 0.0 runner-side for the same components | #692 |
| `state_checks.hash.expected_state_hash` | `SCORED_CHECK` | `CORE_ONLY` | field resolution | translated onto the runner's `expected_hash` field, which no runner code path reads — runner hash grading always recomputes a golden hash from `golden_actions` | #693 |
| `state_checks.hash.weight` | `CONFIG_INPUT` | `CORE_ONLY` | field resolution | core blends the hash score against the jsonpath score by this weight; the runner multiplies the two and has no weight concept | #686 |
| `state_checks.db_probes` | `SCORED_CHECK` | `RUNNER_ONLY` | integration differential | the probe DSN resolves only inside the task's docker network, which the runner joins and the host-side core engine does not | architectural |
| `llm_judge` | `SCORED_CHECK` | `RUNNER_ONLY` | integration differential | the rubric judge runs runner-side on the shared `ToolCallingLoop`; the core engine deliberately leaves the component unset | architectural |
| `grading_method` | `AGGREGATION` | `RUNNER_ONLY` | field resolution | a runner-side dispatch selector with no `grading.yaml` counterpart; the dispatch returns before the component phase | architectural |

Architectural entries can never be both substrates and carry no tracking issue.
Every other row is drift and names the issue that closes it. The exemption sets
live in the test module, not beside the manifest, so widening one is an edit a
reviewer sees in the same commit.

The `state_checks.hash` family (`hash`, `hash.enabled`, `hash.golden_actions`)
claims both substrates at `FIELD_RESOLUTION_ONLY`: the runner's evaluator drives
db-service over HTTP, so no service-free differential exists (#687). Mocking the
DB client to make the canonical guard pass would defeat the guard.

### What the guard cannot see

`model_fields` introspection enumerates typed config fields, and all four core
grading models are `extra="ignore"`. So `state_checks.hash.*`, the
`state_checks.jsonpaths[*]` operator vocabulary, and `custom_checks.*` internals
are structurally outside the enumeration — the manifest records nested dict keys
as **declared data**, verified only to live inside a dict-typed field. And a green
parity suite proves each key *discriminates*, not that its discrimination is
*correct*.

It also cannot see a key the two substrates read from **different evidence**. The
manifest freezes config keys and field paths, not evaluation sources, so
`transcript_rules.required_actions` passes every lock while core evaluates it from
`trajectory.messages` and the runner evaluates it from the tool-call record — see
[Both substrates consume it](#both-substrates-consume-it).

### The recorded tool calls both substrates read

Both substrates record every tool call as a `RecordedToolCall`
(`tolokaforge/runner/models.py`, re-exported from `tolokaforge.core.models`), so a
check over tool calls sees the same fields whichever substrate grades it:

| Field | Meaning |
| --- | --- |
| `call_id` | the provider's tool-call id — the only key that joins a call to its result |
| `sequence` | trial-wide, 0-based, execution order across **every** executor |
| `tool_name` | the tool the call named |
| `arguments` | the arguments the caller passed, verbatim |
| `executor` | `agent` or `user` (`ToolExecutorIdentity`) |
| `status` | how the call ended (`ToolExecutionStatus`) |
| `output` | the tool's output, untruncated — or the failure text on a failed call |
| `latency_seconds` | wall time measured by the recording caller |
| `timestamp` | when the call was recorded |

One recorder per trial owns the list and stamps `sequence` at append time, so
interleaved order across executors is correct by construction rather than
reconstructed afterwards. Calls the executor **refuses** — an unknown tool name,
a schema-violating or unparseable argument — are recorded too, carrying the
rejection's own status: the transcript gets a `role: tool` error message for them
either way, so a record that omitted them would read as a call the agent never
attempted.

Two properties are worth reading carefully:

- **`output` on a failed call is the executing layer's failure text, not the
  tool's.** It is also not the text the `role: tool` message carries — the
  message view and the record view word a failure differently. A `result` matcher
  combined with `status != success` is matching harness text.
- **`arguments` are never rewritten.** They are the grader's input; see
  [`docs/SECURITY.md`](SECURITY.md#tool-call-arguments).

**One status value is not producible on every path.** `SUCCESS`, `ERROR`,
`TOOL_NOT_FOUND` and `INVALID_ARGUMENTS` come from four distinct branches of the
in-process executor and are recorded on both substrates. `TIMEOUT` is produced
only on the runner substrate, because the pure in-process executor implements no
timeout at all — `ToolPolicy.timeout_s` is declared and never read (#691). That is
a missing *feature*, not a recording gap: there is no behaviour to record, so no
check loses signal it would otherwise have had.
[`tests/canonical/test_tool_execution_status_reachability.py`](../tests/canonical/test_tool_execution_status_reachability.py)
drives a real recording path for every member, so the vocabulary cannot grow a
value no run produces.

`executor: user` is unreachable in every run today, **equally on both
substrates**, because no code path constructs a user-side tool executor (#688).
An unreachable-everywhere value is a scope limit, not substrate drift.

### How the trial ended

Both substrates give grading the trial's `TerminationReason`: the core substrate
reads `Trajectory.termination_reason`, and the host sends the same value on
`GradeTrialRequest.termination_reason`. It exists so grading can tell a
deliberate finish (`agent_done`) from an exhausted turn budget (`max_turns`) —
the same score means something different in each case.

**It is grading input, not an author-matchable key.** There is no `grading.yaml`
field for it and no key-manifest entry, so no task can score itself on it. That
is deliberate: a task's score must depend on what the agent did, not on how the
harness or the provider happened to stop the run, and a matcher on the
termination reason would let a task pass or fail on infrastructure weather.

Only `agent_done`, `user_stop` and `max_turns` reach the runner. Every other
reason describes a trial the host grader resolves itself, without an RPC — see
[`docs/GRPC_PROTOCOL.md`](GRPC_PROTOCOL.md#gradetrialrequest).
[`tests/canonical/test_termination_reason_reachability.py`](../tests/canonical/test_termination_reason_reachability.py)
drives a real termination path for every member of the enum, so a reason that no
run can produce cannot be introduced, and pins which of them reach `GradeTrial`.

---

## Trial event timeline

A trial leaves two records of itself, and neither alone is gradeable. The
**message view** — assistant and user turns, each tool call carried on the
message that requested it — says what the agent asked for but knows no status,
latency or executor identity. The **tool-call record** says what happened when
each call ran but has no conversation.

[`build_trial_timeline`](../tolokaforge/core/grading/trace_timeline.py) joins them
into one ordered tuple of `TraceEvent`:

```python
def build_trial_timeline(
    messages: Sequence[Message],
    recorded_calls: Sequence[RecordedToolCall],
    termination_reason: TerminationReason | None,
) -> TrialTimeline
```

It is a pure function — no services, no I/O — over three inputs both grading
substrates already hold, which is what makes a check over the timeline mean the
same thing whichever substrate grades the trial:

| Argument | Runner substrate | Core substrate |
| --- | --- | --- |
| `messages` | `decode_transcript_wire(llm_messages_json)` | `trajectory.messages` |
| `recorded_calls` | `trial_context.tool_call_history` | `trajectory.tool_log` |
| `termination_reason` | `GradeTrialRequest.termination_reason` | `trajectory.termination_reason` |

### Both substrates consume it

Every transcript rule is evaluated off the timeline on both substrates. The
runner builds it once in `GradeTrial`, before any grading component runs, and
`evaluate_transcript_rules(timeline, rules)` decomposes the author's config
against it. The core `GradingEngine` builds it from the trajectory and
`TranscriptChecker.grade(timeline, …)` reads the same events. The call/result
join and the assistant-turn view are shared accessors on the timeline module
(`attempted_calls`, `assistant_texts`), so the two substrates cannot drift into
reading one timeline differently.

**A reconciliation failure fails the RPC.** `TimelineInconsistencyError` from
either builder call is never folded into a score. Runner-side `GradeTrial`
returns `success = False` with the offending `call_id` in the error and no
`Grade` at all; core-side the exception propagates. A trial whose transcript and
tool-call record disagree would otherwise grade as though the calls it made never
happened — a `0.0` reported against evidence that was never read.

**One key is still evaluated from different sources.** The core engine evaluates
`transcript_rules.required_actions` and `transcript_rules.communicate_info`
through `ActionEvaluator` / `CommunicateEvaluator` over `trajectory.messages`,
outside `TranscriptChecker` and therefore off the timeline; the runner evaluates
`required_actions` from the timeline's records. Both substrates read the key and
both discriminate it, so the manifest's parity claim holds — but the *evidence*
differs, and the manifest freezes config keys, not evaluation sources. Closing
#685 should unify the source as well as the averaging.

### The event

One flat `TraceEvent` type carries all four kinds — `assistant_message`,
`user_message`, `tool_call`, `tool_result` — so a matcher is a conjunction of
field predicates with uniform field access. **`None` means the field is either
inapplicable to the kind or unrecorded, and a predicate over a `None` field is
unmatched, never vacuously true.**

Unrecorded is the second case and it is not rare: `executor`, `status`, `result`
and `latency_seconds` are `None` on every event of a bundle-sourced timeline
(G6b), and on any call that never ran (G4). So `status != success` matches nothing
at all on such a timeline rather than matching everything — read `records_present`
before trusting either answer. Per-field detail is in the table below; G4 and G6b
say when each field goes missing on a kind it does apply to.

| Field | Kinds it applies to | Meaning |
| --- | --- | --- |
| `position` | all | dense, 0-based index into `events` |
| `turn_index` | all | 0-based index of the assistant generation the event belongs to |
| `kind` | all | `TraceEventKind` |
| `text` | `*_message` | the message text as the wire carries it |
| `call_id` | `tool_call` / `tool_result` | the provider's tool-call id — the join key |
| `tool_name` | `tool_call` / `tool_result` | the tool the call named |
| `executor` | `tool_call` / `tool_result` | `ToolExecutorIdentity`, from the record |
| `arguments` | `tool_call` | the arguments the caller passed, verbatim |
| `status` | `tool_result` | `ToolExecutionStatus`, from the record |
| `result` | `tool_result` | the recorded output, untruncated |
| `latency_seconds` | `tool_result` | wall time measured by the recording caller |

`turn_index` is the assistant *generation* an event belongs to. Every event one
assistant message emits — the message, the tool calls it requested, the results
they produced, and the user message that answered it — carries that generation's
index, so "in the same turn" means "in the same assistant generation". The
initial user prompt precedes the first assistant message and carries index 0.

### Guarantees

- **G1 — message order is authoritative.** Event order follows `messages` order,
  and `position` is dense: `events[i].position == i`.
- **G2 — `turn_index` counts assistant generations**, per the paragraph above.
- **G3 — a call and its result are joined by id, and uniqueness is enforced.**
  Each `tool_call` has at most one `tool_result` with the same `call_id`, at a
  later `position`. A `call_id` occurring twice raises
  `TimelineInconsistencyError` naming both positions rather than picking a
  winner: two calls to one tool with identical arguments differ only in the id, so
  a collision makes the join ambiguous, and an ambiguous join is a broken
  invariant rather than task data.
- **G4 — an attempted call is always an event, and "attempted" is not
  "executed".** A `tool_call` is **never** dropped, because dropping one makes an
  `absent` or `count` constraint wrong in the agent's favour. Three states:
  - *Never attempted.* Termination is decided before a turn's calls execute, so a
    terminating turn's calls reach the message view and never run. Emitted as a
    `tool_call` with no `tool_result` and `status = None`.
  - *Attempted and rejected.* An unknown tool name or schema-invalid arguments are
    recorded, so the call emits a normal pair carrying `tool_not_found` /
    `invalid_arguments` and a `status` matcher counts it.
  - *`trial_not_found`.* Unrecordable — there is no trial context to record into —
    so it emits a `tool_call` with no `tool_result`, indistinguishable from the
    never-attempted case. Declared rather than implied; it is a harness fault for
    which no grading verdict is meaningful.
- **G5 — `result` and `status` come from the record, not the message.** The two
  views word the same failure differently: the `role: tool` message carries
  `Error: <error>`, while the record carries the executing layer's own text,
  untruncated. The record wins. The two substrates also word an executor-level
  failure differently from each other, so a `result` predicate combined with
  `status != success` is matching harness text and is not substrate-portable;
  match on `status` instead.
- **G6 — records-only is a declared input state.** Hash-only grading legitimately
  omits the transcript, and `role: system` messages are not events (N3), so an
  input carrying no assistant or user turn is built from the records alone:
  `tool_call` + `tool_result` pairs in `sequence` order, all at `turn_index` 0,
  `message_view_present = False`.
- **G6b — messages-only is the normal state for a recorded bundle.** `tool_log` is
  not written to `trajectory.yaml`, so a timeline rebuilt from a bundle has no
  records: `records_present = False`, every `tool_call` is unpaired, and
  `executor` / `status` / `result` / `latency_seconds` are `None` throughout.
- **G7 — reconciliation failure is loud.** When a message view is present, every
  record must be linkable by `call_id` to a call in it. An unlinkable record
  raises `TimelineInconsistencyError` naming its `call_id`, `sequence` and
  `tool_name`: the two views disagreeing about one trial is a harness bug, and
  grading around it would be exactly the silent degradation
  [AGENTS.md](../AGENTS.md) core rule 1 forbids.
- **G8 — within a turn, executed calls follow recorded execution order.** The
  `tool_call`s of one assistant message are emitted in ascending
  `RecordedToolCall.sequence` — execution order, since `sequence` is stamped at
  execution time — with calls that never executed after them in declaration
  order. So an "immediately before" or "nothing between" constraint rests on a
  guarantee rather than on a coincidence.

Both degenerate states are reported on the timeline, never inferred: a constraint
that reads a field the missing view supplies must become a **named failing
sub-check, not a silent pass.**

The tool-expectation checks on both substrates honour that by gating on
`records_present`, not on `status is None`. Those two are indistinguishable per
call — a terminating turn's declared call and a bundle-sourced call both carry no
status — so the flag is the only thing that says whether "no record" is a fact or
an absent view. A tool the message view never declared still passes a
`disallowed_tools` check with no records present, because a record can only name a
declared call (G7): the message view alone proves that tool never ran.

### Non-guarantees

- **N2 — no user-executed tool events occur today.** The builder emits
  `executor: user` whenever a record carries it, but no code path constructs a
  user-side tool executor (#688), so the vocabulary is defined and unreachable.
  A user-simulator call also emits no `role: tool` message — the result is inlined
  into the user message text — so such a call pairs with a `tool_result` built
  from the record and never from the message view.
- **N3 — `role: system` messages are not events.** The loop appends termination
  and max-turns notices as system messages, and the transcript wire prepends the
  agent's policy as one. They are harness text, not agent or user behaviour;
  making them matchable would let a task grade itself on harness strings.
  `TrialTimeline.termination_reason` is the typed channel for the same
  information.
- **N4 — message text is the wire text.** `content_blocks` (screenshots),
  `reasoning` and per-message timestamps are not on the timeline. A
  screenshot-only turn carries `text = ""`.
- **N5 — `timeout` is unproducible on the pure in-process path**, because that
  executor implements no timeout at all (#691). `status` itself is present on
  every recorded call on both substrates.
- **N6 — the timeline says what happened, not whether it was correct.** A green
  timeline is not a correctness proof; that is each task's grading config's job.
- **N7 — `TraceEvent` is not hashable.** `arguments` is a dict on every
  `tool_call`, even an empty one, so a generated hash would raise for that kind and
  succeed for the others — `set()` / `Counter()` over results working while the
  same code over calls raised. `__hash__` is `None` so it fails uniformly at the
  first use. Key on `position` (unique per event) or `call_id` (unique per call).
  Equality is unaffected.

[`tests/canonical/test_trace_timeline_substrate_parity.py`](../tests/canonical/test_trace_timeline_substrate_parity.py)
drives one scripted tool-call sequence through each substrate's real recording
path and compares the resulting events field by field, so a divergence in either
substrate's recording fails there. `latency_seconds` is excluded from that
equality — two substrates cannot measure the same wall time — and is instead
asserted to be a positive float on both.

---

## Hash-Based Grading (Tau-Bench Compatible)

Hash grading canonicalizes the final state and the golden state, hashes both
with SHA256, and passes iff the two hashes match. Grading is **engine-vs-engine**:
the golden hash is (re)computed live by replaying the golden actions, not read
from a stored literal (see the caveat below).

### Algorithm

Scalars pass through `canonical_number()` so a pure numeric-representation
difference is not graded as a state change. Two tiers:

- **Numeric TYPES** (`int` / `float` / `Decimal`) always fold:
  `72 == 72.0 == Decimal("72.00")`. Generic and safe (the type declares
  number-ness). On by default.
- **Numeric-looking STRINGS** (`"130.00" == "130.0"`) fold ONLY for values under
  a record key listed in `state_checks.numeric_string_fields` (see below).
  Per-field, because a string that merely looks numeric can carry meaning in its
  exact form (versions `"1.10"` vs `"1.1"`, codes, zero-padded ids).

```python
import hashlib
from tolokaforge.core.grading.state_checks import to_hashable, consistent_hash

# to_hashable(item, string_fields=None) sorts dict keys, canonicalizes numbers
# via canonical_number, and is key-aware for the string tier: a value folds
# numeric strings only when its immediate record key is in string_fields.
# consistent_hash(value) = sha256(str(value)).

# Usage (types-only folding, the default):
# golden_hash = consistent_hash(to_hashable(final_state))
# Usage (also fold numeric strings under the money field):
# golden_hash = consistent_hash(to_hashable(final_state, frozenset(["custom_refund_amount"])))
```

Guards (both tiers): `bool` never folds to `int`; leading-zero ids (`"00123"`)
are never equated with `"123"`; genuinely different numbers stay different; a
genuine string that begins with the reserved numeric-token prefix is escaped so
it cannot masquerade as a number.

> **Hash-algorithm change (recompute stored hashes).** `to_hashable` now applies
> `canonical_number`, so it produces different digests than the pre-canonicalization
> version for any numeric-bearing state. Because grading recomputes the golden
> hash live (golden-action replay via `compute_tau_style_expected_hash`), this is
> symmetric and safe. But any **externally pre-computed** `expected_state_hash`
> stored from before this change is stale and will false-fail — recompute it.
> (Scanned at time of writing: `0` task-pack grading configs store a hash literal,
> so there is nothing to migrate in-tree.)

### Computing Golden Hashes

```python
# 1. Initialize environment
env = Environment(initial_state="task_initial.json")

# 2. Execute ground-truth actions
env.update("$.reservations", value={"id": "R123", "status": "confirmed"})

# 3. Compute hash
from tolokaforge.core.grading.state_checks import to_hashable, consistent_hash
golden_hash = consistent_hash(to_hashable(env.dump()))
```

### Folding numeric strings for a money / quantity field

Some backends round-trip `Decimal` columns as strings, so the same amount can
surface as `"130.00"` on one side and `"130.0"` on the other and false-fail a
correct trial. Opt the specific field(s) into string folding — never globally:

```yaml
state_checks:
  hash:
    enabled: true
    weight: 1.0
  numeric_string_fields:      # per-field allow-list; matched by record key at any depth
    - custom_refund_amount    # e.g. the d365 travel refund field
```

Only list fields that are genuinely numeric quantities. Do NOT list identifier
or code fields (`payment_method_last4`, `id`, `organization_id`): folding those
would treat `"0042"`-style values as numbers. Both substrates consume this key
with score parity — see [Substrate Parity](#substrate-parity) for what that
claim covers and how it is enforced.

### Declaring a table's primary key for non-`id` tables

The grader finds and writes records by primary key and assumes the key column is
literally `id`. Tables keyed by something else (e.g. a `<name>_id` column) must
declare it, or upserts/deletes cannot resolve the key. Declare it per table under
`state_checks.id_fields`; a table absent from the map defaults to `"id"`, so
`id`-keyed domains need nothing:

```yaml
state_checks:
  hash:
    enabled: true
    weight: 1.0
  id_fields:                          # per-table primary-key override; absent => "id"
    widgets: widget_id
    line_items: line_id
```

Map keys are the table names as they appear in `initial_state`. This is config
data that travels with the task, so key resolution never depends on reading model
source at runtime (the previous `inspect.getsource`-based guess broke whenever the
domain source was not on disk). A table keyed by neither `"id"` nor a declared
field fails loud at write time with the exact `id_fields` entry to add. The
MCP-subprocess and Tau diff-sync paths (`_sync_mcp_state_to_db`,
`TauSyncToolWrapper._sync_state_changes`) consult the same map, so records with
their key omitted also fail loud instead of collapsing to a single `None` bucket
and silently corrupting the state diff.

The adapter cross-checks the `id_fields` keys against `initial_state.tables` at
task-description build time — a typo names an "unknown table" and the pack fails
loud with the exact remediation (fix the typo, add the table, or opt in below).
Legacy tasks that pre-date the check can downgrade the raise to a warning:

```yaml
state_checks:
  id_fields:
    legacy_widgets: widget_id
  relaxed_validation: true            # temporary — legacy escape hatch only
```

`relaxed_validation` defaults to `false`; new tasks should fix typos rather than
enable it. The runner also runs the same check as belt-and-suspenders for engines
that bypass `NativeAdapter.to_task_description`. Both keys are consumed at load
time / `RegisterTrial` on both substrates rather than in the grade-time component
phase — see [Substrate Parity](#substrate-parity).

**Tables materialized only by `initialization_actions`**: the cross-check reads
`initial_state.tables` (typically populated from `initial_state.json_db`). A
table that first appears only via an `initialization_action` won't be visible to
the check — an `id_fields` entry for such a table needs `relaxed_validation:
true` today. Add the table to `initial_state.json_db` (even with an empty list)
if you want the strict check to accept it.

**Runner-engine version lock**: `id_fields` and `relaxed_validation` are declared
on the runner-side `StateChecksConfig` (`extra="forbid"`), so a new engine emitting
these keys requires a runner image built from the same release. Old engine + new
runner is safe **for this key** (core-side `extra="ignore"`).

**Runner-engine version lock (both directions)**: the runner-side
`StateChecksConfig` is `extra="forbid"` and declares no `env_assertions` field. An
engine older than this release translates `env_assertions` onto that field for
**every** pack carrying a non-empty `state_checks:` block, whether or not the pack
declares the key, and the trial spec crosses the wire as a plain
`model_dump_json()`. So an old engine against a new runner image is rejected at
`RegisterTrial` for *every* such trial, not only for packs that used the key —
`state_checks` requires engine and runner image from the same release in both
directions. (`db_hash_check` was never declared on the runner config at all, so no
engine ever emitted it and it is not part of this lock — a populated
`db_hash_check` is rejected core-side at config load.)

### Best Practices

- Filter non-deterministic fields (timestamps, UUIDs) before hashing
- Prefer golden-action replay over storing a hash literal; if you must store one,
  recompute it whenever the hashing algorithm changes (see the callout above)
- Fold numeric strings per-field (`numeric_string_fields`), never as a global switch
- Declare non-`id` primary keys per table (`id_fields`); leave `id`-keyed tables unset
- Use `relaxed_validation` only as a short-lived escape hatch for legacy tasks
- Combine with JSONPath assertions using `weight: 0.8` for flexibility

---

## Transcript Rules

`transcript_rules` grades the *process* — what the agent said and which tools it
reached for — rather than the final state. Both substrates consume every key in
the block, and both read it off the
[trial event timeline](#trial-event-timeline).

**What a rule can see.** A tool rule sees the calls that reached the substrate: a
call the agent declared on a terminating turn never ran, so it satisfies no
`required_tools` entry and violates no `disallowed_tools` entry. A phrase rule
(`must_contain`, `disallow_regex`, `communicate_info`) sees the agent's own text
runner-side; core-side it also sees the user's turns and the untruncated text
tools returned. Neither substrate can see the harness's `role: system`
annotations — a termination notice cannot satisfy a required phrase (N3).

### `tool_expectations`

Names the tools the agent must use and the tools it must not touch:

```yaml
transcript_rules:
  tool_expectations:
    required_tools: ["db_update"]        # each must have been called successfully
    disallowed_tools: ["bash"]           # none may be called, at any status
```

**One sub-check per declared tool** on the runner path, the same decomposition
`must_contain` and `disallow_regex` get: the component score is the fraction of
sub-checks that passed, and every failure is named in `grade.reasons`. A task
declaring two required and two disallowed tools yields four independent
sub-checks.

**The two lists treat call status differently, deliberately.** A `required_tools`
entry is satisfied only by a call with `status == "success"` — an errored call did
not do the work the author required, the same rule `required_actions` applies. A
`disallowed_tools` entry fails on a call at **any** status, errors included:
attempting a forbidden action is itself the violation, so a `delete_customer` call
that happened to blow up still fails the check.

`extra="forbid"` on the block means a misspelled key (`required_toolz`) fails at
load rather than grading as an empty list.

**Known limitation — a misspelled *tool name* is not caught here.** Grade-time
evaluation cannot tell `required_tools: ["db_updat"]` from "the agent never called
it", and a typo in `disallowed_tools` passes trivially because no call ever matches
it. Validating tool names against the task's declared tool set belongs at load
time and is owned by **#679**; do not read a green `tool_expectations` check as
evidence that the names are spelled correctly.

**Score parity:** signal, not score. Both substrates discriminate, but the core
`GradingEngine` folds both lists into one of four averaged buckets, so a violation
that scores `0.0` on the runner scores `0.75` core-side (#685). Core also ignores
call status. See [Substrate Parity](#substrate-parity).

**Runner-engine version lock**: `tool_expectations` is declared on the runner-side
`TranscriptRulesConfig` (`extra="forbid"`), so a new engine emitting the key
requires a runner image built from the same release — `RegisterTrial` rejects it
otherwise. Old engine + new runner is safe **for this key** (core-side
`extra="ignore"`).

---

## LLM Judge (Rubric Grading)

The `llm_judge` component grades subjective quality against a **structured
rubric** — not a free-text prompt. A read-only agentic judge runs *inside the
Runner* over the trial's final state, scores each criterion independently, and
emits a per-criterion verdict the reviewer can audit.

### Rubric shape

```yaml
grading:
  weights: { state_checks: 0.5, llm_judge: 0.5 }
  pass_threshold: 0.8
  llm_judge:
    rubric:
      reference: |                # optional, author-written ground truth shown to the judge
        Correct refund is $328.50 (base fare minus 24h-cancellation fee).
        Policy requires offering travel credit before a cash refund.
      criteria:
        - id: refund_amount
          description: "Reply quotes the correct refund amount"
          expected: "$328.50"     # optional per-criterion author reference
          kind: binary            # binary (0/1) or graded (0–1 gradient)
          required: true          # failed → rubric fails outright, regardless of others
          weight: 1.0
        - id: tone
          description: "Reply is polite and professional"
          kind: graded
          weight: 0.5
```

### How the judge works

* **A separate, run-level judge model.** The judge model is configured once per
  run under `models.judge` (the run config — sibling to `models.agent` and
  `models.user`), **not** in the per-task grading block. It is independent of the
  agent under test — this prevents self-grading bias and keeps the judge constant
  across agent comparisons — while a provider switch is a one-line run-config edit
  rather than an N-task change. There is **no default and no fallback to the agent
  model**: if a selected task uses an `llm_judge` component but the run config has
  no `models.judge`, the orchestrator aborts the run up front, before any trial
  executes (AGENTS.md rule 1). The judge builds its own LLM client via the agent's
  provider-correct capability path (so tool schemas/calls are correct for any
  provider).
* **Author-written reference channel.** The judge sees only the rubric's
  `reference` and per-criterion `expected` — author-written *for grading*. The
  deterministic oracle (`golden_actions`, `expected_hash`, `jsonpath_checks`) is
  **never** piped to the judge: that would cause path-matching bias and defeat
  path-independence. The judge's input surface is exactly
  `{agent_system_prompt, transcript, rubric, read-only tools, state_diff}`.
* **Harness-owned read-only tools.** The judge gets a fixed read-only allowlist —
  DB reads (`get_db_state` / `query_db`), a KB search mirroring the agent's
  (`search_kb` for rag-service or the reused `search_policy` for TypeSense — see
  *Judge KB faithfulness* below), `read_file` (only when the agent produced a
  workspace), and the rubric-derived `submit_report`. No `write`, no `compute`.
* **Single call, per-criterion output.** The judge inspects the final state, then
  calls `submit_report` once with `{justification, met|score}` for every criterion
  (its arg schema is generated from the rubric). For each criterion the schema
  places the justification **before** the verdict field, so the verdict is written
  after the reasoning (reason-then-answer). Each justification must end with a
  `VERDICT: MET` / `VERDICT: NOT MET` (binary) or `SCORE: <value>` (graded) marker
  line, and the submitted verdict must match it — a missing or contradicting
  marker is rejected (see *Fail-loud* below). The marker is stored verbatim in the
  `criterion_results` justification.

### Judge KB faithfulness

A rubric often says "the response complies with policy X", so the judge must be
able to read the **same knowledge base the agent read** — never a different
corpus, and never none while still scoring policy compliance. The judge's KB
capability is therefore resolved **per-trial to mirror the agent's** (issue #95):

* **rag-service** — when the agent had the rag `search_kb` tool (a
  `RAGSearchToolWrapper` was reconstructed and a rag client exists), the judge
  gets a `search_kb` bound to the **same `rag_client` + `trial_id`**, querying the
  per-trial `/trials/{trial_id}/search` index. Identical retrieval by
  construction: the agent gets hits ⇒ the judge does too; the agent 404s ⇒ the
  judge 404s.
* **TypeSense (`search_policy`)** — when the agent had the read-only
  `search_policy` KB tool (the mcp_core TypeSense connector), the judge reuses
  **that exact reconstructed tool** through a read-only passthrough: same tool,
  query, backend, and ranking. No mcp_core import, no assumptions about
  `search_policy`'s I/O.
* **None** — if the agent had no KB tool, the judge gets none. You cannot
  penalise an agent for information it could not access.

**Disabling knowledge search per task or project.** For a task whose rubric is
fully self-contained, letting the judge pull policy context the author
deliberately superseded is a correctness risk. Set
`grading.llm_judge.customization.disable_knowledge_search: true` (a sibling of
`rubric`) and the judge's tool surface carries **no** knowledge-search tool — the
rag `search_kb`, the `search_policy` passthrough, and any future KB backend are
**removed from the judge's schema, not stubbed**. This is **judge-side only**: the
*agent's* KB tools for the same task are untouched; the runner still resolves the
agent's KB faithfully and the judge withholds it by construction. Every non-KB
read tool (DB reads, `read_file`) is unaffected. The setting is tri-state and
layers project→task — see
[PROJECTS.md](PROJECTS.md#task-override-semantics) and
[CONFIG.md](CONFIG.md#grading-specification-gradingyaml). When absent, behaviour
is exactly as above.

**Seeing which backend was used.** The judge's `reasons` (surfaced into the grade
output's `reasons`) always ends with a `Judge KB: …` note — `Judge KB: search_kb`,
`Judge KB: search_policy`, or `Judge KB: none offered`. When knowledge search was
disabled by config and the agent actually had a KB tool to withhold, the note
reads `Judge KB: none offered (disabled by config)`, distinguishing a deliberate
gate from a rubric that simply needed no KB. The `JudgeResult` also carries the
structured `kb_tools_offered` tuple. This is the visible "graded with / without
KB" signal. "none offered" is **observability, not an error** — we
cannot statically know whether a given rubric needs a KB, so a KB-less judge
still `COMPLETED`; the note simply makes the gap auditable. The judge's own
`judge_trajectory.yaml` records which KB tools it actually *called*.

**Honest limitation.** The `search_policy` reuse path is validated only against a
fake reconstructed tool in unit tests; real TypeSense retrieval is exercised only
in a deployed mcp_core environment (mcp_core is not importable in this repo).
Likewise the mcp_core TypeSense client handle registered at trial setup is not
torn down at cleanup — a documented, bounded pre-existing leak (no confirmable
deregister API in mcp_core's registry); see the runner's `cleanup_trial`.

### Customizing the judge's system prompt

When a pack's grading philosophy needs a different judge voice than the default,
set `grading.llm_judge.customization.system_prompt` (a sibling of `rubric`,
alongside `disable_knowledge_search`) to a full replacement of the judge's
**grading-stance body**. The harness **always appends the enforced marker
contract** — the sentence instructing the judge to end each justification with a
`VERDICT:` / `SCORE:` marker and call `submit_report` exactly once — so a custom
prompt can never silently break `submit_report` validation. The marker is
non-overridable by construction; a custom body cannot drop it.

```yaml
llm_judge:
  customization:
    system_prompt: |
      You are grading a customer-support transcript against the refund policy.
      Reward precise policy citations; penalise unsupported claims.
  rubric:
    criteria:
      - id: cites_policy
        description: "Reply cites the applicable refund clause"
        kind: binary
        weight: 1.0
```

The setting layers project→task: a task-level `system_prompt` overrides a project
default, omitting the key inherits the project value, and a task sets
`system_prompt: null` to reset a project-level custom prompt back to the default.
An empty or whitespace-only string is rejected loudly at load. When absent, the
judge runs with the byte-for-byte default prompt. The full custom text is recorded
in the bundle's `task.yaml.grading_config`.

### Gating the agent's policy out of the judge's evidence

By default the judge's opening-message evidence includes the agent's own policy /
system prompt, so the judge can see the framing the agent operated under. For a
pack whose rubric is fully self-contained, embedding the agent policy can bias the
judge toward the agent's framing or leak instructions that supersede the rubric.
Set `grading.llm_judge.customization.include_agent_system_prompt: false` (a sibling
of `rubric`, alongside `disable_knowledge_search` / `system_prompt`) and the
agent-policy section is **removed from the judge's opening message, not stubbed** —
the judge grades against the transcript, the state diff, and the rubric alone.

This is **evidence gating**, distinct from `system_prompt` (which changes the
judge's own *wording*): it controls what evidence the harness assembles, not how
the judge is instructed to grade. It is **judge-side only** — the agent's own
system prompt and tool surface are untouched.

```yaml
llm_judge:
  customization:
    include_agent_system_prompt: false
  rubric:
    criteria:
      - id: cites_policy
        description: "Reply cites the applicable refund clause"
        kind: binary
        weight: 1.0
```

The setting is tri-state and layers project→task: unset and `true` both include the
agent policy (today's behaviour); `false` omits it; a task sets `true` or `null` to
re-include over a project `false`. When absent, the opening message is byte-for-byte
the default. The effective decision is recorded in `grade.yaml` as
`judge_agent_prompt_included`. See
[PROJECTS.md](PROJECTS.md#task-override-semantics) and
[CONFIG.md](CONFIG.md#grading-specification-gradingyaml).

### Fail-loud: the ERRORED status

If the judge malfunctions — repeated malformed `submit_report` past its retry
budget, turn / wall-time exhaustion, or a crash — it produces **no score** and
marks the grade `judge_status: errored`. It **never** falls back to `0.0` or
`0.5` (AGENTS.md rule 1). An errored `llm_judge` component is left *unscored* and
**excluded from the weighted combine** — it is not read as a zero. Reviewers see
`judge_status: errored` in `grade.yaml`; downstream analytics must branch on it.

A submitted verdict that disagrees with its justification's trailing
`VERDICT:` / `SCORE:` marker (or a justification missing that marker) is a
malformed `submit_report`: the criterion is named and both sides quoted, the judge
is re-prompted, and on retry exhaustion the trial rides the same ERRORED path — an
unverifiable verdict is never accepted as a grade.

The rejection is delivered on the wire as the **tool result** for the rejected
`submit_report` call: the retry sequence answers every `tool_call_id` on the
terminating assistant message with an adjacent `role=tool` result — the
`submit_report` id carries the rejection reason plus the corrective instruction,
and any read/search call the judge emitted in that same turn (never executed —
`submit_report` ends the turn before tools run) carries an honest "not executed"
note. This is a provider-valid tool-call/tool-result cycle, so the re-prompt
gives the judge a genuine second attempt on every provider.

### Required-gate semantics

A criterion with `required: true` is a **pure gate**, and is **excluded from the
weighted average**: if the judge marks it not-met, the whole rubric fails —
`binary_pass` is forced `false` regardless of the weighted score or any other
heavily-weighted component. A high score on the other criteria cannot rescue a
failed required criterion. Conversely, a *met* required criterion contributes
nothing to the score — it only opens the gate. The weighted average (next
section) is computed over the **non-required criteria only**.

If **every** criterion is required (no non-required criteria to average), the
judge score collapses to the gate verdict: `1.0` when all required criteria are
met, else `0.0`.

### The two weighting layers

Weights act at **two distinct levels**, and they compose multiplicatively:

1. **Per-criterion `weight`** (inside the rubric) — sets each criterion's share
   of the **judge component score**. Non-required criteria aggregate as
   `Σ(weight · score) / Σ(weight)` → a single `llm_judge` score in `[0, 1]`.
   Required criteria are gates, not weighted contributors.
2. **`weights.llm_judge`** (top-level `combine`) — scales that whole judge
   component against `state_checks`, `transcript_rules`, and `custom_checks` in
   the final-score formula below.

So a criterion's pull on the final score is `(its weight / Σ judge weights) ×
weights.llm_judge / Σ all weights`. Tune *within-rubric* importance with
per-criterion `weight`; tune *how much grading trusts the judge at all* with
`weights.llm_judge`.

### Pass semantics: `binary_pass` vs graded `met`

* For a **graded** criterion, the judge's `met` flag uses a **0.5 threshold** on
  the criterion `score` — it is indicative ("did this clear the author's bar?"),
  not the authoritative pass signal.
* The **authoritative pass** for the trial is decided by the combine layer:
  `final_score ≥ pass_threshold` **AND** no required criterion gated
  (`not gate_failed`). Per-criterion `met` flags inform the reviewer; they do not
  by themselves decide the trial.

### Output

Per-criterion results, `judge_status`, and the judge's own token usage / cost
land in `grade.yaml`; the judge's full message transcript lands in the sibling
`judge_trajectory.yaml` sidecar (the audit channel for *why* a criterion was
scored as it was). See [OUTPUT_FORMAT.md](OUTPUT_FORMAT.md).

---

## Custom Checks

The `custom_checks` component runs author-written Python `@check`
functions from a pack's `checks.py`. It's the deterministic-Python gap
the other three components don't express: arithmetic over final DB
rows, invariants that span multiple tables, transcript patterns tied to
computed values. Each `@check` returns `CheckPassed` / `CheckFailed` /
`CheckSkipped`; per-check results ride the wire as `CustomCheckResult`
entries and the aggregate `CheckResultSet.aggregate_score` fills the
`custom_checks` component.

```yaml
custom_checks:
  enabled: true
  file: "checks.py"
  interface_version: "1.0"
  timeout_seconds: 30
  weight: 1.0
  fail_on_error: true
```

The full authoring API (`@init`, `@check`, `CheckContext`), the network
doctrine (checks may not initiate network — the runner container's
`no_internet` policy enforces at the container boundary; #673 tracks
per-check sandboxing), and the delivery mechanics (`checks.py` bundled
into `TaskDescription.tool_artifacts`) live in
[custom_checks.md](custom_checks.md). The seam itself is
[ADR-0012](adr/0012-custom-checks-extension.md).
[`examples/native/custom_checks/`](../examples/native/custom_checks/)
is the runnable reference — a ledger-reconciliation task that verifies
`balance == opening + sum(credits) - sum(debits)` and combines that
with `state_checks` under `combine.weights.custom_checks`.

---

## Infrastructure aborts produce no grade

A trial the provider or the substrate killed before the agent could work is not a
task the model failed. It produces **no `Grade` at all** — not a zero, not a
status field — and it is excluded from every rate in `per_task_metrics.json` and
`aggregate.json`.

This is the same rule as the errored judge one level up. An errored `llm_judge`
component is left unscored and dropped from the weighted combine rather than read
as `0.0` (see § Fail-loud: the ERRORED status); a trial that never ran is left
ungraded and dropped from the denominator for exactly the same reason. `Grade.score`
is a required `[0, 1]` float, so a grade for such a trial would have to carry a
number describing work nobody did, and every consumer that reads `.score` without
branching would read that number as a model failure. `Trajectory.grade` is
`Grade | None`: absence is unrepresentable as zero, and a consumer that forgets to
branch fails loudly.

### Which trials are aborts

Exclusion is earned by **typed** evidence. Exactly three termination reasons
qualify, and each is produced from an exception type or an HTTP status rather than
from matching prose against an exception message:

| Reason | Evidence |
|---|---|
| `rate_limit` | `openai.RateLimitError` (which `litellm.RateLimitError` subclasses, so one check covers every provider litellm routes) or `status_code == 429`, found on the exception or on its `__cause__` chain |
| `api_timeout` | `LLMApiTimeoutError` |
| `provision_error` | `ProvisionError` raised by the runtime backend's `provision` / `await_ready` |

Everything else is **counted**, including the cases that look like
infrastructure:

| Reason | Class | Why it counts |
|---|---|---|
| `timeout` | measured | A declared wall-clock budget over agent actions, the same as `max_turns`. A thrashing agent hits it too, and excluding it would make thrashing vanish from the denominator |
| `api_error` | measured | Produced by matching provider names in the message text, which also matches a context-window overflow (agent behaviour) and a 400 from a malformed tool schema (our bug) |
| `error` | harness error | The classifier's fall-through, so usually a defect of ours. Counted — excluding our own bugs would hide them — and reported separately as `harness_errors` so a non-zero count is visible as a run-health signal |
| `stuck_detected` | measured | The agent repeated itself without progress. It auto-fails with `score: 0.0`, and that verdict is correct |

The asymmetry decides every borderline case: misclassifying an agent failure as
infrastructure raises every published number with nothing in the output to show
it, while misclassifying infrastructure as an agent failure lowers them by a
bounded amount that `infrastructure_aborts` makes visible. So an unrecognised
termination reason is counted, and a rate-limit-shaped message with no typed
exception behind it terminates as `error` rather than buying its way out of the
benchmark.

`outcomes_by_reason` records every observed reason with the class it was counted
as, so any of these judgements can be recomputed from a finished run's aggregate
without a rerun. See [`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md:1) § Run-level
metric denominators and [`docs/ANALYTICS.md`](ANALYTICS.md:1) § The denominator:
measured trials.

## pass@k Metrics

Estimates probability that at least 1 of k attempts succeeds.

### Formula

Given `n` **measured** trials with `c` successes:

```
pass@k = 1 - C(n - c, k) / C(n, k)
```

Where `C(a, b)` is binomial coefficient "a choose b". `n` counts the trials that
measured the agent, so an infrastructure abort neither counts as a failure nor
props up the sample size — and one lost trial can therefore turn `pass@5` into
`null`, since five samples are needed to estimate it and four cannot. The run
logs a warning naming each task whose coverage was reduced that way.

### Example

8 trials, 5 passed, 3 failed:

| Metric | Calculation | Result |
|--------|-------------|--------|
| pass@1 | 1 - C(3,1)/C(8,1) = 1 - 3/8 | 0.625 |
| pass@4 | 1 - C(3,4)/C(8,4) = 1 - 0/70 | 1.0 |
| pass@8 | 1 - C(3,8)/C(8,8) = 1 - 0/1 | 1.0 |

### Configuration

```yaml
orchestrator:
  repeats: 8              # Trials per task (must be >= k)

evaluation:
  metrics: [pass@1, pass@4, pass@8]
```

### Aggregation

- **Macro-average**: Mean of pass@k across tasks
- **Micro-average**: pass@k over all trials combined

---

## Substrate Grading (`state_checks.db_probes`)

`db_probes` grade against a task-declared postgres **substrate** directly,
rather than against the agent's own written file or the engine's JSON DB
state service. Each probe connects to a task-local DSN, runs an author-written
read-only `SELECT`, and applies the same JSONPath assertion vocabulary as
`jsonpaths` (`equals` / `equals_ci` / `contains` / `contains_ci`) to the query
result. This is an **independent oracle**: it reads the database through a
least-privilege read-only role, not through the API the agent mutated, so an
API bug cannot mask a grading miss.

```yaml
state_checks:
  db_probes:
    - name: corrective_action_recorded
      dsn: "postgresql://grader:grader_pw@app-db:5432/mfg"
      query: "SELECT reason_code, status FROM corrective_actions WHERE lot_id = 7"
      expect:
        - path: "$.rows[0].reason_code"
          equals: "CAPA-01"
          description: "reason code matches"
        - path: "$.row_count"
          equals: 1
          description: "exactly one corrective action"
      description: "a corrective action exists for lot 7"
```

**Fields:**

- `name` — probe identifier, shown in grade reasons.
- `dsn` — postgres connection string. Use a dedicated read-only role
  (`GRANT SELECT` only) so grading cannot mutate the substrate.
- `query` — a single read-only `SELECT`.
- `expect` — JSONPath assertions evaluated against the probe result.
- `description` — human-readable summary.

**Result shape.** Rows are shaped into
`{"rows": [{col: val, ...}, ...], "row_count": <int>}`, so `expect` paths
address individual rows (`$.rows[0].status`), whole columns
(`$.rows[*].status`), or the count (`$.row_count`).

**Aggregation (two-level).** A probe *passes* iff **every** one of its `expect`
assertions passes; the component score is the **fraction of passing probes**.
A single-probe task therefore scores 0.0 or 1.0.

**Fail-loud.** A connection or query failure is a **failed** probe with an
actionable reason — never a silent pass. The runner image ships `asyncpg`, the
async driver `db_probes` connect with; the runner container joins the task's
docker network, so it reaches the substrate (e.g. `app-db:5432`) at grade time.

`db_probes` is the sole state source for the tasks that use it — it is not
combined with hash or `jsonpaths` checks in the same task. It fills the
`state_checks` component and combines with `transcript_rules` / `llm_judge`
through the normal weighted combine below.

A probe can encode **policy correctness**, not just existence: assert the
specific value a policy selects (`resolution_path == "reschedule"`) rather than
that any well-formed row was written, so an agent that takes a plausible-but-wrong
path grades down even though its row parses. The
[`multi_service_helpdesk_workflow`](../examples/native/multi_service_helpdesk_workflow/)
pack is the adversarial example — three resolution paths look defensible; the
probe passes only for the one the after-hours policy permits.

---

## Score Combination

Final score formula:

```
final_score = (state_score       * W_state
             + transcript_score  * W_transcript
             + judge_score       * W_judge
             + custom_score      * W_custom)
              / (W_state + W_transcript + W_judge + W_custom)

binary_pass = (final_score >= pass_threshold) AND (no required rubric criterion gated)
```

A component that was not evaluated is **excluded** from both the numerator and
the denominator — this includes an `llm_judge` component whose judge ERRORED
(see [LLM Judge](#llm-judge-rubric-grading)): a broken judge is never folded in
as a `0.0`. A `custom_checks`-only pack whose score comes back absent still
fails loud (empty active set with a configured `custom_checks` weight ⇒
`(0.0, False)`, not a silent `(1.0, True)`).

### Weighting Strategies

**Strict deterministic (tau-bench):**
```yaml
combine:
  weights: { state_checks: 1.0 }
  pass_threshold: 1.0
```

**Balanced outcome + process:**
```yaml
combine:
  weights: { state_checks: 0.6, transcript_rules: 0.3, llm_judge: 0.1 }
  pass_threshold: 0.75
```

**Outcome + deterministic-Python check:**
```yaml
combine:
  weights: { state_checks: 0.4, custom_checks: 0.6 }
  pass_threshold: 0.8
```

### Inheriting `combine` from the project

`combine` is optional per task. A task's effective `combine` is the project's
`task_defaults.grading_defaults.combine` with the task's own `grading.yaml.combine`
layered on top: task fields win, `weights` merge key-by-key (a task key overrides
the project's; project-only keys survive), and any field neither layer sets falls
through to the canonical defaults (`method: weighted`, `weights: {}`,
`pass_threshold: 0.8`).

A task that ships no `combine` block inherits the project block whole; a task that
ships a partial block inherits every field it does not set. When the project
declares no `grading_defaults`, a task without `combine` resolves to the canonical
defaults.

```yaml
# project.yaml
task_defaults:
  grading_defaults:
    combine:
      weights: { llm_judge: 1.0 }
      pass_threshold: 0.8

# tasks/long_debugging_session/grading.yaml — overrides only pass_threshold
combine:
  pass_threshold: 0.7
# effective: weights { llm_judge: 1.0 } (inherited), pass_threshold 0.7, method weighted
```

---

## Grading for RL Training

Tasks used for RL training need grading that produces a meaningful signal — not always 1.0 or always 0.0.

### Principles

- **Use `state_checks` (weight 1.0) for deterministic tasks.** State checks are objective and reproducible. They verify that the agent actually changed the environment correctly.
- **Reserve `llm_judge` for genuinely subjective tasks.** An LLM judge giving 0.7 for "attempted the task" masks real failures. Don't use it as padding.
- **CI portability:** the judge model is a run-level role (`models.judge`), so CI can point it at `mock/mock-judge` to run without live judge inference; for real evaluations set `models.judge` to your production judge model. (No per-task edit is needed — switch the whole run in one place.)
- **Check specific values, not just existence.** Assert `equals: "Large (14\")"` instead of just checking the path exists. Assert `equals: "apple_pay"` instead of checking that any payment method was set.
- **Set `pass_threshold` to allow partial differentiation.** With 6 checks at `pass_threshold: 0.8`, an agent that gets 5/6 still passes but scores lower than 6/6. This provides gradient signal.

### Configuration for Strict RL Grading

```yaml
combine:
  weights: { state_checks: 1.0 }
  pass_threshold: 0.8

state_checks:
  jsonpaths:
    - path: "$.db.orders[0].status"
      equals: "confirmed"
    - path: "$.db.orders[0].paymentMethod"
      equals: "apple_pay"
    # ... more specific assertions
```

You can avoid brittle filename assumptions for file-output tasks by using `path_glob`:

```yaml
state_checks:
  jsonpaths:
    - path_glob: "/env/fs/agent-visible/submissions/*"
      contains_ci: "rollback"
```

### Calibration Checklist

1. Run the task 5+ times with the target agent model.
2. **100% pass rate**: Task is too easy. Add requirements, change defaults, remove system prompt hints.
3. **0% pass rate**: Task is broken or impossible. Verify HTML flow manually, check grading assertions match actual data formats.
4. **30-70% pass rate**: Good range for RL training signal.

---

## See Also

- [REFERENCE.md](REFERENCE.md) - Configuration schemas
- [custom_checks.md](custom_checks.md) - Custom Python validation
- [ADR-0012](adr/0012-custom-checks-extension.md) - `CheckExecutor` Protocol seam
- [TASKS.md](TASKS.md) - Task authoring guide with difficulty design patterns
