# Grading System

Tolokaforge evaluates agent performance across five dimensions:

1. **State Checks** - Final environment state verification (hash-based or JSONPath)
2. **Transcript Rules** - Process constraints (required phrases, tool usage, turn limits)
3. **Trace Checks** - Declarative conditions on the trial's event timeline: order,
   scoped absence, counting, and argument-level matching, with alternative routes
   and checks that must hold without being scored. See [Trace Checks](#trace-checks).
4. **LLM Judge** - Per-criterion rubric grading by a read-only agentic judge
5. **Custom Checks** - Author-written Python `@check` functions for the
   deterministic-Python gap the other four don't express (arithmetic
   over final state, transcript patterns tied to computed values). See
   [custom_checks.md](custom_checks.md).

`combine.method` folds the component scores into one score and one pass flag — their
weighted mean, the weakest of them or the strongest, as the pack declares. See
[Score Combination](#score-combination) for the three rules and
[REFERENCE.md](REFERENCE.md) for the `grading.yaml` schema.

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
  scores). The satisfying/violating **pair** sweep selects `SCORED_CHECK` alone,
  because only a scored key has a violating trajectory that moves a *component*.
  A `CONFIG_INPUT` or `AGGREGATION` key still reaches `DIFFERENTIAL_CANONICAL`
  through a differential over what it does govern, each over a
  [`tests/data/grading_parity/`](../tests/data/grading_parity/) pack:
  `state_checks.hash.weight` by sweeping the weight across one pack's two hash
  cases, `combine.method` by re-authoring the method over one pack's split
  components, and `combine.weights` by re-folding one pack's two scored components
  under maps that omit and declare them. All three escape the pair sweep, so a
  frozen set in the test module enumerates every claim it does not reach and
  asserts, per entry, the property that entry's own differential rests on (below).
- **`coverage`** — `BOTH_SCORE_PARITY` (both substrates consume it and produce the
  same component score), `BOTH_SIGNAL_PARITY` (both consume it and both
  discriminate; the magnitudes differ because the two substrates aggregate
  differently), `CORE_ONLY`, `RUNNER_ONLY`. Anything other than a `BOTH_*` value
  requires a written `reason`.
- **`enforcement`** — how strongly the coverage claim is proven.
  `DIFFERENTIAL_CANONICAL`: a satisfying/violating pair moves both substrates'
  scores in-process. `DIFFERENTIAL_INTEGRATION`: the differential needs real
  services, and `enforcing_test` names the test function that runs it as a pytest
  nodeid — `<module path>::<test function>`, resolved by the canonical suite
  against the module's own AST so naming a file that merely contains a test is
  rejected. `FIELD_RESOLUTION_ONLY`: only "the field exists and resolves" is proven.

`trace_checks` is the one component where parity is structural rather than
maintained: both substrates call the same `evaluate_trace_checks` over the same
timeline, so there is no second implementation to keep in step, and both sides of
every entry in the family name that one function. The canonical suite still drives
the two *integration points* — the core engine's `grade_trajectory` and the
runner's `GradeTrial` — against one authored pack, because a substrate can reach a
shared evaluator with a differently translated config, or not reach it at all.

**The family is enumerated at leaf granularity**, one entry per constraint kind
plus one for each per-constraint field, because the ten kinds are independently
implementable: a kind evaluated on one substrate and skipped on the other is the
realistic drift shape, and a single `trace_checks.constraints` entry would not
see it. Each kind owns a fixture pack that must discriminate on both substrates,
so partial per-constraint coverage cannot land green.

[`tests/canonical/test_grading_substrate_parity.py`](../tests/canonical/test_grading_substrate_parity.py)
makes the manifest load-bearing. Adding a grading field to either substrate's
config model without a manifest entry fails that suite naming the field; a scored
key that claims both substrates at `DIFFERENTIAL_CANONICAL` must move both
substrates' component scores against
[`tests/data/grading_parity/`](../tests/data/grading_parity/) fixtures; every key
both substrates declare must survive adapter translation non-default; and every key
the runtime ledger checks must resolve to a field on the runner config, be declared
in `accountable_author_keys()`, **and** have its recording site driven — a real
`RegisterTrial → ExecuteTool → GradeTrial` per key, with the outcome the ledger
reports asserted to be the one the manifest implies. A key whose site is deleted,
downgraded to a skip, or filed as `EVALUATED` over an evaluation that never ran
fails the suite instead of failing every `GradeTrial` that carries it.

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

Every one of those recording sites is *driven*, per key, by the canonical suite —
the hash family, the db probes and the judge included. For each ledger key naming a
runner field, a real `RegisterTrial → ExecuteTool → GradeTrial` populates the key,
lets its evaluator run, and the outcome the ledger reports is asserted to be the one
the manifest implies. Two external services are substituted and nothing else: the
judge's model provider, and the postgres a db probe queries, whose DSN resolves only
inside the task's docker network. Neither stands in for a recording site, an
evaluator's decision, the audit or the combine. A key the manifest enforces at the
integration tier for its **score** is still driven here for its **recording site**;
the two are orthogonal.

Three properties keep the ledger from rejecting configs that grade correctly:

- **It covers `kind: SCORED_CHECK` only.** `CONFIG_INPUT` keys (`id_fields`,
  `relaxed_validation`, `numeric_string_fields`) shape another check rather than
  producing a component, and `AGGREGATION` keys are the combine itself, so neither
  is ever evaluated in the component phase.
- **A key counts as populated only when it is non-empty.** An explicitly written
  `disallowed_tools: []` is indistinguishable from unset, and either way has
  nothing to evaluate.
- **Every skip is recorded, not silent.** The `transcript_rules` keys other than
  `min_assistant_turns` are skipped when the trial's timeline carries no events,
  every `trace_checks.constraints.<kind>` key on that same timeline, `llm_judge`
  when it has no messages, `custom_checks` when the pack wrote the block but left
  `enabled` off, and the `state_checks.hash` members the runner's hash evaluator
  reads when `hash.enabled` is not set. On a timeline that *does* carry events, a
  constraint whose [binder yielded no assignment](#correlating-arguments-across-matchers)
  skips the kinds **nested inside** its `require` tree, which nothing entered; the
  tree's own kind is evaluated, because the constraint takes a verdict under it
  either way. A kind another constraint in the block scored is evaluated too — the
  skip is filed per kind, and a kind that carried a verdict anywhere is not one the
  grade contributed nothing under.
  Each skip records its reason, which appears in
  `grade.reasons` whenever the skipped key was populated: a degenerate trial scores
  badly rather than erroring the RPC, but the reason it scored badly is visible.

  A declared `min_assistant_turns` is the one transcript rule **evaluated** on an
  events-less timeline, because absence is exactly the answer that key asks for: a
  trial that left no trace made no assistant turn. It scores the whole
  `transcript_rules` component `0.0` there and its siblings still record the skip,
  so the component enters the combine rather than dropping out of it. See
  [Turn bounds](#turn-bounds).

  "No events" is narrower than "no messages": `role: system` messages are harness
  annotations and never become events (N3), so a trial whose only messages are a
  termination notice and which made no tool call is skipped despite having
  messages. That is the point — grading a rule against harness text would let a
  task score itself on strings the harness wrote.

**The guarantee is narrower inside a route.** A manifest entry carries one
`runner_field`, and the per-constraint entries address
`TraceChecksConfig.constraints`, so a constraint kind or per-constraint field
written *only* inside an [`alternatives`](#alternative-paths) path is covered by the
`trace_checks.alternatives` key rather than by its own leaf — populated-implies-accounted
holds for the block, not for that leaf (#772). The evaluator's side is unaffected: it
records every kind the walk reached, wherever the constraint was written.

`grading_method: test_execution` returns before the component phase, so the ledger
does not apply to that dispatch mode — recorded as the `grading_method` entry's
declared `reason`.

### Single-substrate keys

| Key | kind | coverage | enforcement | Why only one substrate | Tracked |
|---|---|---|---|---|---|
| `state_checks.hash.description` | `CONFIG_INPUT` | `CORE_ONLY` | field resolution | the runner's flattened hash block declares no description field, so there is nothing on that substrate for the key to resolve against — the wire carries the runner's hash verdict, not the reason text an author writes beside it | architectural |
| `state_checks.db_probes` | `SCORED_CHECK` | `RUNNER_ONLY` | integration differential | the probe DSN resolves only inside the task's docker network, which the runner joins and the host-side core engine does not | architectural |
| `llm_judge` | `SCORED_CHECK` | `RUNNER_ONLY` | integration differential | the rubric judge runs runner-side on the shared `ToolCallingLoop`; the core engine deliberately leaves the component unset | architectural |
| `grading_method` | `AGGREGATION` | `RUNNER_ONLY` | field resolution | a runner-side dispatch selector with no `grading.yaml` counterpart; the dispatch returns before the component phase | architectural |

Architectural entries can never be both substrates and carry no tracking issue.
Every other row is drift and names the issue that closes it. The exemption sets
live in the test module, not beside the manifest, so widening one is an edit a
reviewer sees in the same commit.

The `state_checks.hash` family's members **do not all claim the same coverage**, because
which hash *source* a pack declares decides whether the two substrates compare the trial
against the same expected state:

| Member | coverage | enforcement | Tracked |
|---|---|---|---|
| `state_checks.hash.golden_actions` | `BOTH_SCORE_PARITY` | integration differential | — |
| `state_checks.hash.expect_initial_state` | `BOTH_SCORE_PARITY` | canonical differential | — |
| `state_checks.hash` | `BOTH_SIGNAL_PARITY` | integration differential | — |
| `state_checks.hash.enabled` | `BOTH_SIGNAL_PARITY` | integration differential | — |

None carries a tracking issue of its own: both authorable source shapes are proven, and
the two shapes that are not are ones the authoring gate refuses.

The fold rule is shared on every shape — both substrates call
`compose_state_checks_score` — and so are the *inputs* to it on both authorable source
shapes. They differ only for the two the authoring gate refuses:

- **`golden_actions`** — proven, whenever the task supplies the world the replay needs.
  Both substrates replay the actions and hash the resulting state, so the same trial
  yields the same verdict and therefore the same component. This is the shape the
  `enforcing_test` drives.
- **`expect_initial_state`** — proven, and the shape a refusal task declares. Both
  substrates score one proposition — the trial's final state is the state its task
  started in — each computing *both* sides of the comparison in its own hash algebra:
  core hashes the task's declared `initial_state.json_db`, the runner resets db-service
  and hashes what it restored. That is what a stored digest can never be, the two
  algebras labelling the same state differently (#915) — which is why a hash source
  names a state rather than a digest.
- **`hash.enabled` with no declared source** — not proven, and **refused at the
  authoring gate** for that reason wherever the adapter grading the task reports that
  nothing lies beneath the authored block, which is what `adapter_type: native` means.
  Core produces **no** verdict (below), while the runner runs hash grading anyway for
  the refusal shape and produces a real binary one. Measured, on a pack with live
  assertions scoring `0.5` at `weight: 0.6`: core's component is `0.5` on both hash
  outcomes, the runner's is `0.8` on a match and `0.2` on a divergence. What remains
  reachable is a directly built config, a bundle recorded before the rule, and every
  pack whose adapter answers otherwise. An adapter may compute the source itself, the
  way the frozen-core family replays a golden-actions fixture the authored block never
  names: one reporting a usable source makes the bare block a checked pass, one
  reporting the source missing or empty makes it a refusal naming that fixture, and
  one that answers nothing — including an adapter this environment has not installed —
  leaves the shape reported unchecked, because which substrate reading such a pack
  takes is not settled here.
- **`golden_actions` with no world to replay them in** — not proven, and **refused at
  the authoring gate** for the same reason. Core raises `UnbuildableGoldenReplayWorld`
  and the trial is left unscored (below), while the runner has nothing to lack: its
  replay world *is* the live trial — the tools `RegisterTrial` registered, over
  db-service's state — so it replays, hashes and produces a real binary verdict. What
  remains reachable is a directly built engine and a config no gate saw.

The rows say *signal* parity rather than *score* parity because of the two shapes the
gate refuses: they survive in bundles recorded before the rule, and there each substrate
takes a different component.

**One comparison, one substrate.** Every hash comparison in the system computes both
sides on one substrate: core hashes both operands with `state_digest`
(`consistent_hash(to_hashable(...))`,
[`state_checks.py`](../tolokaforge/core/grading/state_checks.py)), the runner hashes
both with [`compute_stable_hash`](../tolokaforge/core/hash.py). The two algebras share
the folding promise — `numeric_string_fields` behaves identically on both — and nothing
else: they label every state differently, so digests never cross substrates and are
never authored, which is why a hash source names a state rather than a digest. The two
functions are deliberately not unified: `compute_stable_hash` backs persisted digests —
db-service ETags, snapshot hashes, and the `ResetTrialResponse.state_hash` /
`GetStateResponse.stable_hash` wire fields — and core's algebra reproduces the digests
recorded bundles carry, so changing either function invalidates digests that already
exist, while nothing needs a digest to travel between substrates.
[`tests/canonical/test_expected_state_hash_is_not_portable.py`](../tests/canonical/test_expected_state_hash_is_not_portable.py)
locks all three facts: same equivalence relation, a different label on every state, and
no wire route for a digest to cross.

The golden-actions differential runs over real gRPC and a real db-service, in
[`tests/integration/test_docker_grading_hash_composition.py`](../tests/integration/test_docker_grading_hash_composition.py):
a matching and a diverging final state against the same golden replay, at two weights
strictly inside `(0, 1)`, with the wire's `state_checks` component pinned to the blend
and required to differ between the two weights.

**What that proves and what it does not.** The runner's own golden-replay verdict
reaches the shared composer, and the author's `weight` reaches the fold — measured
on the wire: forcing the runner's fold to a constant weight, and replacing it with a
plain product of the two scores, each turn every cell red. What it does not prove is
`state_checks.numeric_string_fields`, which stays `FIELD_RESOLUTION_ONLY` (#687) —
the folding pair is claimed to be honored identically on both substrates and no test
drives it through the runner's hash evaluator. Nor does the canonical suite prove
this test *passes*: it resolves the nodeid and stops there, and `test-gate` does not
fire on a pull request (#700), so this tier is run locally and its output quoted.

**A service-free differential reaches that evaluator.** `_drive_hash_family`
([`tests/canonical/test_grading_substrate_parity.py`](../tests/canonical/test_grading_substrate_parity.py))
grades a hash-enabled trial whose one golden action names a registered tool, replaying
it through an in-process `json_db_service`, and asserts the replay ran whole — so the
runner's evaluator runs against a real database with no service to stand up. Nothing
load-bearing is mocked there: the db-service app is the real one and only the HTTP
transport is a `TestClient`. The hash family's `DIFFERENTIAL_INTEGRATION` rows therefore
name the tier each entry was measured at rather than the strongest tier reachable for
it, and #687 owns re-measuring them against that path.

**Coverage and enforcement are orthogonal on purpose**, which is what lets a true
coverage claim carry weak enforcement. `state_checks.hash.golden_actions` is the
example: `BOTH_SCORE_PARITY` states what is the case — both substrates produce the
same component score — and `DIFFERENTIAL_INTEGRATION` states how strongly that is
proven. Weakening a *true* coverage claim to `BOTH_SIGNAL_PARITY` to signal thin
enforcement would make the manifest say something false in order to avoid saying
something weak; the enforcement axis is where the weakness belongs, and it says so.
The two `BOTH_SIGNAL_PARITY` rows above are the opposite case — there the score
claim itself is false for a source shape, so the coverage axis is the honest place
for it. `state_checks.db_probes` sits at the same enforcement tier.

`state_checks.hash.weight` is proven at `DIFFERENTIAL_CANONICAL` by a composition
sweep in the same suite: a fixture pack configuring both state sources — an
`expect_initial_state` comparison and two `$.db.…` assertions of which one holds —
is graded on both substrates at four weights spanning `(0, 1)`, and each composite
is pinned to `jsonpath_score * (1 - weight) + hash_score * weight` computed by the
test rather than compared only against the other substrate. Cross-substrate equality
alone would prove nothing: both substrates call one composer, so they agree by
construction even if that composer ignored its arguments.

That differential is a `CONFIG_INPUT` key, and the lock that runs the
`DIFFERENTIAL_CANONICAL` fixtures selects `SCORED_CHECK` keys only, so the claim
would otherwise be reached by no lock at all. A frozen set in the test module
enumerates every `DIFFERENTIAL_CANONICAL` entry outside that lock's reach, and for
each one asserts the property that entry's own differential rests on. Membership in
the set enforces nothing by itself — a differential deleted wholesale leaves the set
unchanged — so the per-entry clause is what stops the enforcement level from resting
on a citation:

- `state_checks.hash.weight` — the sweep still spans two weights strictly inside
  `(0, 1)`, the weights at which a fold that merely *selects* the dominant source is
  distinguishable from one that mixes them.
- `combine.method` — the method differential's hand-written answer table still
  covers every declared method, with a distinct score each, so an implementation
  returning one aggregation for all three cannot satisfy it. See
  [Score Combination](#score-combination).
- `combine.weights` — the membership differential's weight maps still span both
  sides of the question: a map omitting a scored component, where both folds must
  refuse, and one declaring every scored component, where both must fold. Hollowed
  down to complete maps the refusal is never reached, and hollowed down to
  incomplete ones no fold runs — either way every row would agree, and the
  surviving rows still pass, so this clause is the only thing that sees it. Its
  zero-share table also still answers `all` and `any` differently from `weighted`,
  so the [`weighted`-only scoping](#score-combination) of the zero-total-weight
  rule cannot be widened to every method without redoing it.
- `trace_checks.constraints.weight`, `.on_missing`, `.severity`, `.within`, `.bind` —
  each still names a pack in the parametrisation that drives its differential, so a
  key escaping the scored-key lock without one is caught here. Each pack is authored
  so a build that ignored the field would score its two trials *identically*: the
  weight pack passes one of two differently-weighted constraints in each trial, the
  `on_missing` pack pairs an unmatched anchor against a definite wrong order, the
  `severity` pack fails one scored check in one trial and trips its gate in the
  other so that a build folding both alike scores them equally, the `within` pack
  moves one call in and out of the turn window, and the `bind` pack reads one
  report in both trials and writes back a different one in the violating trial, so
  only the correlation between the two arguments separates them.
- `trace_checks` — the family root declares no field on either substrate, so it
  has no differential of its own; what its enforcement rests on is its leaves',
  and the clause asserts at least one member of the family is still reached by the
  scored-key lock.

Core's verdict there is its own: the fixture declares `expect_initial_state`, so
`check_hash` produces the verdict in process against the hash of the state the task
declares it starts in. **The runner's is handed to its fold rather than produced**,
because the runner's hash evaluator drives db-service over HTTP. That keeps the sweep a
statement about the *fold* — the key is `CONFIG_INPUT` — and not a claim about what
either evaluator returns. The substitution is honest
because the hash verdict either substrate produces is `0.0` or `1.0`, never a
fraction, so the value handed in is one the runner's own path would yield; the same
lock asserts core's evaluator returns exactly those two for the fixture's two states.

**That premise is guarded, within a stated limit.** The producers the manifest names
split by the shape their verdict leaves in. Core's `check_hash` and
`check_hash_against_golden_replay` hand theirs on as a bare float in a tuple, so the
suite reads their sources: each must choose its score between literals rather than
computing it, and every `return` must carry that score somewhere the audit reads. The
runner's `_execute_hash_grading` returns its verdict inside `HashGradingResult`, whose
`hash_score` is derived from the boolean `hash_match` — a non-binary or contradictory
verdict is unrepresentable, so that producer's source needs no audit; the suite proves
the derivation over both `hash_match` values, that constructing the model with an
explicit `hash_score` is refused, and that the producer's declared return type keeps
its verdict inside the model. The producer set is derived from the hash family's
declared evaluators and asserted as set equality against the union of the two frozen
partitions, so a fourth producer forces a reviewable edit rather than landing with the
guard green. What the source audit cannot see is a producer reached only *through* one
of the functions it reads: it follows declared evaluators, not call graphs. So a
partial hash score cannot land inside a guarded producer without the sweep's premise
being re-examined, and a new producer cannot be declared without one.

### Score-parity keys outside the hash family

| Key | kind | coverage | enforcement |
|---|---|---|---|
| `transcript_rules.must_contain` | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `transcript_rules.disallow_regex` | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `transcript_rules.max_turns` | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `transcript_rules.min_assistant_turns` | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `transcript_rules.required_actions` | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `transcript_rules.communicate_info` | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `transcript_rules.tool_expectations` | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `trace_checks.constraints` | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `trace_checks.constraints.<kind>` × 10 | `SCORED_CHECK` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `trace_checks.constraints.weight` / `.on_missing` / `.severity` / `.within` / `.bind` | `CONFIG_INPUT` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |
| `trace_checks` (family root) | `CONFIG_INPUT` | `BOTH_SCORE_PARITY` | `DIFFERENTIAL_CANONICAL` |

`trace_checks` and `transcript_rules` are the two scored families where **every**
member is differentially proven in-process, which is what one shared evaluator buys:
there is no second implementation whose agreement has to be measured, only two
integration points and one pack per leaf. The ten kinds are
`present`, `absent`, `count`, `before`, `immediately_before`, `absent_before`,
`absent_between`, `all_of`, `any_of`, `negate` — the same closed set the evaluator
and the runtime ledger read, asserted equal across all three sources.

**Both families rest on a different mechanism than
`state_checks.hash.golden_actions`'.** That key agrees because both substrates fold
their inputs through one shared composer, so the aggregation itself is common code.
The two shared-evaluator families agree a tier earlier: `evaluate_trace_checks` and
`evaluate_transcript_rules` are each the single implementation both substrates call,
so a component score for a given config and trajectory is identical by construction
rather than by measurement. The parity suite still drives every one of these keys
through both substrates' own paths and asserts the two scores equal — construction is
what makes the agreement true, and the differential is what keeps a second
implementation from reappearing unnoticed. See
[§ Transcript Rules](#transcript-rules) for what each of the seven keys asserts.

Whether `transcript_rules` produces a component at all is shared the same way.
`scored_transcript_rules` decides which rules a trial's timeline carries evidence
for — nothing, on an events-less timeline under a pack declaring no activity floor,
and the floor alone when it declared one — and both integration points fold through
it, so the component drops out of the combine on both substrates or on neither.

### What the guard cannot see

`model_fields` introspection enumerates typed config fields, and the contents of a
**dict-typed** field are values rather than fields. So the
`state_checks.jsonpaths[*]` operator vocabulary and `custom_checks.*` internals are
structurally outside the enumeration whatever their parent model does with an
unknown key. And a green parity suite proves each key *discriminates*, not that its
discrimination is *correct*.

An author key living inside the **elements** of a `list[SomeModel]` field is the
one nested position that is *not* declared data. The field walker treats such a
field as a single leaf — its elements are the shape of one key's value, not
separate keys — so an entry there names the list in `*_field` and a dotted
`*_element_path` walked from the element model: `trace_checks.constraints.before`
is `TraceChecksConfig.constraints` addressed at `require.before`. The canonical
suite resolves every segment against the model the one before it holds, so a path
naming a field of the wrong model fails naming that model. Several entries then
share one field, which is why an element-addressed entry is not counted as
*claiming* it — the claim is the position inside. Reading declaredness off an
authored `grading.yaml` follows the same path and descends through the composite
kinds, so a constraint kind written only inside an `all_of` counts as declared;
descent stops outside a constraint kind, because a matcher's `args` keys are the
author's own argument names.

Nor can a satisfying/violating pair reach a key's **degenerate** boundary, the input
that declares nothing at all: both cells of a discriminating pair have to declare
something, or the pair would not move a component. So `state_checks.jsonpaths` holds
`BOTH_SCORE_PARITY` at `DIFFERENTIAL_CANONICAL` on evidence that says nothing about
an *empty* assertion list, and that boundary carries a differential row of its own —
an empty list leaves the component unscored on both substrates, asserted beside a row
reading the pack's own assertions so an implementation scoring nothing at all cannot
satisfy both. The tier on a row is a claim about the cells some lock reaches, not
about every input the key admits.

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
| `output` | the tool's output, untruncated — or, on a failed call, the tool's own failure text |
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

- **`output` on a failed call is the tool's own failure text**, worded
  identically by both grading substrates — see G5 for the four forms it takes.
  The `role: tool` message carries the same text behind an `Error: ` prefix, so
  the two views still differ by that prefix and the record is the one to read.
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

Every transcript rule is evaluated off the timeline on both substrates.
`evaluate_transcript_rules(timeline, rules)`
(`tolokaforge/core/grading/transcript.py`) is the one evaluator: it takes the
trial's timeline and the validated `TranscriptRulesConfig` — the model, not a
dump, so the two substrates cannot hand it differently-shaped configs — and
decomposes the author's block into one sub-check per declared entry. The runner
builds the timeline once in `GradeTrial`, before any grading component runs, and
calls it there; the core `GradingEngine` builds the same timeline from the
trajectory and calls it in `grade_trajectory`. The call/result join and the
assistant-turn view are shared accessors on the timeline module
(`attempted_calls`, `assistant_texts`), so the two substrates cannot drift into
reading one timeline differently.

One config + one trial therefore reaches one component score whichever substrate
graded it, by construction rather than by measurement. The differential that
drives an authored pack through both substrates' real paths and asserts the two
columns equal is `tests/canonical/test_transcript_substrate_parity.py`.

**A reconciliation failure fails the RPC, and the host does not substitute a
verdict.** `TimelineInconsistencyError` from either builder call is never folded
into a score. Runner-side `GradeTrial` returns `success = False` with the offending
`call_id` in the error and no `Grade` at all; core-side the exception propagates.
On the host, `RunnerRPCTrialGrader.grade` raises `GradingFailedError` for **any**
`GradeTrial` that returns no verdict — reconciliation failure, an undecodable
payload, or an unaccounted scored key. A stand-in `score=0.0` would be worse than
what it replaced: such a trial stays inside the measured denominator, so the zero
would enter `success_rate`, `avg_score`, `pass@k` and `binary_pass` as an agent
failure reported against evidence that was never read.

The consequence is that such a trial is **counted but unscored**. The conductor's
grading phase catches the exception, records the reason on
`Trajectory.grading_error`, and lets the trial finish its normal path: its
`status` and `termination_reason` still describe how the trial itself ended, its
bundle is written with the cause in `trajectory.yaml` and no `grade.yaml`, and it
reaches `total_trials` and `measured_trials` while staying out of `scored_trials`.
The failure is logged at `error` level where it happens.

It is attributed as **ours**, not as the agent's: the trial classifies
`TrialOutcomeClass.UNGRADEABLE`, adds to the `ungradeable` count in
`per_task_metrics.json` and `aggregate.json`, gets its own `outcomes_by_reason`
row keyed `ungradeable_<reason>`, and lands in `failure_attribution.json` as
`failure_class: grading_failure` with `deterministic: true`. It is a non-pass in
`success_rate` and `pass@k`, so a grading regression shows up as a visible,
bounded deflation instead of as a run that got quietly smaller. While the run is
still going, the live panel says the same thing: the trial's row reads `n/a`,
which is a third verdict distinct from both `pass` and `fail`
(see [CLI.md § Live run panel](CLI.md#live-run-panel---displayrich-during-tolokaforge-run)).

Because the attempt terminates normally, it is **not retried** — retryability
reads the trajectory's own status and reason, which grading's failure does not
touch. A grading failure that a second attempt would have got past is therefore
recorded ungradeable on the first: the price of never fabricating a verdict and
never counting one attempt twice.

### The event

One flat `TraceEvent` type carries all four kinds — `assistant_message`,
`user_message`, `tool_call`, `tool_result` — so a matcher is a conjunction of
field predicates with uniform field access. **`None` means the field is either
inapplicable to the kind or unrecorded, and a predicate over a `None` field is
unmatched, never vacuously true.**

Unrecorded is the second case and it is not rare: `executor`, `status` and
`latency_seconds` are `None` on every event of a records-less timeline (G6b),
and on any call that never ran (G4). So `status != success` matches nothing at all
on such a timeline rather than matching everything — read `records_present` before
trusting either answer. `result` is the exception: a bundle keeps the `role: tool`
messages, so even a records-less bundle still says what each tool returned (G6b).
Per-field detail is in the table below; G4 and G6b say when each field goes
missing on a kind it does apply to.

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
| `result` | `tool_result` | what the tool returned: the record's untruncated output, or the answering `role: tool` message's text when there are no records |
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
  - *`trial_not_found`.* The two substrates differ, and the difference is declared
    rather than implied. The runner's own trial-context recorder has no trial to
    record into, so runner-side the call emits a `tool_call` with no `tool_result`,
    indistinguishable from the never-attempted case. **Core-side it is recorded.**
    Since the caller records, `GrpcRunnerClient` builds a `ToolResult` whose status
    the proto does not map (`RECORDED_STATUS_BY_PROTO` has no entry for
    `TRIAL_NOT_FOUND`), and `resolve_tool_status` then resolves a failed result to
    `error` — so the call emits a normal pair and a `status` matcher sees `error`.
    Either way it is a harness fault for which no grading verdict is meaningful, so
    a constraint should not depend on which shape it takes. Whether `error` is the
    right status for a call that never reached a tool is #727.
- **G5 — where both views describe one call, the record wins.** The two views word
  the same failure differently: the `role: tool` message carries `Error: <text>`,
  while the record carries that text alone, untruncated. So `result` and `status`
  are read from the record wherever a record exists. This is a rule about
  precedence between two present views, not about what exists when only one of
  them is — G6b covers that. Both substrates record one text for one failure, in
  one of four forms: the message the tool signalled in `ToolResult.error`; the
  message a raised exception carries, or its class name where it carries none;
  `Tool returned failure with no error message` where a tool failed without
  saying why; and `Tool '<name>' not found` for a call naming a tool the trial
  does not have. No executing layer adds a wrapper of its own — the exception's
  type and traceback stay in that layer's log — so the recorded text is the same
  whichever substrate ran the trial.
- **G6 — records-only is a declared input state.** Hash-only grading legitimately
  omits the transcript, and `role: system` messages are not events (N3), so an
  input carrying no assistant or user turn is built from the records alone:
  `tool_call` + `tool_result` pairs in `sequence` order, all at `turn_index` 0,
  `message_view_present = False`.
- **G6b — messages-only is a declared input state, and its results come from the
  message view.** A trial bundle carries its tool-call record as the `tool_log.yaml`
  sidecar, so a timeline built from `trajectory.yaml` alone — which is also every
  bundle written before that sidecar existed — has no records:
  `records_present = False` and
  `executor` / `status` / `latency_seconds` are `None` throughout. The tool output
  is not lost with them — `trajectory.yaml` keeps every `role: tool` message with
  its `tool_call_id` — so each `tool_call` is paired with a `tool_result` carrying
  that message's text, joined by id and never by position. A failed call's text is
  then the agent-facing rendering — the recorded text behind an `Error: ` prefix —
  which is why G5 reads the record wherever one exists.
  `records_present` therefore means "a record view was supplied", not "results
  exist": a constraint reading `status`, `executor` or `latency_seconds` is still a
  **named failing sub-check** and never a silent pass, while a phrase rule still
  reads what the tools returned. A `role: tool` message answering a call the
  message view does not declare raises `TimelineInconsistencyError` naming its
  index, symmetrically with G7 — that text is the only surviving evidence of what
  the call returned, so it can be neither joined nor dropped. Where records *are*
  present those messages are the shadowed view: neither read nor validated, because
  extending the join's loudness to evidence nothing reads would fail a live grading
  run over a discrepancy no verdict depends on.
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
that reads a field only the missing view supplies must become a **named failing
sub-check, not a silent pass.**

The tool-expectation checks on both substrates honour that by gating on
`records_present`, not on `status is None`. Those two are indistinguishable per
call — a terminating turn's declared call and a call on a records-less timeline
both carry no status — so the flag is the only thing that says whether "no record" is a fact or
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

> **A change to `to_hashable` is symmetric.** Every hash source names a *state* rather
> than a digest, so each substrate computes both sides of its comparison with the same
> function — the golden replay through `StateChecker.check_hash_against_golden_replay`,
> the refusal shape by hashing the task's declared initial state. A digest is never
> stored, so none can go stale.

### Folding numeric strings for a money / quantity field

Some backends round-trip `Decimal` columns as strings, so the same amount can
surface as `"130.00"` on one side and `"130.0"` on the other and false-fail a
correct trial. Opt the specific field(s) into string folding — never globally:

```yaml
state_checks:
  hash:
    enabled: true
    weight: 1.0
    expect_initial_state: true  # or golden_actions — an enabled hash needs a source
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
`state_checks.id_fields`. The value takes two forms of one shape: a single field
name, or an ordered list of field names for a table where no single column is
unique (a composite key). A table absent from the map defaults to `"id"`, so
`id`-keyed domains need nothing:

```yaml
state_checks:
  hash:
    enabled: true
    weight: 1.0
  id_fields:                          # per-table primary-key override; absent => "id"
    widgets: widget_id
    line_items: line_id
    positions: [account_id, symbol]   # composite: no single column is unique
```

Map keys are the table names as they appear in `initial_state`. This is config
data that travels with the task, so key resolution never depends on reading model
source at runtime. The runner reads the same map when it matches a toolset's
model classes to db-service tables at trial setup: a model declaring every
component of a table's key — single or composite, in any order — is registered
to that table up front, rather than resolved from its class name on first use.
A table keyed by neither `"id"` nor a declared
field fails loud at write time with the exact `id_fields` entry to add, and a
record missing any declared key component fails loud naming the table, the
missing component and the full declared key — per component, not just for the
whole key. The MCP-subprocess and Tau diff-sync paths (`_sync_mcp_state_to_db`,
`TauSyncToolWrapper._sync_state_changes`) consult the same map, so records with
their key omitted also fail loud instead of collapsing to a single `None` bucket
and silently corrupting the state diff.

The rubric judge's `initial → final` state diff uses the same map as its key
source: each table's rows are matched on the declared key, comparing every
component (with numeric folding applied per component, so `1` and `1.0` key the
same row), layered over the task schemas' primary keys. A table with no
declaration falls back to the `id` / `*_id` single-field heuristic, then to
whole-record matching. A composite-keyed edit therefore renders as one
field-level modification labelled with all key components, not a remove/add
pair.

Pack tool code addresses a composite-keyed record with a **mapping of component
values**: `db.get_by_id(Position, {"account_id": "A1", "symbol": "MSFT"})`, and
the same shape on `delete_by_id`. A scalar is refused naming the table and its
declared components — the engine cannot interpret whatever concatenation the
model's `get_id()` produces — and a bare sequence is refused too, because
`["a", "b"]` is ambiguous between two components and one list-valued component.
`update(obj)` and `delete(obj)` need no addressing at all: the key value is
taken component-wise from the record itself.

Every path that indexes records by a composite key compares **component-wise**:
two records carry the same key only when they agree on every declared field.
The components are never concatenated into one synthetic value — concatenation
collides (`("a_b", "c")` and `("a", "b_c")` join to the same string), which
would reintroduce exactly the ambiguity a composite key exists to remove. A
one-element list means the same thing as the bare string, and a record missing
*any* declared component fails loud naming the table, the missing component and
the full declared key. A declaration that cannot name a key at all — an empty
list, a blank or non-string component, or a component repeated twice — is
refused when the config loads, naming the table.

The whole `id_fields` map is cross-checked against the seeded
`initial_state.tables` at three gates, all reading one computation so they cannot
disagree: `tolokaforge validate`, task-description build time (the orchestrator's
pre-run gradeability gate) and `RegisterTrial`. A bad declaration therefore costs a
`✗` line at validate — before a run is even started — and a build error after that,
never a trial. One check, three findings, reported together: a map key naming no
seeded table ("unknown table" — a typo, or a table missing from `initial_state`); a
declared key component — single field and composite components alike — absent from
every seeded record of its table; and a declared key — single or composite — that
does not uniquely identify the table's seeded records, named by the colliding key
value. A key that cannot tell two seeded rows apart cannot address either row
for an update or a delete, so the non-unique finding is what refuses a
single-field declaration over rows only a composite key distinguishes. Each
finding carries its exact remediation (fix the typo, add the table, seed the
field, widen the key to a composite list, or opt in below). A component
present in *some* seeded record passes the component finding; a record
actually missing it still fails loud at write or diff time, per component.

`validate` reads the seeded state the native way, from `initial_state.json_db`, so
a task whose `json_db` names a file that is not on disk is a `✗` naming the path
there rather than a `RuntimeError` at run start. A task an adapter maintained
outside this repository owns may seed its state some other way, and holding its
declaration against a reading that adapter does not use would reject packs that run
fine: those packs draw a `?` line for `state_checks.id_fields` — never checked,
never fatal — and `RegisterTrial` keeps enforcing at run time. The shape rules on
the declaration itself (empty list, blank or duplicate component) fire at every gate
whatever the adapter, because they read the declaration alone. Legacy tasks that
pre-date the check can downgrade every finding to one warning:

```yaml
state_checks:
  id_fields:
    legacy_widgets: widget_id
  relaxed_validation: true            # temporary — legacy escape hatch only
```

`relaxed_validation` defaults to `false`; new tasks should fix typos rather than
enable it — it downgrades **all three** findings, the unknown table, the absent
component and the non-unique key. The runner also runs the same check as
belt-and-suspenders for engines
that bypass `NativeAdapter.to_task_description`. Both keys are consumed at load
time / `RegisterTrial` on both substrates rather than in the grade-time component
phase — see [Substrate Parity](#substrate-parity).

**Tables materialized only by `initialization_actions`**: every gate reads
`initial_state.tables` (typically populated from `initial_state.json_db`). A
table that first appears only via an `initialization_action` is visible to none of
them — an `id_fields` entry for such a table needs `relaxed_validation:
true` today. Add the table to `initial_state.json_db` (even with an empty list)
if you want the strict check to accept it: a table seeded empty passes the
unknown-table finding and is skipped by the record-level findings (absent
component and non-unique key), because there is no record to hold the declared
key against.

**Both keys carry the runner-engine version lock.** `id_fields` and
`relaxed_validation` are declared on the runner-side `RunnerStateChecksConfig`
(`extra="forbid"`), so an image that does not present a key rejects a pack declaring
it at `RegisterTrial` rather than ignoring it — see
[§ Which keys a grading block refuses](#which-keys-a-grading-block-refuses).
`id_fields` is locked by its *value* as well as its name; the release its image first
presents that value in is in
[§ Runner-engine version lock](#runner-engine-version-lock). `relaxed_validation` has no
row there because it predates that table's support floor — every image the table speaks
about already presents it.

### Runner-engine version lock

The trial spec crosses the wire as a plain `model_dump_json()` parsed by
`extra="forbid"` runner models — so a field, or a field *value*, that the receiving
side does not declare fails validation rather than being dropped.

**`first declared by` names the release whose image first presents the key in the
shape this row's lock is about** — not the release the key's name first appeared in.
For a key whose shape changed, that is the release the current shape arrived in; for a
key this table covers because a current image *lacks* it, it is the release the absence
arrived in. `unreleased` means no released image presents it yet.

**This table speaks about runner images from `v0.13.1` onward**, and a key belongs on
it exactly when its locked shape arrived at or after that floor. Nothing in this
repository declares how far back images are supported, so the floor is a stated
judgement rather than a derived policy — but it is not an arbitrary one: `v0.13.1` is
the single release where `env_assertions` was removed, `hash_weight`,
`tool_expectations` and `custom_checks` arrived, and the `combine_method` value domain
changed. That membership rule is operative rather than aspirational: every grading key
the engine can put on the wire is held to it, so one added below any container has to
join this table or be recorded as predating the floor. Which keys predate it is a
declaration made when that record was written, not something re-derived per release.

**A row dated to the floor itself bites only images older than this table's scope.** It
is listed so the release is on record, not because an image the table speaks about can
reject it.

| key | emitted for | first declared by | direction |
|---|---|---|---|
| `state_checks.env_assertions` | no current engine | `v0.13.1` | old engine → new image |
| `state_checks.hash_weight` | a pack declaring `state_checks` | `v0.13.1` | new engine → old image |
| `transcript_rules.tool_expectations` | a pack declaring `transcript_rules` | `v0.13.1` | new engine → old image |
| `combine_method` | every pack | `v0.13.1` | both directions |
| `custom_checks` | every pack | `v0.13.1` | new engine → old image |
| `transcript_rules.min_assistant_turns` | a pack declaring `transcript_rules` | `v0.15.0` | new engine → old image |
| `trace_checks` | every pack | `v0.15.0` | new engine → old image |
| `state_checks.id_fields` | a pack declaring `state_checks` | `v0.16.1` | new engine → old image |
| `state_checks.expect_initial_state` | a pack declaring `state_checks` | `unreleased` | both directions |
| `transcript_rules.required_actions[*].name` | a pack declaring `transcript_rules.required_actions` | `unreleased` | both directions |
| `search.plane` | every pack | `unreleased` | new engine → old image |

`emitted for` is what the adapter puts on the wire, not what the pack asks for: a key
whose cell reads **every pack** is emitted as `null` when the pack declares nothing
under it, and `null` is a key an image must still declare. That is why `trace_checks`
bites a pack that grades no trajectory at all, and why `search.plane` bites a task
with no knowledge base.

Three rows need more than a cell:

- **`state_checks.expect_initial_state` bites in both directions because two spellings
  cross.** A current engine emits `expect_initial_state`; an engine predating it emits
  `expected_hash` — the field a stored digest crossed on, deleted here — which a
  current image does not declare. The authored `grading.yaml` key is
  `state_checks.hash.expect_initial_state`, and the digest it replaces is retired: a
  pack declaring `expected_state_hash` migrates, per
  [§ Which keys a grading block refuses](#which-keys-a-grading-block-refuses).
- **`state_checks.id_fields` is locked by its value, not its name.** An image
  predating the list form declares the value as a plain string, so a pack declaring a
  composite (list-valued) key is rejected with a Pydantic `string_type` error naming
  `id_fields` — the correct fail-loud outcome, since that image cannot resolve a
  composite key. A single-field declaration crosses as the same plain string it always
  did.
- **`state_checks.env_assertions` is on this table because a current image does *not*
  declare it.** An engine predating its removal translates the authored key onto that
  field, so the rejection is an old engine against a new image — the one row here whose
  direction runs that way.

`combine_method` is locked by its value domain the same way `id_fields` is: the runner
validates it against the closed set in [§ Score Combination](#score-combination), so a
value one side's set does not hold is rejected at the value rather than at the key.
`transcript_rules.required_actions[*].name` reached the wire as `tool_name` before one
model served both the authored block and the trial spec; the authored `grading.yaml`
key is `name:` and is unchanged, so nothing in a task pack migrates.

A new engine therefore requires a runner image presenting every key above, and
`make docker-build-core` is part of every engine upgrade. `db_hash_check` is **not**
on this table: it was never declared on the runner config at all, so no engine ever
emitted it, and a populated `db_hash_check` is rejected core-side at config load.

**This lock is narrower than the proto3 rule that governs the rest of registration.**
`engine_protocol_version` and `call_id` are proto message fields, which an older
runner drops as unknown — so for those the bound is one-sided and a newer engine
registers fine (see [`RUNNER.md`](RUNNER.md#engine--image-version-lock)). The trial
spec is not a proto message: it crosses as `trial_spec_json`, a JSON string parsed by
`extra="forbid"` Pydantic models, where an unknown field is an error rather than a
dropped byte. The signature of the skew is a Pydantic `extra_forbidden` error naming
`hash_weight` or `min_assistant_turns` in the `RegisterTrialResponse.error` —
whichever block the pack carries.

An old engine against a new runner image can also be rejected for a second,
narrower reason: such an engine drops `hash.weight` on the way to the wire, so a
pack configuring a hash source *and* non-empty `jsonpaths` reaches the runner with
nothing saying how to fold them, and the presence gate rejects it. That rejection is
correct — the alternative is grading the trial by a rule the author never chose.

### The `jsonpaths` assertion vocabulary

One assertion names a `path` and **exactly one** comparison from a closed set of
four:

| operator | holds when |
|---|---|
| `equals` / `equals_ci` | the value at the path is equal, case-sensitively or not |
| `contains` / `contains_ci` | the value contains it — recursively, per [`contains`](#operators) |

The same four are the vocabulary of `db_probes[*].expect`. They are deliberately
narrower than the seventeen [`trace_checks` operators](#operators): a second comparison
at one path has no conjunctive reading and is almost always a typo, so **two
operators on one assertion is a failed check**, not a conjunction. So is **no**
operator: a bare `path:`, or a misspelled `op:` / `expected:` key, fails rather than
passing as an existence check, because a strict-looking assertion that silently
cannot fail is worse than none. A path that resolves to nothing is a failed check
too. A path resolving to several values holds when **any** of them satisfies the
comparison.

**`path_glob` is a different assertion with a narrower vocabulary.** It matches
written files by shell glob rather than the state by JSONPath — the way a
file-writing task avoids asserting on a filename the agent chose — and the two
substrates read it differently: core-side it applies any of the four operators to the
matched entries of `state["filesystem"]`, while the runner routes it to a
file-content evaluator that reads only `contains_ci`. Write `path_glob` with
`contains_ci` for a check that means the same thing wherever the trial was graded.

### Folding the hash verdict with `jsonpaths`

`state_checks` has two possible sources — the state hash and the JSONPath
assertions — and one component score. Each source reads a different level of the
trial's final state, and both levels are fixed:

| source | evaluated against |
|---|---|
| `hash` | the **unwrapped** database inside the final state (`db`, else `agent`, else the state itself) — the level the golden state and `compute_stable_hash` both describe |
| `jsonpaths` | the **whole** final environment state, so an assertion is rooted `$.db.<table>[…]` |

A source that declares nothing to evaluate produces no verdict, and a source
nobody configured contributes nothing rather than a score. An empty `jsonpaths`
list is therefore not a source: asking "what fraction of zero assertions passed?"
has the answer `1.0`, and that fraction of nothing never becomes a component score.

- **hash only** (`jsonpaths` empty — the tau-bench shape): the component *is* the
  hash verdict, at every `weight`. An empty assertion list is not a pass.
- **non-empty `jsonpaths` only** (no hash source declared): the component is the
  assertion score. **Core-side only** — see the substrate note below.
- **both**: `jsonpath_score × (1 − weight) + hash_score × weight`.
- **neither** — an empty `jsonpaths` list with hash grading off, or on and unable
  to produce a verdict: the component is **not evaluated**. It is absent from the
  grade rather than present at a number, so `combine.weights` folds the components
  that were actually decided. Core-side this is the answer for a `db_probes`-only
  pack too, since core has no probe evaluator.

`hash.weight` is consulted only in the third case, and it has **no default**:
every candidate value there silently discards something the author asked for. So a
pack that needs a weight and declares none is **rejected at load** — by
`tolokaforge validate` and by the grading config model — with a message naming the
three meaningful choices: `1.0` lets the hash decide, `0.0` lets the jsonpaths
decide, `0.5` gives them equal shares. The value must lie within `[0.0, 1.0]`;
outside that range the component leaves `[0, 1]` altogether, and a value that is not
a real number in that range — a bool, a numeric string — is rejected on **both**
substrates rather than coerced into one.

**The flag and a source are declared together, or neither is.** Both halves are
rejected at load where the adapter grading the task reports that the authored keys are
the whole layer, which is what `adapter_type: native` means. Where it instead names the
source it supplies beneath them, the enabled half is decided against that source — a
usable one passes, a missing or empty one is refused naming the fixture — while the
disabled half is rejected as written, since a source the block declares and nothing
reads is the author's defect whatever lies beside it. Where no adapter answers, both
shapes are reported unchecked at the same address. See the hash rows in
[What is validated before a run](#what-is-validated-before-a-run):

- **Either source under a falsy `enabled`** is a comparison that never runs. Both
  substrates test the flag before reading any source, so the pack grades its state
  without the hash its author asked for and says nothing — a golden path there replays
  on neither substrate, and an initial-state comparison runs on neither. The refusal is
  addressed at the source the pack wrote: one flag to fix, so one finding.
- **`enabled` with neither source** is hash grading with nothing to compare
  against, and the two substrates answer it differently: core produces no hash verdict
  at all while the runner compares the trial against its initial state, so the same
  trial takes two different `state_checks` components. A refusal task — one whose
  expected final state *is* the initial state — declares `expect_initial_state: true`,
  which is that comparison asked for rather than fallen into.
- **`expect_initial_state` beside either other source** names two expected states with
  no precedence between them, so the block is refused wherever it is constructed: the
  core config, the authoring gate, both of the adapter's reads, and the runner's
  translation of its own flattened fields. The message names both keys, either being
  the author's to drop.

Both halves read for truth rather than for `true`/absence: core branches on the
flag's truthiness and the runner coerces it, so `enabled: 1` grades and loads, and an
empty `golden_actions` list replays nothing, so it is no more a source than an absent
one. The rules' class and the rest of the pre-run gate are in
[What is validated before a run](#what-is-validated-before-a-run).

**The block is closed, and both of the adapter's reads of it refuse the same key.** A key
`hash` does not declare requests *nothing* — a misspelled `enalbed` or
`expected_state_hsah` leaves the hash unscored while the trial grades on whatever else
the pack declared, which scores *higher* than the same block spelled correctly. So the
accepted set is exactly `enabled`, `expect_initial_state`, `golden_actions`, `weight` and
`description`, and anything else is a load error
([§ Which keys a grading block refuses](#which-keys-a-grading-block-refuses)).
`NativeAdapter` reads a `grading.yaml` on two errands — `get_grading_config` builds the
host-side config, `to_task_description` lowers the block onto the runner's flattened
`hash_enabled` / `expect_initial_state` / `hash_weight` fields — and **both construct the block
rather than reading it key by key**, so neither lowers a key the other refuses. The two
share a file and not an object, which is what makes the second read load-bearing:
`tolokaforge run-trial` runs no grading pre-flight, so the description build is the only
read a trial there passes through, and a key dropped at that read reaches
`RegisterTrial` as an absent hash and is paid for.

| key | what it declares |
|---|---|
| `enabled` | whether the hash is compared at all. A source under a falsy flag, or a truthy flag with no source, is refused rather than graded |
| `golden_actions` | the actions to replay for an expected state |
| `expect_initial_state` | that the expected final state *is* the state the task starts in — the refusal-task shape, read from `initial_state.json_db` in either shape a task writes it. Refused beside `golden_actions`, which names a different expected state |
| `weight` | the hash's share of the `state_checks` component where `jsonpaths` is non-empty too. No default — the shape that needs one and declares none is refused |
| `description` | what the hash asserts, in the author's words. A non-empty value is appended in parentheses to the hash verdict's reason in `grade.reasons`, the way an assertion's `description` reads into its own |

`description` is the one key of the five the runner's flattened block does not declare, so
a trial the runner graded reports the hash verdict without it.

**`golden_actions` is the list of actions to replay, or there is no replay.** A **falsy**
value — `[]`, `{}`, `""`, `0`, `false`, or the key written bare, which is what an author
reaches by commenting the actions out — is no replay at every read site on either
substrate: the description a pack builds carries no actions and core reports the source as
absent. What each substrate then *grades* for a replay of no actions is where they part
company, and that difference belongs to the sourceless shape rather than to this rule: core
takes no hash verdict at all, while the runner's refusal-task semantics reset the
environment, hash the initial state as the golden one and hand the fold a binary verdict.

A **truthy** value that is not a list can be replayed by neither substrate, so each of the
three surfaces that has to act on it refuses it in one sentence naming the key, the type
received and the fix. `tolokaforge validate` reports an ERROR at
`state_checks.hash.golden_actions` and exits non-zero.
`NativeAdapter.to_task_description` raises `UnreplayableGoldenSource` — a
`GoldenReplayError` subclass — before a trial can be registered; a run's pre-flight
resolves each pack's description before it reaches the gate, so that raise stops the pass
where it stands with its own sentence instead of joining the named list of offending tasks,
and #880 owns folding that class into it. Core's hash path raises the same class above the
world the actions would otherwise need. `NativeAdapter.compute_golden_hash` resolves no
source at all and answers `None` for every shape (#836).

A **list element** that is no mapping — an action written `- place_order` where
`- name: place_order` belongs — declares no tool to call. `tolokaforge validate` refuses it
at `state_checks.hash.golden_actions[i].name`, the description build raises
`UnresolvableGoldenAction` naming the offending index, and core raises the same class out
of name resolution, before the first action runs. A mapping element whose `name` is absent
or empty is refused at that same address by the gate, and core refuses it at resolution
too — but the description build lowers it onto the wire as an empty tool name, where it
fails only once the runner's replay resolves it, which is #886.

"Needs a weight" is exactly: `hash.enabled` is on, **and** `hash` declares
`golden_actions` or `expect_initial_state`, **and** `jsonpaths` is non-empty. Every
other shape yields at most one score *core-side*, so a weight there would have
nothing to divide: it loads, its range is still checked, and `grade.reasons` records
that it was declared but not consulted — on both substrates, from one constant.

**Which substrate graded the trial still matters, for two hash-block shapes — both of
them ones no gate admits.** The
fold is one function (`core/grading/state_composition.py`) and both the core engine
and the runner's `GradeTrial` call it, so the *rule* is shared; the runner carries
the weight as the flattened `state_checks.hash_weight` on its `RunnerStateChecksConfig` and
applies the same presence gate at `RegisterTrial`. What is not shared is what each
substrate feeds that fold:

- **`hash.enabled` with no declared source** — **not authorable**, and the divergence
  below is why. Core produces no hash verdict and the component is the assertion score
  alone. The runner runs hash grading anyway — the refusal shape, where the expected
  state *is* the initial state — so it folds a real binary verdict with the assertions.
  Measured at `weight: 0.6` against assertions scoring `0.5`: core `0.5`, runner `0.8`
  on a match and `0.2` on a divergence. The pre-run gate refuses the shape rather than
  leaving the two substrates to disagree over it, so it is reachable only from a config
  built directly against the engine and from a bundle recorded before the rule — the
  bundles `retrace` replays, where core's *no verdict* is the answer stated below.
- **`golden_actions` with no world to replay them in** — **not authorable** either, and
  the divergence is the sharper one: core computes no expected state at all and raises,
  leaving the trial unscored, while the runner has nothing to lack. Its replay world *is*
  the live trial — the tools `RegisterTrial` registered, over db-service's state — so it
  replays, hashes and folds a real binary verdict with the assertions. The gate refuses
  the shape wherever a caller can resolve what the task supplies, so it too survives only
  in a directly built engine and in a config no gate saw.

Both authorable shapes — **`golden_actions` replayed in a world the task supplies** and
**`expect_initial_state`** — are proven to hand both substrates the same verdict, and
therefore the same component; see
[Substrate Parity](#substrate-parity) for the manifest rows and the tests that prove
them.

**Core-side**, a hash block declaring no source at all — `hash.enabled` with neither
source — yields **no** hash verdict and names the
skipped check in `grade.reasons`, rather than a `0.0` that reads as a state the agent got
wrong. Beside an empty `jsonpaths` list there is then no verdict at all, so the whole
component is unevaluated and no score sits next to the reason contradicting it.

**A golden replay that cannot be executed is a grading error rather than a verdict**, on
either substrate: core raises and the trial is left unscored, the runner answers
`GradeTrial` with `success=false`. Having no world to replay *in* is one of those
failures. Core raises `UnbuildableGoldenReplayWorld`
(`tolokaforge.core.grading.golden_replay`), naming in one message every task-level fact
the replay needs and does not have — `initial_state.json_db` as a path to a JSON file
rather than an inline mapping, `tools.agent.mcp_server`, and the task directory the
caller passes. So a pack never collects the `state_checks` score its JSONPath assertions
earned while the hash they are weighed against went uncomputed. Which source the block
declares decides whether a world is needed at all: `expect_initial_state` compares in
process against the state the task starts in, so a pack declaring it replays nothing and
needs none. The shape is refused earlier still, wherever a caller can
resolve what the task supplies — see
[What is validated before a run](#what-is-validated-before-a-run) — so the raise is
reachable only from a config no gate saw.

**An action name that resolves to nothing is one of those failures, on both
substrates.** Every authored name is resolved before the first action runs, so a
partially replayed golden world is never built and nothing is ever hashed against one.
An action with no `name` key, `name: ""`, `name: null`, or a `name` that is no string at
all resolves to nothing the same way and draws the same error, and one raise names every
offending action, its index, and the set it was resolved against — an author correcting
a golden path sees the whole
list rather than paying for a replay per typo. Both shapes are refused earlier still,
wherever the authoring gate can resolve the task's tool set — see
[What is validated before a run](#what-is-validated-before-a-run) for the namespace it
resolves them against, which is not quite either substrate's.

What each substrate resolves *against* still differs, and #815 owns unifying the two.
Core matches the pack's `TOOLS` map exactly and raises `UnresolvableGoldenAction`,
leaving the trial unscored. The runner matches the tools `RegisterTrial` registered for
the trial, accepting a single registered `…_<name>` suffix on top of an exact match
because golden actions are authored unprefixed, and answers `GradeTrial` with
`success=false`. It resolves before it writes anything — before the MCP state sync, the
`pre_golden` snapshot and the reset — so a pack defect costs the trial's database
nothing and the trial still holds what the agent left behind.

**An action that resolved and ran but did not take effect is different: the verdict
stands, and the grade names it.** Two shapes reach that state. The action *raised*, or it
ran and *reported* failure in what it returned — which is how every tool built through
`create_server` signals its own declared failures, since `DomainToolRegistry` converts a
`ToolError` raise into a returned `{"error": …}` payload. Either way the replay continues
past it — tau-bench continues past a precondition failure and golden-action hash grading
is the tau-style path — so the partial world it left is hashed against and yields a binary
verdict as usual. `grade.reasons` then carries one sentence, built once for whichever
substrate graded the trial, under the `GOLDEN REPLAY ERRORS:` prefix: how many of how many
actions did not take effect, then each by index and name with the verb it failed under and
the message — `[1] confirm_payment raised TypeError: …` beside `[1] confirm_payment
reported Order 'O-999' not found`. The verb is what sends an author to the right defect:
`raised` means the golden path calls the tool wrong, `reported` means it calls it right
about a state that refuses it. Whether a verdict computed against a partial world should be
admissible at all is open (#816) — the sentence annotates such a verdict, it does not
sanction it, and the reproduced case is a trial that failed its task scoring `1.0` because
the golden path stopped at the same place the agent did.

**A reported failure is read on both substrates, by one predicate.** Each reads what the
action's tool answered and takes a truthy top-level `"error"` as a declared failure, through
the same `declared_failure`: core reads what `invoke` returned, the runner the text its tool
wrapper returned. Both shapes the predicate accepts are reached — core is handed a mapping
by a pack whose tools return one and the JSON string of one by a tau-style pack whose tools
`json.dumps` their answer, while every payload the runner reads is a string,
`ToolWrapper.execute` being typed `-> str`. An `MCP_SERVER` pack is covered too, because
FastMCP renders a returned mapping as one `json.dumps` text block that decodes cleanly.
Truthiness rather than key presence, so `{"error": null}` stays the success it reads as; top
level only, so a nested domain `"error"` field in a returned state slice is not one.

**A raised failure is read on both substrates too, under the same kind.** Core-side and on
the runner's tau and MCP-async paths the wrapper re-raises, so the replay loop's
`except Exception` sees the exception. An `MCP_SERVER` pack states it out of band instead:
the protocol answers a call whose exception escaped the tool with `isError: true` beside the
prose FastMCP flattened that exception into, `MCPServerToolWrapper.execute_call` reports the
flag beside the text, and the loop records the action as raised — the flag is the substrate's
own statement, so the tool's body never decided anything, which is what the raised kind
means. The flag is read before the returned-payload predicate, so a flagged call is recorded
once whatever its text also looks like; the two populations are disjoint on that substrate
anyway, `DomainToolRegistry` catching a `ToolError` and returning it, so only an undeclared
exception ever reaches the flag. What differs across substrates is the message, deliberately:
each quotes the layer that actually failed, so core's reads `TypeError: …` on one line while
the runner's carries the MCP server's multi-line validation prose, `Error executing tool …`
prefix and newlines included — `grade.reasons` carries those newlines rather than a tidied
one-liner, because an author searching a log for the string finds what the server wrote. The
kind, the verb and the sentence's shape are shared.

A tool reporting failure as a bare prose string — `"Error: invalid characters in
expression"` — is detected on neither substrate by design: telling one from legitimate
output needs substring matching over prose, which false-positives on any tool whose own
output mentions the word (#855).

### Best Practices

- Filter non-deterministic fields (timestamps, UUIDs) before hashing
- Every hash source names a state, not a digest — `golden_actions` for a task that
  changes state, `expect_initial_state` for a refusal task
- Fold numeric strings per-field (`numeric_string_fields`), never as a global switch
- Declare non-`id` primary keys per table (`id_fields`); leave `id`-keyed tables unset
- Use `relaxed_validation` only as a short-lived escape hatch for legacy tasks
- Combining the hash with JSONPath assertions requires an explicit `weight` —
  decide which source carries the verdict, per
  [Folding the hash verdict with `jsonpaths`](#folding-the-hash-verdict-with-jsonpaths).
  `tolokaforge validate` rejects that combination without one

---

## Transcript Rules

`transcript_rules` grades the *process* — what the agent said and which tools it
reached for — rather than the final state. Both substrates consume every key in
the block, and both read it off the
[trial event timeline](#trial-event-timeline).

**What a rule can see.** A tool rule sees the **agent's** calls that reached the
substrate: a call the agent declared on a terminating turn never ran, and a call the
user simulator ran is another actor's, so neither satisfies a `required_tools` entry
nor violates a `disallowed_tools` entry. A phrase rule
(`must_contain`, `disallow_regex`, `communicate_info`) sees the agent's own text
and nothing else: not the user's turns, so a phrase the user supplied cannot
satisfy a rule about what the agent said, and not the text a tool returned, which
is [trace checks](#trace-checks) territory — a result predicate beside
`status: {equals: success}` is where an assertion about tool output lives. Nor can
either substrate see the harness's `role: system` annotations — a termination
notice cannot satisfy a required phrase (N3).

### Turn bounds

`max_turns` and `min_assistant_turns` bound one counter from two sides — the
number of **assistant generations** the trial produced:

```yaml
transcript_rules:
  max_turns: 18            # the agent must not take more than 18 turns
  min_assistant_turns: 1   # opt-in: the agent must have taken at least 1
```

**`max_turns` alone passes a trial that produced nothing.** A do-nothing agent
took zero turns, which is within any limit, so the check passes vacuously. On a
refusal-style task — where the expected final state equals the initial state — that
trial also matches the expected state hash, and the whole trial passes without the
agent having acted. `min_assistant_turns` is the assertion that it acted at all;
[`docs/TASKS.md`](TASKS.md#refusal-tasks-and-other-do-nothing-passes) § Refusal
tasks and other do-nothing passes covers when a task should declare one, the
`combine.weights` entry it needs to reach the final score, and the state-side half
of the same hole that the floor does not close.

**The floor is a gate on the whole `transcript_rules` component, not a sub-check
inside it.** Unmet, the component is `0.0` on both substrates whatever the other
keys scored. Met, it contributes nothing at all — no sub-check row runner-side, no
extra bucket core-side — so a pack that declares it and satisfies it scores exactly
what the other keys score. That is deliberate: as a fifth core-side bucket a failed
floor would score `(1+1+1+1+0)/5 = 0.8`, which is the default `pass_threshold`, and
as one more runner sub-check alongside two passing keys it would score `0.667`,
which any `pass_threshold` at or below that swallows. Either way the bound would be
declarable and unable to fail a trial.

**It counts generations, not answers.** Three tool-call-only turns with no prose
satisfy `min_assistant_turns: 3`. The sharper "did the agent actually *answer*"
check — a non-empty assistant message after the last tool call — is **#678**'s trace
checks; do not read a green floor as evidence the agent replied.

**A declared floor is evaluated on an events-less timeline**, where every other
transcript rule is skipped, on both substrates. Without a floor the whole component
drops out of the combine there; with one, the floor alone scores it `0.0`. The
runner additionally records the skip against each sibling key — see
[The runtime ledger](#the-runtime-ledger).

**A window no trial can land in is rejected at load.** A floor above the ceiling
admits no assistant-turn count at all, so the component would be `0.0` however the
agent behaved. `tolokaforge validate` rejects such a pack before the run is paid
for, naming both keys and both values:

```
grading.yaml transcript_rules declares an unsatisfiable turn window:
min_assistant_turns (5) is above max_turns (3), so no assistant-turn count
satisfies both bounds and every trial fails the transcript component. Lower
min_assistant_turns to at most 3, or raise max_turns to at least 5.
```

One `TranscriptRulesConfig` serves the authored block and the trial spec, so a window
the engine rejects at validate time is rejected at `RegisterTrial` too rather than
registering and grading. A floor *equal*
to the ceiling is satisfiable — by exactly that many turns — and either key on its
own bounds one side only, so only a pack declaring both can close the window.

**Both keys are declarable from `1` up**, which is what keeps that last sentence
true: a ceiling of `0` closes the window on its own, and a floor of `0` asserts
nothing. Either is rejected at load naming the key and the bound.

**`min_assistant_turns` carries the runner-engine version lock.** It is declared on
`TranscriptRulesConfig` (`extra="forbid"`) and the engine emits it on **every** pack
carrying a `transcript_rules:` block, as `null` when the pack declares no floor. The
release its image first presents it in, and the direction it bites, are in
[§ Runner-engine version lock](#runner-engine-version-lock).

### `tool_expectations`

Names the tools the agent must use and the tools it must not touch:

```yaml
transcript_rules:
  tool_expectations:
    required_tools: ["db_update"]        # each must have been called successfully
    disallowed_tools: ["bash"]           # none may be called, at any status
```

**One sub-check per declared tool**, the same decomposition `must_contain` and
`disallow_regex` get: the component score is the fraction of sub-checks that
passed — unless a declared `min_assistant_turns` floor is unmet, which forces the
component to `0.0` — and every failure is named in `grade.reasons`. A task
declaring two required and two disallowed tools yields four independent
sub-checks.

**The two lists treat call status differently, deliberately.** A `required_tools`
entry is satisfied only by a call with `status == "success"` — an errored call did
not do the work the author required, the same rule `required_actions` applies. A
`disallowed_tools` entry fails on a call at **any** status, errors included:
attempting a forbidden action is itself the violation, so a `delete_customer` call
that happened to blow up still fails the check.

**Both lists read the agent's own calls.** A user-simulator call satisfies no
`required_tools` entry and violates no `disallowed_tools` entry, at any status —
the posture the phrase rules already take towards the user's text. Where the actor
is the point, [trace checks](#trace-checks) are the vocabulary: a matcher carries an
explicit `executor` field, so "no actor may call `x`" and assertions about a
user-side call are written there. `required_actions` names its actor too, through
`requestor`.

**Which actor ran a call is on the record.** A call the message view declares and
the [trial event timeline](#trial-event-timeline) holds no record for therefore
fails both lists, whichever message carried it: nothing says whether it ran, let
alone who ran it, and a "did not run" reading would pass every forbidden call.

`extra="forbid"` on the block means a misspelled key (`required_toolz`) fails at
load rather than grading as an empty list.

**A misspelled *tool name* is rejected at load, not at grade time.** Grade-time
evaluation cannot tell `required_tools: ["db_updat"]` from "the agent never called
it", and a typo in `disallowed_tools` passes trivially because no call ever matches
it. So both lists are checked against the task's declared tool set by
`tolokaforge validate` and by the pre-run gate: a name no actor of the task can call
is an authoring error naming the tools the task does declare. See
[What is validated before a run](#what-is-validated-before-a-run).

**`tool_expectations` carries the runner-engine version lock.** It is declared on
`TranscriptRulesConfig` (`extra="forbid"`), so an image that does not present it
rejects a pack declaring one at `RegisterTrial`. The release its image first presents
it in, and the direction it bites, are in
[§ Runner-engine version lock](#runner-engine-version-lock).

---

## Trace Checks

`trace_checks` states conditions on **what the agent did and in what order**,
evaluated over the [trial event timeline](#trial-event-timeline). Where
`transcript_rules` asks flat, unordered, exact-equality presence questions,
`trace_checks` expresses ordering, scoped negation, non-equality argument
predicates, nested argument paths, counting, and a call's status or result.

**Both substrates score it through one function.** `evaluate_trace_checks`
(`tolokaforge/core/grading/trace_checks.py`) is called by the core engine's
`grade_trajectory` and by the runner's `GradeTrial`, over the timeline each
already builds, so the component score does not depend on which substrate graded
the trial. The per-constraint verdicts cross the wire on `Grade.trace_checks`,
each carrying its `severity` and whether it was
[undecided](#when-a-constraint-cannot-be-decided), and are written inline in
`grade.yaml` under `trace_check_results`; `Grade.trace_checks_summary` carries the winning route, the
gates that shut and one line per alternative, and lands beside them under
`trace_checks_summary`. **A tripped gate fails the trial on both substrates** —
the core engine's combine and the runner's `GradeTrial` each force `binary_pass`
false, the same act the runner already performs on the judge's
[required-criterion gate](#required-gate-semantics).

**A trial whose timeline carries no events leaves the component unscored.** Every
constraint would otherwise be answered by evidence the trial does not have. The
runner records that as a skip against each declared constraint kind, and — since
a component the pack configures but nothing scores is not folded in — a pack
weighted entirely on `trace_checks` fails such a trial rather than passing it.
The guard against a trial that *does* carry events but should not have counted as
work is `transcript_rules.min_assistant_turns`, which is a separate declaration.

**Records-less bundles read fewer fields.** `status` and `executor` come from the
tool-call record alone, so on a bundle re-graded without one a matcher reading
either is [undecided](#when-a-constraint-cannot-be-decided) rather than unmatched
— a named failing sub-check, never a pass in the agent's favour.

### The config surface

```yaml
trace_checks:
  constraints:                                  # hold whatever route the agent took
    - id: lookup_before_denial                  # unique across the whole block
      description: "payment looked up before the duplicate-refund case is denied"
      weight: 2.0                               # default 1.0
      severity: scored                          # scored (default) | gate
      on_missing: fail                          # fail (default) | pass
      within: { first_turn: 2, last_turn: 5 }   # optional, inclusive turn window
      bind: …                                   # optional, see Correlating arguments
      require:
        before:
          left:  { quantifier: any,   match: { kind: tool_call, tool: { equals: billing_api_get_payment },
                                               args: { payment_id: { equals: "PAY-664306" } } } }
          right: { quantifier: first, match: { kind: tool_call, tool: { equals: servicenow_csm_update_case },
                                               args: { u_resolution_code: { equals: denied_ineligible } } } }
  alternatives:                                 # optional, two or more routes
    - id: refund_via_reversal
      description: "the payment is reversed at the processor"
      constraints:
        - id: reversal_requested
          description: "a reversal is requested for the duplicate charge"
          require: { present: { match: { kind: tool_call, tool: { equals: billing_api_reverse_payment } } } }
    - id: refund_via_credit_note
      description: "a credit note settles the duplicate charge"
      constraints:
        - id: credit_note_issued
          description: "a credit note is issued for the duplicate charge"
          require: { present: { match: { kind: tool_call, tool: { equals: billing_api_issue_credit } } } }
```

`require` carries **exactly one** constraint kind, and each kind's value is that
kind's own payload. Two conditions are an `all_of` over two expressions. Every
model is `extra="forbid"`, so a misspelled operator, kind or matcher field fails
at `tolokaforge validate` rather than grading as unset. `bind` is how a constraint
compares one call's argument against another's rather than against a literal, and
has its own section: [Correlating arguments across
matchers](#correlating-arguments-across-matchers).

A block declares `constraints`, `alternatives`, or both — but not neither, which
would score nothing. **Every `id` in the block shares one space**: the path ids and
every constraint id, shared and per-path alike, because an id is how the grade names
a sub-check and how [the pre-run gate](#what-is-validated-before-a-run) addresses one
— `trace_checks.<id>` for a shared constraint and
`trace_checks.<path id>.<constraint id>` for one inside a route. A repeat anywhere is
a load error.

### Matchers

`kind` is required on every matcher and nothing is inferred from which predicates
are present, so what a matcher selects is readable from the YAML. Which fields
each kind may carry a predicate on:

| `kind` | matchable |
|---|---|
| `tool_call` | `tool`, `executor`, `args` (nested paths), and `status` / `result` **read from the paired tool result** |
| `tool_result` | `tool`, `executor`, `status`, `result` |
| `assistant_message` | `text` |
| `user_message` | `text` |

A predicate on a field the kind never carries is a **load error**. That is what
makes the timeline's rule — a predicate over a `None` field is unmatched, never
vacuously true — safe: without it an author's typo produces a silently unmatchable
matcher, and the default `on_missing` reports that as the agent's failure.

`kind: tool_call` selecting on its own outcome is the only way to write "a failed
call to X with argument Y", because `arguments` live on the call event and
`status` on its result. A matched `tool_call` contributes the **call event's**
position to ordering, so `before` means "requested before".

`args` addresses nested argument paths by dotted segments, so
`args: { body.resolution_path: { exists: true } }` reaches inside a request body.

**`latency_seconds` is not matchable.** Wall time is not compared across
substrates — it is excluded from the timeline parity suite's compared fields — so
grading must not depend on it.

### Operators

A predicate is the **conjunction of its operators**: every one it declares must
hold, so `{ gt: 0, lt: 100 }` is a range. Seventeen operators:

| operator | holds when |
|---|---|
| `equals` / `not_equals` | the value is (is not) equal |
| `equals_ci` | a string equal to it, case-insensitively |
| `contains` / `contains_ci` | the value contains it, case-sensitively or not |
| `regex` | the pattern **searches** the value — unanchored, and only a string matches |
| `gt` / `gte` / `lt` / `lte` | the value is a real number and the comparison holds |
| `in_` / `not_in` | the value is (is not) a member of the list |
| `len_gt` / `len_gte` | the value has a length, above (at or above) the bound |
| `exists` | the field is present (`exists: false` is the absence primitive) |
| `equals_binding` / `contains_binding` | the same, against a value the constraint's `bind` extracted under that name |

**The two binding operators name a value rather than writing one.** Their argument
is a name declared under the constraint's own `bind.values`, and the comparison
they make is the one `equals` and `contains` make — so a constraint over a single
bound value scores exactly as the same constraint with that value written out. A
name no predicate in the constraint references, and a reference to a name the
constraint does not bind, are both load errors, which is what scopes a correlation
to one constraint.

`contains` **recurses**: against a list, tuple or set it holds when any element
contains the needle, against a dict when any **value** does — keys are never
searched — and against two non-strings it falls back to equality. So
`args: { items: { contains: W1 } }` matches a list holding `W1` and a dict holding
it as a value.

**The numeric comparisons read a real number and nothing else.** A `bool` is not a
number here and neither is a numeric *string*, so `{ gt: 0 }` is false against
`true` and against `"5"` — a JSON body that quotes its numbers needs `equals` on the
string, not a range. `len_gt` / `len_gte` are the same shape one level up: they hold
only where the value has a length (a string, list, dict), so `{ len_gt: 0 }` reads
"non-empty" and is false against a number.

Two limits worth meeting here rather than in a silently ignored predicate:

- **`equals: null` is not expressible.** An operator counts as declared when its
  value is not `null`, which is what keeps a predicate meaning the same thing after
  the gRPC round trip that writes every unset field as `null`. So "this argument is
  JSON `null`" cannot be written; `exists: false` covers the far commoner "the
  argument is absent".
- **There is no `not_contains` / `not_regex`.** A predicate cannot negate a
  substring or pattern match — `negate` operates on a whole constraint, not on one
  predicate — so "select the calls whose url does *not* contain `/admin`" is not a
  *selection*. "Never another customer's record" is `not_equals` on the argument,
  which does ship.

There is no `absent` operator — it is `exists: false`, and an operator named
`absent` beside a *constraint* named `absent` is an ambiguity the vocabulary does
not need. A predicate declaring **no** operator is rejected at load.

### What a matcher resolves to: matched, and undecidable

Resolving a matcher yields two sets — the events that **definitely** match, and the
events **nobody can decide**. Three rules govern them.

**A predicate over a `None` field is unmatched, never vacuously true.** Only
`exists` reads a `None`; every other operator is false there. So
`args: { refund_id: { not_equals: R-1 } }` does **not** hold for a call that carried
no `refund_id` at all — an absent argument satisfies no negative predicate. Write
`exists: false` for "the argument is absent".

**A `tool_call` matcher reads `status` and `result` through the result paired to it
by `call_id`.** The call event carries neither of its own — a `tool_call` event
always has `status: None` — so the pairing is what decides a status predicate, and
a call the trial recorded no result for has no outcome to read at all.

**Evidence only the tool-call record could supply makes an event undecidable.**
`executor` and `status` come from that record alone. An event whose every other
predicate passes, but whose record-only evidence is missing, is neither a match nor
a definite miss: one completion of the record would select it and another would
not. Two ordinary states reach it — a bundle re-graded without its tool-call record,
and a call the agent declared that never executed. **Nothing may read an
undecidable event as a pass in the agent's favour** — the hazard
[G4](#guarantees) names when it says dropping an attempted call makes an `absent`
or `count` constraint wrong in the agent's favour.

Undecidability is scoped **to the matcher**, never to the event kind:

- an event whose other predicates already fail is *decided* — it cannot match at
  any status — so an unexecuted call to a tool the matcher does not name changes
  nothing;
- a matcher over a fully recorded call is *decided*, because the pairing answers
  it.

### The constraint vocabulary — ten members

| kind | payload, by position | meaning | `on_missing` anchor |
|---|---|---|---|
| `present` | `match` | at least one event matches (LTLf `F A`) | rejected |
| `absent` | `match` | no event matches (`G ¬A`) | rejected |
| `count` | `match`, `min`, `max` | the match count is within the bounds | rejected |
| `before` | `left`, `right` | ordering under both quantifiers | both sides |
| `immediately_before` | `left`, `right`, `among` | adjacency in the named view (`A ∧ X B`) | both sides |
| `absent_before` | `forbidden`, `anchor` | `¬A U B` — the no-prefill primitive | `anchor` |
| `absent_between` | `forbidden`, `start`, `end` | nothing forbidden inside the window | `start`, `end` |
| `all_of` | `list` of expressions | conjunction | delegated |
| `any_of` | `list` of expressions | disjunction | delegated |
| `negate` | one expression | negation | delegated |

`on_missing` is rejected over any `require` tree holding `present`, `absent` or
`count`: their verdict *is* the match, so a policy for "the matcher found nothing"
would answer the question the constraint asks. The three composites delegate the
policy to every expression they hold rather than consuming it, so the rejection
reads the whole tree — `on_missing` beside an `all_of` is admitted exactly when
every kind under it anchors something.

There is no `after`, because it reduces exactly:

> `after(left = L:qL, right = R:qR)` ≡ `before(left = R:qR, right = L:qL)`
>
> The two sides swap position; each quantifier **rides with its own matcher** and
> is *not* swapped.

### Quantifiers, and the closed form

`Quantifier = {any, all, first, last}`, required on every side of `before` and
`immediately_before`. `first` / `last` reduce a side to its earliest / latest
match; `any` / `all` quantify over the side's matched set. Every combination
reduces to one comparison of extremes:

| `left` | `right` | `before` holds iff |
|---|---|---|
| `any` | `any` | `min(L) < max(R)` |
| `any` | `all` | `min(L) < min(R)` |
| `all` | `any` | `max(L) < max(R)` |
| `all` | `all` | `max(L) < min(R)` |
| `first` | *q* | as `any`/*q* with `L := {min(L)}` |
| `last` | *q* | as `all`/*q* with `L := {max(L)}` |

and symmetrically on the right: `right: first` reduces `R := {min(R)}`,
`right: last` reduces `R := {max(R)}`.

`immediately_before` reads the same four quantifiers over the same two sides, but
the relation between a left match and a right match is adjacency in the `among`
view rather than order, so it has **no** min/max closed form. It is the quantified
reading of the pairs: `any` / `any` is "some left match is immediately followed by
some right match", `all` / `any` is "every left match is", `any` / `all` is "one
left match is immediately followed by every right match" — satisfiable only where
the right side matched exactly once — and `first` / `last` reduce their side to one
event before the pair is read. Where ordering and adjacency disagree, adjacency is
the stricter: `min(L) < max(R)` holds for any interleaving, while adjacency holds
only for the pairs the view puts side by side.

**Two side types.** A quantifier is a per-side field, never fused into the kind:

- `MatcherSide` = `{quantifier: any | all | first | last, match}` — the two sides
  of `before` and `immediately_before`, where the position is genuinely
  quantified.
- `AnchorSide` = `{quantifier: first | last, match}` — a window anchor. `any` and
  `all` are **rejected at load**: over a prefix or an interval `any` collapses
  onto `first` and `all` onto `last`, so admitting all four would ship two
  verdicts under four spellings. One selected anchor is also what makes a window
  a single interval rather than a cross-product of every start against every end.
- `forbidden` is a **bare matcher with no quantifier**: "no A occurs in the
  window" is inherently universal over A, so a quantifier there names nothing.

**The window rules:**

- `absent_before` — the window is `[0, anchor.position)`, every position strictly
  before the selected anchor.
- `absent_between` — the window is `(start.position, end.position)`, strictly
  between the selected anchors.
- An **inverted or empty** window (`start.position >= end.position`) is
  *unmatched*, not vacuously true: the anchors did not occur in the declared
  order, so `on_missing` decides and defaults to a named failure.

### `immediately_before` requires an explicit `among`

Closed set: `tool_calls`, `tool_results`, `messages`, `events`. **There is no
default.** Events interleave inside a turn — a call's own result sits between it
and the next call — so every candidate default is wrong for some common intent:
`tool_calls` cannot express confirm-before-acting, where one side is a message,
and `events` cannot express two consecutive calls.

### `within` — the turn window

`{first_turn, last_turn}`, inclusive, over the timeline's `turn_index`,
restricting every matcher in that constraint. The opening user prompt **shares
turn 0** with the first assistant turn, so `first_turn: 0` includes it. "Before
the first user message" is therefore not expressible as a window — that window is
always empty — and the intent is `absent_before`.

### Matching a result on a failed call

A `result` predicate loads beside any `status` predicate, or none. Both substrates
record one text for one failure — the four forms are written out beside
[G5](#guarantees) — and the timeline parity suite holds them byte-equal, so
asserting *why* a call failed is as portable as asserting that it did:

```yaml
- id: refused_as_already_refunded
  description: the refund failed because the order was already refunded
  require:
    present:
      match:
        kind: tool_result
        status: { equals: error }
        result: { contains: already refunded }
```

The same holds for a binder extracting `field: result`. What a `result` read still
depends on is the tool-call **record**: on a bundle re-graded without one, the text
comes from the `role: tool` message instead and carries an `Error: ` prefix (G6b).

### `on_missing` — what an unmatched anchor decides

A side or anchor that matched **nothing** leaves the constraint's question unasked:
`before` has no ordering to check, `absent_between` has no window. That is not the
same as the condition failing, so it is decided by `on_missing`, which defaults to
**`fail`** — a named failing sub-check saying which position selected no event.

The default is `fail` because the alternative is a vacuous pass, and a matcher that
selects nothing is far more often an author's typo or an agent that never got
started than a condition genuinely satisfied. `on_missing: pass` is the explicit
opt-in for "this constraint only applies when the anchor occurred".

`on_missing` is rejected at load wherever `present`, `absent` or `count` appears in
the `require` tree, whose verdicts *are* the match. On `present` the pair would be an
always-pass check — unmatched passes by the policy, matched passes by the constraint
— so the load error is what stops a declaration that cannot fail from being written.
Nesting the kind under a composite does not change that: `all_of` / `any_of` /
`negate` pass the policy down unchanged, so the rejection is read off every kind in
the tree and not off the top one. `present` also decides its own empty match as a
failure rather than deferring it to the policy, so the vacuous pass is out of reach
from the evaluator's side too.

### Correlating arguments across matchers

Every literal predicate above compares a field against a value written into the YAML,
which cannot say *this call's id equals that call's id*. A constraint's optional `bind`
says it: one matcher whose events supply candidate values, one or more names
extracted out of each, and predicates elsewhere in the same constraint that
reference those names with
[`equals_binding` / `contains_binding`](#operators).

```yaml
- id: every_record_written_was_read_first
  description: "the agent read each record before it wrote to it"
  bind:
    match: { kind: tool_call, tool: { equals: write_record } }   # which events supply candidates
    values:
      rec: { field: args.record_id }                             # name -> extraction
    on_unbound: fail                                             # fail (default) | pass
  require:
    before:
      left:  { quantifier: any, match: { kind: tool_call, tool: { equals: read_record },
                                         args: { record_id: { equals_binding: rec } } } }
      right: { quantifier: any, match: { kind: tool_call, tool: { equals: write_record },
                                         args: { record_id: { equals_binding: rec } } } }
```

`field` addresses `tool`, `text`, `result` or an `args.<dotted path>` on the kind
`bind.match` selects, and an optional `pattern` narrows it to a regex capture —
`{ field: text, pattern: '\$([0-9][0-9,]*\.[0-9]{2})' }` binds one candidate per
dollar figure the message quotes. A binding is **scoped to its own constraint**:
a name no predicate in that constraint references, and a reference to a name the
constraint does not bind, are both load errors, so a correlation cannot reach
across constraints or across [routes](#alternative-paths).

#### The binder site is the direction of the implication

The `require` tree above is symmetric in `read_record` and `write_record`, and the
`bind` is what makes it an assertion about one of them. **Binding at the write says
"every record written was read first"; binding at the read says "every record read
was later written"** — a different claim, and usually not the one intended. Measured
over the same three-call trajectory `read X`, `read Y`, `write X`:

| binder | candidates | verdict |
|---|---|---|
| `write_record` | `X` | **passes** — the one record written was read |
| `read_record` | `X`, `Y` | **fails** — `Y` was read and never written |

#### Several candidates: the constraint must hold under every one

Quantification over the candidate set is **universal**. Measured, binding at the
write, with each candidate scored by writing its value out as a literal:

| trajectory | candidates | per-candidate | first-match | **universal** | any-satisfying |
|---|---|---|---|---|---|
| `read X`, `write X` | `X` | `X: pass` | pass | **pass** | pass |
| `read X`, `write Y` | `Y` | `Y: fail` | fail | **fail** | fail |
| `read X`, `read Y`, `write X` | `X` | `X: pass` | pass | **pass** | pass |
| `read X`, `write X`, `write Y` | `X`, `Y` | `X: pass`, `Y: fail` | pass | **fail** | pass |
| `read X` twice, `write X` | `X` | `X: pass` | pass | **pass** | pass |
| `read X` only | — | — | `on_unbound` | `on_unbound` | `on_unbound` |

Row 4 is why **any-satisfying is not the rule**: the agent wrote a record it never
read, and an existential reading passes because a *different* write happened to be
correlated — the check silently stops covering everything the author did not think
to enumerate, which is the whole reason to write a correlation instead of literals.

**First-match is not the rule** either, and its cost shows up when the binder site
is the read. Measured:

| trajectory | candidates | first-match | **universal** |
|---|---|---|---|
| `read X`, `read Y`, `write X` | `X`, `Y` | pass | **fail** |
| `read Y`, `read X`, `write X` | `Y`, `X` | fail | **fail** |

Those two rows are the same set of actions in a different order. First-match is
deterministic but flips the verdict on the order of two reads the constraint says
nothing about; the universal reading is invariant under any permutation of the
binder's events, which is what makes the grade reproducible. It also makes the
*reported* binding reproducible, because the report is the **set** of values that
failed and a set has nothing to choose:

```
before is unmatched: left selected no event; failed under (rec='Y')
```

**Candidates are distinct values, not events.** Ten calls naming one record are one
reading of the `require` tree rather than ten identical ones, and the failure names
one value rather than the same value ten times. Two candidates are the same when
every name's value is equal **and of the same type**, so `True` and `1` are two
candidates.

A bound constraint costs one evaluation of its `require` tree per distinct
candidate, so it multiplies whatever the tree already costs rather than adding to
it — [measured](#declared-limits-and-what-owns-each) beside the `absent_between`
shape it compounds with.

#### `on_unbound` — the trial where the binder selected nothing

The universal reading is vacuously true over an empty candidate set, and
`on_unbound` overrides it. It defaults to **`fail`**, which is right for
read-before-write: zero writes means the agent did not do the task, and a vacuous
pass there is exactly the hazard [`on_missing`](#on_missing--what-an-unmatched-anchor-decides)
defaults against for the same reason. `on_unbound: pass` is the opt-in for a
constraint whose empty case genuinely holds — "no figure the agent quoted was
invented" is satisfied by an agent that quoted no figure, and failing it charges a
second time for a gap another check already charges.

This is a policy over **decidable** evidence: the candidate set is genuinely empty,
not unreadable — the unreadable case is
[below](#a-candidate-set-the-trial-cannot-determine). `on_unbound: pass` beside
`severity: gate` is a load error, since a gate carries no weight for the second
charge to be avoided on.

#### A candidate set the trial cannot determine

The binder resolves through the same matcher machinery every predicate does, so it
has [undecidable](#what-a-matcher-resolves-to-matched-and-undecidable) events of its
own: a `bind.match` reading `status` on a call the trial recorded no outcome for
cannot say whether that call is a candidate. An extraction can go unread the same
way — a `field: result` on a call with no recorded outcome is a candidate whose
*value* the trial does not carry, where a `field: args.<path>` the call simply did
not pass binds nothing at all. Absent value, absent evidence: the same distinction a
predicate over the field draws.

That second reading is about an argument the agent omitted, not one the author
mistyped: an extraction naming an argument the *tool* does not declare is rejected by
[`tolokaforge validate`](#what-is-validated-before-a-run) before the run, because it
binds nothing on every trajectory and the default `on_unbound` would charge that to
the agent.

So the candidate set is three-valued too, and the constraint is decided only where
every completion of it agrees. Writing `D` for the definite candidates and `U` for
the undecidable ones, the readings compared are the **empty** one, `D ∪ {u}` for
**each** `u` in `U`, and `D ∪ U`.

The singletons are not redundant beside the two ends. With `D` empty the empty
reading is `on_unbound` rather than a vacuous pass, so both ends can read *fail*
where a completion binding one satisfied candidate holds — measured, at `D = {}`,
`U = {u₀ holds, u₁ fails}`, `on_unbound: fail`:

| reading | verdict |
|---|---|
| `{}` | fails — nothing bound, and `on_unbound` is `fail` |
| `{u₀}` | **passes** |
| `{u₁}` | fails |
| `{u₀, u₁}` | fails |

Comparing the two ends alone reports a definite failure there, on evidence the trial
does not carry. That is the same over-fail the `count` bound guards against by
reading the whole reachable interval rather than its endpoints, met one level up.

An undecidable candidate set that changes no verdict is not reported: where the
completions agree, the missing evidence changed nothing an author can act on. Where
they disagree the constraint is undecided and says which evidence is missing and
where, and a value the trial definitely binds absorbs an undecidable reading of the
same value — it is in the set whatever the missing evidence says.

#### `negate`, and `within`

Quantification is outermost, so `negate` inside a bound constraint reads
**`∀v ¬P(v)`** — "no candidate satisfies `P`" — and not `¬∀v P(v)`. A
`negate: { present: … }` over a binder on `write_record` therefore fails as soon as
*one* written record was read, which is the useful reading; a reader expecting
`¬∀ = ∃¬` would predict the opposite.

`within` restricts the binder too, because the binder resolves through the same
turn window every other matcher in the constraint does. A window that excludes an
event removes the values it carried from the candidate set.

#### The bound value's type is load-bearing

`contains` compares two strings as substrings and falls back to **equality** for
any other pair, and `equals` over a string and a non-string is false outright — so a
bound `int` is neither found inside a string nor equal to one. Measured:

```python
contains("http://api/deliveries/4021", 4021)    # False
contains("http://api/deliveries/4021", "4021")  # True
contains(4021, "http://api/deliveries/4021")    # False — the reverse direction
contains(["W1", "W2"], "W1")                    # True  — descent finds a scalar
contains(["W1", "W2"], ["W1"])                  # False — and never a container
1 == 1.0 == True                                # True  — three JSON types, one value
```

Which pairs can ever hold is a per-operator table, not "do the two types differ":
`equals_binding` holds across `integer` / `number` / `boolean` and between two
arrays or two objects, while `contains_binding` over an array or object **haystack**
holds against any scalar **needle** by descent — and against no container one,
because the descent never compares a container to what it is looking for.

A **binding reference** between a text field and a value bound out of an integer
argument is therefore false on **every** trajectory, `equals_binding` exactly as much
as `contains_binding`. That is not scored as an agent failure: the constraint fails
with a message saying the comparison was not made, naming the binding, its value, its
type, the operator that could not make the comparison, and the two ways to write the
intent — a reference on an `args` predicate, which compares two arguments as they
were written, or a `pattern` capture taken off a field that holds text.

**`tolokaforge validate` catches it first, wherever a schema declares the type.**
The config models cannot: `args.reason_code` is a string and `args.delivery_id` is an
integer, and `BoundValue` cannot tell them apart, so a model-tier rejection broad
enough to catch the second would refuse the first. The declared type lives in the
tool's JSON schema, which the [authoring gate](#what-is-validated-before-a-run) holds
— so the misuse is an **error** before the trial is paid for, on a schema forbidding
extras, and an advisory on one permitting them.

**The gate is the only tier holding the reverse direction.** The evaluation-time
message above is raised from the value the *predicate* reads, and only where that
value is a string, so a text binding correlated against a natively-typed argument
passes it and the constraint reads as `present is unmatched` — the message a genuine
agent miss carries. Every never-true shape the gate can type is answered before the
run; the residue it cannot type — no schema resolved, a path below its first segment,
a property writing no `type` or one outside the six JSON type names — is `unchecked`,
and there nothing backstops the reverse direction at all.

### When a constraint cannot be decided

A matcher yields definitely-matching events and
[undecidable](#what-a-matcher-resolves-to-matched-and-undecidable) ones. A
constraint is decided only when **every** completion of the undecidable evidence
reaches the same verdict; otherwise it is **undecided**, which is a failing
sub-check naming the constraint and the evidence the trial does not carry.

The verdict carries it as a field. `grade.yaml`'s `trace_check_results` entries
each hold `undecided`, `true` exactly where the fold reached no verdict, so
"the agent did not do this" and "nobody wrote down what it did" are told apart
without reading `message` prose. `passed: false` beside `undecided: true` is the
only pairing an undecided verdict takes — see
[`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md#trace-check-verdicts).

Worked, over *d* definite matches and *u* undecidable ones:

| constraint | verdict |
|---|---|
| `present` | passes at `d >= 1` — **even with undecidables present** — fails at `d = u = 0`, undecided at `d = 0 < u` |
| `absent` | fails at `d >= 1`, passes at `d = u = 0`, undecided at `d = 0 < u` |
| `count` | passes when every count in `[d, d + u]` is within the bounds, fails when none is, undecided otherwise |
| `before`, `immediately_before`, `absent_before`, `absent_between` | decided when every reading of each side agrees; undecided otherwise |
| `all_of`, `any_of`, `negate` | Kleene: a conjunction with a failing branch fails whatever the undecided branch would have said, a disjunction with a holding branch passes, and otherwise an undecided branch makes the composite undecided |
| any of the above declaring [`bind`](#correlating-arguments-across-matchers) | the **candidate set is itself subject to the rule**: decided where the empty reading, each single undecidable candidate beside the definite ones, and all of them together agree — undecided otherwise, and `on_unbound` supplies the empty reading rather than a vacuous pass. [Worked](#a-candidate-set-the-trial-cannot-determine) |
| any of the above carrying [`severity: gate`](#severity--a-check-that-must-hold) | undecided **trips the gate** — a scored constraint forfeits its weight there, and a gate's forfeit is the trial |

Undecided is not a pass in the agent's favour and not an over-fail either: definite
evidence answers the question wherever it can. A trial bundle carries its tool-call
record as the `tool_log.yaml` sidecar, so a pack re-graded from one reaches the
verdict the live run reached — which is what
[`tolokaforge retrace`](TRACE_REPLAY.md) re-checks a whole recorded corpus for,
spending nothing. A bundle written before that sidecar existed carries the message
trace alone, and there every `status` and `executor` predicate is unreadable, every
`result` on an unanswered call with it, and so is any binder that reads one — such a
bundle reports undecided, permanently. That is the right semantics rather than a
defect to work around: the evidence really is absent, and the alternative is a
silent pass.

### Weighting the constraints

The component score is `Σ(weight · passed) / Σ(weight)`, `weight` defaulting to
`1.0`, so a pack that omits every weight scores the plain fraction of constraints
that passed. `weight` must be **positive**: a zero weight is a declared check that
contributes to neither the numerator nor the denominator, and "evaluated but not
scored" is what [`severity: gate`](#severity--a-check-that-must-hold) is for.

Prefer uniform weights. Reach for `weight` when migrating an already-weighted
criterion, not to express that one condition feels more important — a weight map
tuned until the numbers look right is a grader fitted to the trajectories it was
tuned on. The same guidance the rubric weights carry applies here.

**`any_of` is not multi-path grading.** A disjunction over flat constraints lets an
agent satisfy half of one route and half of another: `any_of: [A_step1, B_step1]`
combined with `any_of: [A_step2, B_step2]` passes for an agent that did `A_step1`
and `B_step2`, which is neither route. Grading genuinely alternative routes needs
the paths declared as wholes, which is [`alternatives`](#alternative-paths).

### `severity` — a check that must hold

`severity: gate` marks a constraint that is not scored and must hold. A gate is
excluded from the weighted average — it enters neither the numerator nor the
denominator — so writing a `weight` beside one is a load error naming the key
nothing reads. So is [`on_missing: pass`](#on_missing--what-an-unmatched-anchor-decides):
it would open the gate on every trial whose anchor matched nothing. That policy earns
its place on a *scored* constraint, where an unmatched anchor is already charged to
the check that asked whether the thing happened and charging it again would cost the
agent the same failure twice; a gate carries no share, so there is no second charge
to avoid and the pass buys nothing but a check that must hold holding vacuously.
`severity: scored` is the default and is the constraint that carries a share.

This is the same concept as the rubric judge's
[required criterion](#required-gate-semantics), reached by the same reasoning: some
conditions are not worth a fraction of the score, they are the condition the trial
is allowed to exist under. Reach for a gate where a partial score would be
misleading rather than merely low — a forbidden tool called, another customer's
record touched, an order mutated by a diagnose-only agent.

A tripped gate takes the component to `0.0` and fails the trial, whatever the scored
constraints said, and the grade names the gates that tripped.

**A gate nobody can decide trips.** Undecided is not a pass in the agent's favour
anywhere in this vocabulary, and a gate is the one check the author said must hold —
an undecided gate that opened would be a silent pass on exactly that check, and would
leave a gate *weaker* than the scored constraint it replaced. The consequence is
sharpest on a bundle written before the tool-call record was persisted, which
[cannot read `status` or `executor` at all](#when-a-constraint-cannot-be-decided), so
a gate reading either fails every trial re-graded from one. Write gates over evidence
the message view carries — which
tool was called, with which arguments, in which order — and keep `status` and
`executor` for scored constraints, where the same limit costs a weight rather than
the trial.

**A block of nothing but gates scores the gate verdict:** `1.0` when every gate held,
`0.0` otherwise. There is no weighted average to take — every member is excluded from
it — and this is the same collapse the judge's
[all-required rubric](#required-gate-semantics) already returns. It applies to a flat
`constraints` list and to a route's decision set alike.

**A route with nothing scored beside a route that has something scored is a load
error**, not a collapse. Such a route is `1.0` wherever its gates hold, so it ties or
beats every scored sibling on every trajectory and the component reports the gate
instead of the route the agent walked. The gate belongs in the shared `constraints`,
where it applies whichever route was taken. A block whose routes are *all* gate-only
is admitted: there is no scored sibling for one to stand in front of, and the block
asks the gates' own question whichever route the agent walked.

### Alternative paths

`alternatives` declares two or more routes, each a `TracePath` carrying an `id`, a
`description` and its own `constraints`. A path is a named **whole**, which is what
separates it from `any_of`: an agent is measured against one route at a time, so
half of one plus half of another is not a passing trajectory on either.

```yaml
trace_checks:
  alternatives:
    - id: served_vs_source
      description: "the bug is located by comparing the served body against the source"
      constraints:
        - id: source_fetched
          description: "the source document is read"
          require: { present: { match: { kind: tool_call, tool: { equals: read_source } } } }
    - id: cache_inspector
      description: "the bug is located by reading the cache inspector"
      constraints:
        - id: inspector_read
          description: "the cache inspector is queried"
          require: { present: { match: { kind: tool_call, tool: { equals: cache_inspect } } } }
```

`constraints` may be omitted entirely when every check belongs to a route. **Two is
the floor**: one path is the flat form written the long way round — the best of one
path is that path — so a single-path block is rejected at load, pointing the author
at `constraints:`.

#### How a multi-path block scores

Each path is scored over its **decision set**: the shared `constraints` plus that
path's own. Every path therefore carries the shared checks, and each path's score is
normalised within its own set — which is why paths need no weights, and why a long
route is not penalised for being long.

```
scoreᵢ = Σ(weight · passed) / Σ(weight)   over the non-gate members of Dᵢ
scoreᵢ = 1.0 if every gate in Dᵢ held else 0.0   when Dᵢ has no non-gate member

winner        = the highest-scoring path; a tie goes to a path whose gate shut,
                and between clean paths to the first declared
gate_failed   = some gate in the winner's decision set did not hold
component     = 0.0 if gate_failed else the winner's score
```

The grade records the winner by id, and one line per path carrying that path's own
score and whether its gates held — so "did it win by a mile or a hair" and "do models
cluster on one route" are answerable from `grade.yaml` alone. A path's recorded score
is never zeroed by a gate; only the component is.

The argmax runs over **every** path, including ones whose gates did not hold. A path
is not dropped from contention for failing its own gate: dropping it would let an
agent violate the gate on the route it scored highest on, fall through to a
lower-scoring clean route, and pass. For the same reason a **tie goes to the path
whose gate shut** — otherwise the trial's verdict would turn on which of two
equal-scoring routes the author happened to write first. Preferring the gated path
in a tie can only ever shut a component and never rescue one, so it closes a cell
rather than opening one.

#### Shared gates and path gates: when each is appropriate

A gate in the shared `constraints` is in every decision set, so it applies whichever
route the agent took. A gate **inside a path** is a *process* gate: it constrains how
that route must be walked, and it is consulted only on the route the agent actually
took.

> **A gate that must hold whatever route the agent took belongs in shared
> `constraints`, never inside a path.**

A forbidden tool called, another customer's record touched, an order mutated by a
diagnose-only agent — all route-independent, so all shared gates. The last is a
shipped pack: see [`cache_debug`](#four-worked-packs), whose one gate is shared for
exactly this reason.

This rule is guidance rather than a guarantee, and the reason is worth stating
plainly: a path gate has an escape. Trip route A's gate *and* score **strictly below**
a clean route B, and A's gate is never consulted — the trial passes with the
forbidden action performed. Scoring merely *level* with B no longer escapes, because
a tie goes to the gated path, but scoring below it still does and no rule for path
gates closes that. Preferring a gate-clean
path is worse in the same place (an agent escapes a gate by violating it on the route
it scores highest on), and consulting every gate everywhere fails an agent for
tripping a gate on a route it did not take, which is the premise of the feature. A
shared gate has no escape, which is why route-independent conditions belong there.

**Every component the pack configures needs a weight of its own.** A configured
component absent from `combine.weights` is refused before the run, and a component a
substrate scored anyway makes the fold raise on both substrates — neither may pick a
share, because `1.0` invents one the author never gave and `0.0` discards a verdict the
substrate produced. `tests/canonical/test_example_pack_grading_corpus.py` holds every
shipped example pack to that, reading the combine that is *effective* after the project
layer merges.

### Four worked packs

[`examples/native/multi_service_helpdesk_workflow`](../examples/native/multi_service_helpdesk_workflow/README.md)
grades the process alongside the substrate. Its three constraints are the three
shapes an author reaches for most, and each one is written so a plausible wrong
trajectory fails it and the other two pass:

| constraint | kind | the wrong process it catches |
|---|---|---|
| the query rides in the `POST /search` body | `present` over `args: { json.q: { len_gt: 0 } }` | the query went somewhere other than the body the service reads |
| the policy is read before the case is written | `before`, `first` / `first` | the resolution was recorded first and justified afterwards |
| the delivery is not annotated before the policy read | `absent_before` | the agent guessed the path and wrote it onto the delivery |

The first is the assertion `transcript_rules` cannot express at all: it matches a
**nested argument path** inside the request body, where `required_actions` compares
whole argument values for exact equality.

[`examples/native/multi_service_cache_debug`](../examples/native/multi_service_cache_debug/README.md)
is the multi-path and gate reference. It is a diagnose-only task whose own rubric
reference names **two** comparisons as locating the bug, so the two are declared as
`alternatives` and the component is the better route's:

| check | where | the wrong process it catches |
|---|---|---|
| `no_status_was_written` | shared, `severity: gate` | the agent "fixed" the symptom with `POST /orders/4021` instead of diagnosing it |
| `the_note_was_written` | shared | the trial ended with no root-cause note |
| `both_api_layer_reads_happened` | route `divergence_between_the_api_layers` | a key listing stood in for the source-of-truth read, so no divergence was observed |
| `both_api_layer_reads_precede_the_note` | route `divergence_between_the_api_layers` | the note was written first and the source read afterwards |
| `the_cached_value_and_an_api_read_happened` | route `divergence_against_the_cache` | the cache inspector was never opened |
| `the_cache_comparison_precedes_the_note` | route `divergence_against_the_cache` | the cached value was read after the note that claims to explain it |
| `the_note_quotes_the_value_the_served_read_returned` | route `divergence_between_the_api_layers` | the note recites the mechanism without quoting anything the agent observed |
| `the_note_quotes_the_value_the_cache_held` | route `divergence_against_the_cache` | as above, off the cached read |

Four authoring choices in it are worth copying:

- **The gate is shared, not per-route.** "Do not mutate on a diagnose-only task"
  holds whichever comparison the agent chose, and a gate inside a route is consulted
  only when that route wins — so a route gate here would be escapable by winning on
  the other one. This is the [rule above](#shared-gates-and-path-gates-when-each-is-appropriate)
  applied to a real pack.
- **Each route asks three questions**: were both sides of the comparison read, did
  the reads that happened happen before the note, and does the note quote the value
  the route's own read returned. The ordering check carries `on_missing: pass` and
  the grounded-claim check `on_unbound: pass`, so a read that never happened is
  charged once — to the presence check — rather than three times, which is what lets
  each check fail on its own wrong process rather than cascading. The three are not
  independent as a result: with neither read performed the ordering check is vacuous
  and the binder selects nothing, so a trajectory that writes the note and nothing
  else scores the same `3/4` as one that starts a route and abandons it — four
  equal-weighted scored members, the route's three plus the shared
  `the_note_was_written`, of which only the presence check fails.
- **The judge stays dominant.** The routes are not equally probative: the cache
  inspector shows the stale value itself, while the served-vs-source comparison
  shows only that the read path serves something the database disagrees with. The
  deterministic components therefore sum to less than `pass_threshold`, so no trial
  passes on process alone.
- **The grounded-claim check is per route, and the binder is that route's own read.**
  No single read is common to both routes, so a shared binder would have
  route-dependent candidates and could fail a correct route. Each route binds the
  status token out of the read it guarantees and requires the note to quote it, which
  names no status value and so generalises to whatever the cache is holding. It
  carries `on_unbound: pass` for the same charge-once reason the ordering checks carry
  `on_missing: pass`, and its `require` is an `any_of` whose first branch is "no note
  was written at all" — `on_missing` is [rejected over a tree holding a `present`](#on_missing--what-an-unmatched-anchor-decides)
  at any depth, so the branch is how the same intent is written there. The two capture patterns
  differ because the payloads do: `http_request` renders a JSON response as the parsed
  object's Python `repr`, so the served read shows single-quoted keys while the cache
  inspector's nested JSON string keeps the double quotes it was serialised with. Bind
  against the payload the service really answers with, not against the one the schema
  suggests.

[`examples/native/multi_service_lot_ops`](../examples/native/multi_service_lot_ops/README.md)
is the correlation reference. Its substrate oracle reads the `corrective_actions` row
that exists and cannot say how the values in it were obtained, and its own task
guidance already demands a process nothing in its fold checked — "GET the reason-code
catalog to find the contamination code before opening the action; do not guess it":

| constraint | shape | the wrong process it catches |
|---|---|---|
| `the_reason_code_posted_was_read_from_the_catalog` | `bind` `code` from the POST's `args.json.reason_code`; `before` any successful result `contains_binding` it, then the first POST | the code was written from memory, or fabricated, rather than looked up |
| `the_lot_was_read_before_the_action_was_opened` | `bind` `lot_url` from the POST's `args.url` by a regex capture; `before` any `GET` whose url `equals_binding` it, then the first POST | the action was opened against a lot the agent never read |
| `exactly_one_corrective_action_was_opened` | `count { max: 1 }` over the POST, `severity: gate` | the action is double-posted, leaving the operator a duplicate to reconcile |

Two statements generalise out of it, and both are the difference between a correlation
that earns its weight and one that decorates a pack:

- **A correlation earns its weight only where the substrate oracle cannot already see
  the answer.** The flagship pack's only same-type correlation — the resolution path
  written onto the delivery is the one recorded on the case — is already pinned to
  `reschedule` by two independent db_probes, so adding it would catch nothing and
  would be a check written to satisfy a corpus test. Here the probe reads the row the
  POST created; it has no view of where the code came from, so the correlation is the
  only thing in the fold that asks. Stated precisely: it beats **the fold**, not a
  hard-coded `contains: CAPA-01` on every trajectory — wherever the probe passes, the
  posted code *is* `CAPA-01` and both select the same events. What the binding adds
  over the literal is the fabricated-code trajectory, and that a new code in the
  catalog needs no constraint edit.
- **A correlation over a short token is a correlation over noise — bind the widest
  unambiguous span.** The lot id lives in the URL path, so the obvious capture binds
  `"7"`, and `contains(".../lots/1007", "7")` is `True`: an agent that read the lot
  *code* as though it were the id would pass. Capturing the whole `http://…/lots/7`
  prefix and comparing it with `equals_binding` has no substring reading at all, and
  it still names no lot number. There is no load-time answer to this — the value is
  runtime — so it is an authoring rule rather than a rejected shape.
- **The prompt is a second oracle: check the correlation against it, not only against
  the substrate.** A grounded-claim check is evidence of grounding only where the
  bound token reached the agent through the substrate alone. Everything the trial
  shows the agent before it acts — `initial_user_message`, the user persona and its
  backstory, `policies.guidance` — is a place the answer can already be sitting, and
  a note paraphrasing the request would then satisfy the check having observed
  nothing. `cache_debug` is authored around this: its on-call engineer reports an
  out-of-date status and does not know which one, so `processing` is nowhere in the
  prompt and reproducing it means the agent read a layer. `lot_ops_01` is the same
  discipline from the other side — the persona withholds the reason code and the
  task's own guidance says not to guess it, so the catalog is the only place `CAPA-01`
  comes from. Read the prompt before shipping a correlation; the substrate probe will
  not tell you the answer was in the question.

[`examples/native/native_shared_domain`](../examples/native/native_shared_domain/README.md)
is the migration reference — the one shipped pack where a judge criterion and a trace
constraint grade the same policy, each holding the half it can see. Its
`add_note_duplicate_check_gated` / `add_note_duplicate_check_policy` pair grades a
check-for-duplicates-first policy with two conjuncts:

| the half | who checks it | why that one |
|---|---|---|
| `list_notes` ran before `add_note` | `the_notes_were_listed_before_the_note_was_added`, shared, `severity: gate` | a trajectory predicate: deterministic, free, and re-checkable over a recorded run forever |
| the user was warned about the near-duplicate | `checked_duplicates_first`, `kind: binary`, `required: true` | a judgment about what the assistant *said*, which no tool record answers |

Three authoring choices in it are the ones to copy:

- **The veto survives on both halves, and that is a load-time rule rather than a
  convention.** The criterion is `required: true`, so it carries a trial-level veto and
  **no score share** — retiring or narrowing it moves the judge score not at all, and only
  the veto is at stake. The declaration is therefore only accepted because the constraint
  claiming it is **shared** and carries `severity: gate`
  ([the veto rule](#declaring-a-migration-the-migrationyaml-sidecar)); the criterion stays
  `required: true` for the conjunct it holds. Two vetoes over one policy, and either fails
  the trial alone.
- **The judge's `reference` stops asking for the half it no longer grades.** A reference
  still describing the ordering would have the judge charging a conjunct the gate already
  owns, so the narrow would be a text change and nothing else. It says outright that the
  ordering is checked deterministically and is not the judge's to grade.
- **Neither component can carry a trial by itself.** `combine.weights` is
  `{llm_judge: 0.7, trace_checks: 0.3}` against `pass_threshold: 0.75`, so a pass means
  both halves happened — a `trace_checks` weight is mandatory rather than optional here,
  because a scored component with no declared weight makes the fold raise on both
  substrates rather than being handed a share nobody declared.

The pair is also the corpus behind its own migration: both arms declare it in a
[`migration.yaml`](#declaring-a-migration-the-migrationyaml-sidecar), and
[`tolokaforge reconcile`](RUBRIC_MIGRATION.md) re-checks the declaration against the
seventeen recorded judge verdicts under
`tests/data/migration_corpora/notes_duplicate_check/` at zero cost — 17 observations,
κ `1.0`, `no_counter_evidence`. What that verdict does and does not say is
[RUBRIC_MIGRATION.md § Reading the evidence](RUBRIC_MIGRATION.md#reading-the-evidence);
the mode is the **author's** recorded judgment, and the residual claim — the warning the
judge still reads — is its justification.

#### What a correlation is a candidate to replace, and what it is not

`lot_ops_01` and `cache_debug` each declare, in a [`migration.yaml` sidecar](#declaring-a-migration-the-migrationyaml-sidecar)
beside their `grading.yaml`, the judge criterion their new checks are a `candidate` for.
**Neither retires one**, and a `candidate` changes no grading: the criterion keeps its
weight and its veto, and the declaration is the claim to be measured. Each pack's header
comment points at its sidecar; what a retirement would still have to answer for is written
in the sidecar beside the entry, and summarised here because correlation is what surfaced it.

`by` is a **conjunction** — every constraint it names must pass for the recomputed label to
count as met — so a check is named there only where it is about the same proposition as the
criterion. `lot_ops_01`'s reason-code correlation is therefore *not* part of its candidacy: a
trial that grounded the lot correctly and got the reason code wrong would count against a claim
about the lot. It is a candidate for nothing the pack currently declares, which is the honest
state — no criterion in that rubric is about the code.

| new check | candidate for | what a retirement would still owe |
|---|---|---|
| `lot_ops_01`'s lot correlation | `names_lot` (binary, `required: true`) | a shared `severity: gate` constraint, because the correlation is *scored* and the criterion is a veto — the veto rule refuses the conversion at load. The criterion also accepts *either* `LOT-1007` or `lot 7`, where a binding is one exact value |
| `cache_debug`'s two grounded-claim checks | `explains_mechanism` (graded, weight `1.0`) | a `combine_weights` map for the freed score share, which the freed-share rule requires of a scored conversion. The checks also reach only the half asking the note to be grounded in the observed divergence, not the causal account of why the write leaves the cache stale, which no exact or textual check expresses |

1. **The two conversions are unsafe in opposite directions, and a different rule refuses
   each.** `names_lot` is `required: true` — a trial-level veto carrying **zero score
   share** — so migrating it converts that veto into either a `severity: gate`, which is
   [escapable inside `alternatives`](#shared-gates-and-path-gates-when-each-is-appropriate),
   or a fraction of a scored component; both are strictly weaker than what they replace and
   the weakening is invisible in the component score. `explains_mechanism` is `kind: graded`
   with no `required` flag, so it carries the opposite hazard: its weight sits in the judge
   component's denominator, and dropping it raises the component by `+0.667` on a trial that
   scored it `0.0`. The [veto rule and the freed-share rule](#declaring-a-migration-the-migrationyaml-sidecar)
   are what refuse each conversion at load.
2. **The bar is agreement against recorded judge verdicts, and neither pack has a single
   recorded trial.** [`tolokaforge reconcile`](RUBRIC_MIGRATION.md) needs Cohen's κ over the
   joined labels to be **defined**, which needs judge verdicts on both sides of the
   criterion. It reads those verdicts out of the bundles under `--source`, reporting an entry
   only where a bundle resolves to the pack declaring it — so for these two there is nothing
   to reconcile yet rather than a verdict that falls short. A judge-labelled corpus per
   rubric pack is **#793**.

#### Declaring a migration: the `migration.yaml` sidecar

A task directory may carry a `migration.yaml` beside its `grading.yaml`, naming each rubric
criterion its constraints are a candidate for, have narrowed, or have replaced. The file is
optional; a pack without one is unchanged. Nothing about grading reads it — it records the
claim and the evidence behind it so [`tolokaforge reconcile`](RUBRIC_MIGRATION.md) can check
that claim against recorded judge verdicts, which is why it is a sidecar and not a
`grading.yaml` key: `GradingConfig` is
`extra="ignore"`, so a `migration:` key there is silently dropped, and making it a real key
needs a `GRADING_KEYS` manifest entry, whose every `KeyKind` describes score production.

```yaml
migrations:
  - criterion: checked_duplicates_first        # the rubric criterion id
    mode: candidate | narrowed | retired
    by: [the_notes_were_listed_before_the_note_was_added]   # trace_checks ids in this pack
    was: { kind: binary, required: true, weight: 1.0, description: "<the text measured against>" }
    residual: { kind: none | text, reason: "<why nothing remains / what remains>" }
    combine_weights: { llm_judge: 0.7, trace_checks: 0.3 }  # post-migration combine.weights
    evidence: { corpus: tests/data/migration_corpora/notes_duplicate_check, observations: 17, kappa: 1.0 }
    acknowledged: [ { trial: <bundle path under evidence.corpus>, reason: "<why the judge was wrong>" } ]
```

`residual` is a model rather than a string because no one scalar carries both a sentinel and
free text: `residual: none` parses to the non-empty string `'none'` while `residual: null`
parses to `None`, so a single field answers the wrong question for one of the two modes
whichever way it is spelled. Its presence and its kind are a **total function of `mode`** —
absent for `candidate`, `kind: text` for `narrowed`, `kind: none` for `retired` — so a reader
can tell what an entry claims from its mode alone.

Every rule below is an **error** naming what to write instead. They are checked by
`tolokaforge validate`, and deliberately not by the pre-run gate: the file cannot affect a
grade, so a run must not abort on authoring metadata.

| rule | why |
|---|---|
| `candidate` — the criterion exists and `was` matches its current shape exactly; no `evidence` and no `residual` | a candidacy is against the criterion as it stands and has measured nothing yet, so it has neither a conclusion's support nor a judgment about a migration that happened — either would park a claim no evidence ever checks |
| `narrowed` — the criterion exists, `was.description` differs from it, `evidence` and `residual.kind: text` | a narrow that shortened no text narrowed nothing |
| **`was` cross-check**, `narrowed` only — `was.required` and `was.kind` must equal the criterion's current ones, while **`was.weight` is deliberately free** | every other rule reads `was`, so an unchecked `was.required: false` escapes the veto rule below outright, and a flipped `kind` makes the recorded evidence incomparable with what the judge scores after the migration. `weight` is left out because a criterion that now asks less may legitimately weigh less, and requiring a match would refuse a correct migration while adding nothing against the escape, which turns entirely on `required` |
| `retired` — the criterion is **absent** from the rubric, `evidence` and `residual.kind: none` with a reason | zero disagreements satisfies `narrowed`'s condition and `retired`'s alike, so the choice is the author's and is recorded here. Its `was` is not cross-checked: the criterion is gone from the pack, so no load-time source holds its pre-migration shape |
| every `by` id resolves in this pack's `trace_checks`, shared or inside a route | a migration is *by* the checks that replace the criterion |
| **veto rule** — a `narrowed` / `retired` entry whose `was.required` is true may only name **shared** constraints carrying `severity: gate` | a required criterion is a trial-level veto with no score share, so retiring one moves the judge score not at all; a route-scoped gate is [escapable inside `alternatives`](#shared-gates-and-path-gates-when-each-is-appropriate) and a scored constraint is a fraction of a component where a veto was |
| **freed-share rule** — a `narrowed` / `retired` entry on a criterion that is *not* required must declare `combine_weights` | a scored criterion's weight is in the judge component's denominator, so removing one the agent failed makes the judge *more generous* — `+0.667` on `cache_debug`'s `explains_mechanism`, on a trial that scored it `0.0`. The declaration is **unconditional** for a scored conversion: an author who shifts nothing declares the **identity map**, which a reviewer reads in the diff where an implied claim is invisible. It is a claim rather than a proof, and `tolokaforge reconcile`'s report shows per trial what the declared map does to the judge component and the trial verdict |
| every `acknowledged.trial` is a bundle under `evidence.corpus` | a waiver addresses a disagreement the verdict measured |

A `candidate` entry is charged neither the veto rule nor the freed-share rule: it replaces
nothing, so the criterion keeps its veto and its score share whatever it names. Both shipped
candidacies name scored constraints, which is exactly what those two rules refuse for a
narrow or a retirement.

### Declared limits, and what owns each

Named here so an author meets them in the docs rather than in a check that quietly
does nothing. Each is a separate issue's to close; none is worked around in the
evaluator.

| limit | owner |
|---|---|
| An `args` path is checked only at its first segment, so a typo below it is reported as unchecked rather than caught | #765 |
| Migrating a rubric criterion into a constraint needs recorded judge verdicts to decide it, and one pack in the corpus has them. The machinery ships — the [`migration.yaml` declaration](#declaring-a-migration-the-migrationyaml-sidecar), its two load-time hazard rules, and [`tolokaforge reconcile`](RUBRIC_MIGRATION.md)'s bar — and one criterion is narrowed against a committed corpus. The other rubric packs have no recorded verdicts, so a [declared candidacy](#what-a-correlation-is-a-candidate-to-replace-and-what-it-is-not) there has nothing to be decided against | #793 |
| `executor` never distinguishes a user-side call, because no code path builds one | #688 |
| A harness-side `TRIAL_NOT_FOUND` is recorded as a tool error, so a `status` matcher reads it as the agent's failure | #727 |

Wall-clock time is not on the list: `latency_seconds` is deliberately unmatchable
and stays so, because it is not compared across substrates.

One cost shape worth knowing when authoring: `absent_between` evaluates the
product of its `start` readings, its `end` readings and its `forbidden` readings,
so on a timeline where all three matchers are undecidable its work grows cubically
in the number of undecidable events. Trials in the size range the harness produces
stay well inside that, and a records-present timeline has no undecidable events at
all. A [`bind`](#correlating-arguments-across-matchers) multiplies whatever its
`require` tree costs by the number of distinct candidates, so the two compose.
Measured over a bound `absent_between`, the worst combination the vocabulary allows:

| calls on the timeline | distinct candidates | one reading | the bound constraint |
|---|---|---|---|
| 20 | 5 | 0.97 ms | 4.8 ms |
| 60 | 15 | 2.3 ms | 34 ms |
| 200 | 40 | 4.9 ms | 211 ms |
| 600 | 100 | 15 ms | 1.4 s |

The multiplier is the candidate count and nothing worse — the readings do not
compound each other. Distinct-value counts on real trajectories are a handful per
`(tool, argument)`, so none of this is a reason to author around at the sizes the
harness produces.

---

## What is validated before a run

A mis-authored check is charged to the agent or to nobody: a misspelled tool name in
a `present` matcher scores the component `0.0` with the message a genuine agent
failure carries, the same typo under `absent` passes every trial, and an
uncompilable `regex` raises inside the evaluator once the tokens are spent. So a
task's whole grading block is checked against its tools before anything is paid for — by
`tolokaforge validate`, which exits non-zero, and by the run's own pre-flight, which
makes one pass over every selected task before it schedules the first trial and
aborts naming **every** offending task. The same pass runs at `tolokaforge prepare`,
so a distributed enqueue is rejected once rather than by every worker identically.

The named list is of packs that **load** and cannot be graded. A pack the loader itself
refuses — a malformed grading shape, the file's own or one of its keys; a grading file
that is not parseable YAML; a task naming an `initial_state.json_db` that is not on
disk, read to hold `id_fields` against the tables it seeds; an adapter backend the host
has not installed — stops the pass where it stands with its own sentence, and the packs
behind it are not read. #880
owns folding that class into the named list.

Findings come in three classes:

| rule | class | where |
|---|---|---|
| a `tool: { equals: X }` or `{ in_: [X, …] }` naming a tool outside the task's declared set | error | every `trace_checks` matcher |
| `required_tools` / `disallowed_tools` naming a tool outside that set | error | `transcript_rules.tool_expectations` |
| an `args` address whose first segment is outside the properties of a tool whose schema forbids extras | error | every matcher's `args` key, every `bind.values[*].field` |
| the same against a tool whose schema permits extras | advisory | as above |
| a `bind.values[*].field` the tool types `integer` / `number` / `boolean` / `array` / `object`, read by a reference on one of the event's text fields — `tool`, `text`, `result`, `status`, `executor` — or beside a `regex` on the same predicate | error on a schema forbidding extras, advisory on one permitting them | every `bind.values[*].field` |
| a `bind.values[*].pattern` over an argument the tool types `integer` / `number` / `boolean` / `array` / `object`, or over a bare `field: args` — a capture is taken off text alone, so the name binds on no trajectory | error on a schema forbidding extras, advisory on one permitting them | `bind.values[*].pattern` |
| a reference on an `args` predicate whose declared type and the binding's declared type no value of either can satisfy the operator between — `equals_binding` across `integer` / `number` / `boolean` holds, `contains_binding` finds a scalar inside a container and a container inside nothing | error only where **both** schemas forbid extras, advisory wherever either permits them | the predicate's own `args.<path>` |
| the same reference where the argument's schema writes no `type`, or writes one outside the six JSON type names | unchecked | as above |
| a `regex` pattern that does not compile | error | every predicate, every `bind.values[*].pattern`, plus `transcript_rules.disallow_regex` |
| a `state_checks`, `transcript_rules` or `custom_checks` section written as an empty mapping | error | that section |
| a `state_checks` block declaring no source at all — no non-empty `jsonpaths`, no `db_probes`, and a `hash` block naming neither its flag nor a source | error | `state_checks` |
| `db_probes` beside a non-empty `jsonpaths`, or beside a `hash` block enabled with a source — raised as a config load error before the gate is reached, so it is reported alone | error | `state_checks.db_probes` |
| a `state_checks.id_fields` entry naming a table absent from the seeded `initial_state`, a key component absent from every seeded record of its table, or a key that does not uniquely identify those records — where the caller resolved the seeded tables (a native pack, at `validate` and at the pre-run gate) | error | `state_checks.id_fields` |
| a `transcript_rules` block declaring no rule at all — every list empty, both turn bounds absent, and a `tool_expectations` expecting neither tool | error | `transcript_rules` |
| a `custom_checks` block with no `enabled` key, which the component's own default leaves unrun | error | `custom_checks` |
| any hash source declared under a `hash.enabled` that is not truthy — written `false`, `0`, `null`, or absent — wherever the adapter answers at all, whatever it answers: a source the block declares and nothing reads is the author's defect regardless | error, one for the block | `state_checks.hash.<the declared source>` |
| a truthy `state_checks.hash.enabled` with no source — no non-empty `golden_actions`, no truthy `expect_initial_state` — where the adapter reports that nothing lies beneath the authored block, which is what `adapter_type: native` means | error | `state_checks.hash.enabled` |
| the same shape where the adapter reports the source it supplies beneath the block and that source is **usable** — the frozen-core convention, a golden-actions fixture the block never names | no finding: checked and passed | — |
| the same shape where the adapter reports that source **missing or empty** — the trial would be paid for and take no hash verdict — the message naming the fixture in the adapter's own vocabulary | error | `state_checks.hash.enabled` |
| a truthy `expect_initial_state` beside another hash source — raised as a config load error wherever the block is constructed, so it is reported alone | error | `state_checks.hash.expect_initial_state` |
| either hash flag/source mismatch above, where **no** adapter answers — the declared `adapter_type` names an adapter this environment has not installed, or one that has not implemented the hook | unchecked | the address the error would have carried |
| a truthy `golden_actions` that is not a list of actions, under a truthy `hash.enabled` and whatever else the block declares — the description build raises on the same shape, so a run's pre-flight aborts on it before the gate is reached and only `tolokaforge validate` reports it as a finding | error | `state_checks.hash.golden_actions` |
| a golden action naming a tool outside the task's declared set, under a truthy `hash.enabled` | error | `state_checks.hash.golden_actions[i].name` |
| a golden action declaring no usable name — the key absent, `""`, `null`, or a value that is no string — under the same flag | error | as above |
| a task giving its golden replay no world to be built in — no `initial_state.json_db` naming a JSON file, or no `tools.agent.mcp_server` — where `golden_actions` is the effective hash source | error, one per withheld fact | `state_checks.hash.golden_actions` |
| a component the pack configures with no weight in the **effective** `combine.weights` | error | `combine.weights.<component>` |
| a weight naming a component the pack does not configure, or naming no component at all | error | `combine.weights.<key>` |
| a task naming no grading source at all — no `grading:` field and no sibling `grading.yaml` — where its declared `adapter_type` is `native` | error | the task itself — this refusal carries no block address, because there is no block |
| a task naming a grading file with nothing at the path it resolves to, where its declared `adapter_type` is `native` | error | as above |
| either absence where the task declares any other `adapter_type` | unchecked | `grading` |
| a tool set the loader cannot resolve for this task | unchecked | whole block |
| what a task gives a golden replay, where no caller resolved it | unchecked | `state_checks.hash.golden_actions` |
| an `id_fields` declaration where no caller resolved the seeded tables — the declared `adapter_type` is not `native`, or names an adapter this environment has not installed | unchecked | `state_checks.id_fields` |
| an effective `combine` no caller could resolve | unchecked | `combine.weights` |
| an `args` address on a tool whose schema did not resolve | unchecked | per matcher, per extraction |
| an `args` address below its first segment | unchecked | per path |
| a `bind.values[*].field` whose property writes no `type` | unchecked | per extraction |

An **error** always fails the pack. An **advisory** fails it unless
`evaluation.grading_validation.fail_on: error` is set on the run config: an MCP
tool's schema declares its properties but permits others, so an unknown argument
name there is a probable typo rather than a certainty, and hard-failing would
enforce a claim the schema does not make.

**`unchecked` never fails anything.** It is a separate channel, not a third
severity: nothing reads it to decide whether to raise, so the gate has no
false-reject mode. It is surfaced beside the task all the same — `validate` prints
it, a run logs it — because a gate that could check nothing must not read as a clean
bill of health. A task whose tool set the loader cannot resolve, an MCP pack that
commits no `fixtures/tools.json`, an `args` address below its first segment, a property
whose schema writes no `type`, a replay world no caller resolved, an `id_fields`
declaration whose seeded tables no caller resolved, a hash block whose flag and source
disagree under an external adapter that may supply the source itself,
and a task with no grading block on disk under an adapter that resolves its own all
land here.

**Having no grading block on disk is answered by the adapter the task declares.**
`get_grading_config` is abstract and the implementations disagree: the native adapter
grades from the file the `grading:` field names, while an external adapter may
synthesise a whole grading config without reading that field. So a task with no block
to read is refused where it declares `native` — the run cannot grade it — and reported
unchecked where it declares anything else, since nothing here can say what that adapter
would do with the absence. There are two ways to have no block, and they draw the same
decision under the same sentence structure: a task naming no source at all is refused
naming the task and both ways to supply one, and a task naming a path with no file at
it is refused naming the task, the ref it wrote, the path that ref resolved to, and the
two ways out — correct the path, or create the file. Both answers are decided before
any block is read, which is why neither carries an address inside one. A task naming a
grading file that *is* on disk is gated on that file's contents whatever it declares.

**A section the author wrote declares something to evaluate.** An empty block asserts
nothing and scores nothing, and it cannot survive translation either: the wire erases
an authored empty `state_checks` or `transcript_rules` to an absent section, so while
the shape loads no predicate can answer "did the author write this?" the same way on
both substrates. The error names what to declare, or says to drop the block. Two of
the five components already answer this at load: a `trace_checks` block declaring
neither constraints nor alternatives and an `llm_judge` block with no rubric are both
unrepresentable.

All three sections the rule reaches carry it one step further, because each has keys
that configure how the component runs rather than declaring what it checks. A block
holding only such keys asserts exactly as little as an empty one, and each took a
vacuous pass for it while reading as configured:

| shape | why it asserts nothing |
|---|---|
| `state_checks` with `jsonpaths: []`, or only `id_fields` / `relaxed_validation` | no source any substrate can read |
| `transcript_rules` whose every rule list is empty — `required_actions: []`, `must_contain: []`, a `tool_expectations` expecting neither tool | no rule any substrate can evaluate; the component is averaged over no sub-check |
| `custom_checks` naming a `file` with no `enabled` key | `CustomChecksConfig.enabled` defaults to `false`, so the suite never runs |

Each rule reads its keys for **truth**, not presence, because that is what both
substrates do: an empty `golden_actions` replays nothing, an empty `required_actions`
requires nothing.

**Two state sources, one of them a probe, is refused as well** — the mirror of the
no-source rule above, and the two divide the block between them. `db_probes` beside a
non-empty `jsonpaths`, or beside a `hash` block that is enabled and names either of its
two sources, hands one component two candidate scores with
no share to fold them by, and the substrates would not even discard the same one: only
the runner evaluates a probe, while core folds the hash with the assertions. Neither
config model loads the block, so core raises where the grading config is built and the
runner at `RegisterTrial`, naming both sources and the two fixes — keep the probes and
drop the other source, or drop the probes and let the hash and `jsonpaths` grade the
state. Probes beside a *disabled* hash still load: that hash produces no verdict, so
nothing is discarded, and an enabled hash with nothing to compare against is refused at
the flag by its own rule.

**One surface answers it, and it is the model rather than the gate.**
`tolokaforge validate` constructs the core `state_checks` config on **every** declared
block before it runs the gate, so this rule arrives as a load error whichever source the
probe was written beside, and it is the only defect reported — the rest of the pack is
checked on the next run of `validate`.

**Every golden action names a tool the task gives its actors.** A name that resolves to
nothing costs the whole trial: both substrates resolve the authored names before the
first action runs and refuse the replay outright, so the tokens are spent and no
state-hash verdict comes back at all (see
[Hash-Based Grading](#hash-based-grading-tau-bench-compatible)). An action with no
`name` key, `name: ""`, `name: null`, or — `golden_actions` claiming nothing about its
elements (#907) — a `name` written as anything but a string resolves to nothing the same
way and draws the same
error, and each offending action is addressed by its own index — a name may repeat, and a
nameless action carries nothing else to tell it apart by.

The gate resolves those names against **the tools the task declares** —
`tools.agent.enabled ∪ tools.user.enabled` — which is stricter than either substrate
resolves at replay time: core matches the pack's `TOOLS` map and the runner the tools it
registered for the trial, and neither is readable before a run without importing the
pack's server module. A native pack whose golden action names a `TOOLS` entry it gives
no actor therefore replays but is refused here; no pack in the repository has that
shape, and #815 owns unifying the three namespaces.

Like the source rule beside it, this one reads only a hash block whose flag is truthy,
because a source under a falsy flag is resolved by nobody and refusing it would be
stricter than the grade. Such a block is refused by that rule instead, at the flag
rather than at the name: a `golden_actions` list under `hash.enabled: false` replays on
neither substrate whatever its names are, so what an author fixes is the flag or the
source, and naming an action nothing was ever going to run would send them to the wrong
line.

**A golden replay needs a world to be built in, and the task supplies it.** Two facts,
both written in `task.yaml` and neither of them readable from `grading.yaml`:
`initial_state.json_db` as a path to a JSON file under the task directory — an inline
mapping there supplies no file — and `tools.agent.mcp_server`, the module holding the
tools the actions call. Without them core hashes nothing and raises rather than grading
around the hash (see [Hash-Based Grading](#hash-based-grading-tau-bench-compatible)), so
the gate refuses the shape and the whole trial is never paid for. Each withheld fact is
its own error naming its own key, for the reason each unreplayable action is: an author
supplying two of them otherwise pays a grading pass per omission.

The rule reads the block the way **core** reads it — the flag, then `golden_actions`,
the one source that replays anything. A pack whose source is `expect_initial_state` is
outside the rule entirely: it compares in process against the state the task starts in,
so it needs no world and demanding one would send its author to declare facts nothing
reads. This rule reads `golden_actions` for truthiness and never for shape;
the rule beside it reads the shape and nothing else. So a truthy non-list value under an
incomplete world draws **both** findings at that one address — one naming the fact the
task withholds, one naming a source that is no list of actions — because both are true
and each names a different fix, which is the same reason two withheld facts draw two
findings. The name rule reports nothing about such a value, having no element to address.

The world is the caller's to resolve, the way the tool set is: `tolokaforge validate` and
the run's pre-flight both hold the `TaskConfig`, and a caller holding none — the
trace-replay batch and the rubric migration, which check a `trace_checks` fragment
against a bundle's recorded tools — reports `unchecked` where the rule would have run and
nowhere else. A task an adapter other than the native one owns reports `unchecked` too,
because `tools.agent.mcp_server` is the native reading of a task's server module.

An explicit opt-out is *not* "declares nothing": `custom_checks: {enabled: false}`
states a decision, survives the wire intact, and is read the same way by both
substrates — so it loads, it is not requested, and it needs no weight. The unflagged
block is the shape that needs the rule most, because it escapes the weight rules too:
it is not requested, so no weight is owed, and a pack whose `custom_checks` is its
only section then lands the free pass a pack asking for nothing has earned — scoring
`1.0` on a suite that never ran.

**A component and its weight must name each other, in both directions.** Configuring
a section asks for that component to be scored, and declaring a weight asks for a
component to be folded; either one alone leaves the fold reading a map the author did
not write. Both directions are errors at the gate, each naming the two one-line
fixes — declare the weight, or drop the section; configure the section, or drop the
weight — because the substrates do not answer an undeclared weight the same way (see
[Score Combination](#score-combination)). A weight key naming no component at all
takes the second fix only: `combine.weights` validates no key, so a typo there reaches
both folds unread.

Both rules read the **effective** `combine`, a task's own block layered over its
project's `task_defaults.grading_defaults.combine`, because a task that declares no
`combine` at all still inherits one — five `example-microservices-pack` tasks inherit
their weights that way. A caller that cannot resolve the effective combine reports it
`unchecked` rather than assuming the weights are absent, which would refuse every pack whose
weights are inherited. A pack that deliberately scores nothing — no component section
and no weights — is clean: it asks for nothing, so nothing is missing.

Only the first segment of an `args` address is checked, and only against
`properties`. `json.q` on `http_request` is checked at `json` and stops, because
`json`'s own schema declares no properties and nothing below it is answerable. A tool
named by `regex` rather than by `equals` / `in_` produces no finding at all: a pattern
names a set, not a token.

**Every matcher rule reaches every matcher the block declares.** A matcher lives in
three places — on a shared constraint's `require` tree, on an
[alternative route's](#alternative-paths) constraint, and on a
[binder's](#correlating-arguments-across-matchers) `bind.match` — and all three are
graded identically, so a misspelled tool is one defect wherever it sits. What differs
is the blast radius, and the address is what records it: `trace_checks.<id>` for a
shared constraint against `trace_checks.<path id>.<constraint id>` inside a route,
the block's single id space keeping those two apart, with `.bind.match` naming the
binder rather than the `require` tree. Under `present` a route-local typo lets that
route be walked in full and still score below its siblings; under `absent` it passes
on every trajectory; inside a binder it selects no event, so the binding yields no
assignment and the default `on_unbound` charges that to the agent.

**The type a binder extracts is checked wherever the schema declares it.** `contains`
compares two strings as substrings and falls back to equality for every other pair,
and `equals_binding` *is* that equality — so a value bound out of an `integer`
argument and read by a predicate on one of the event's five text fields —
`tool` / `text` / `result` / `status` / `executor`, the last two typed by closed
vocabularies that subclass `str`, so the value compared is text like the rest — or
beside a `regex` that asserts the same of an argument, is false on **every**
trajectory. That is the [type limit](#the-bound-values-type-is-load-bearing)
answered before the run: the declared type lives in the tool's JSON schema, and the
gate is the only tier holding it.

**An `args` predicate is checked against both declared types, not exempted.** A
reference there compares two arguments as the tools typed them, which is the
correlation the feature exists for — and is false on every trajectory where no pair
of values of those two types could satisfy the operator. So `read_file.path`
correlated with a binding off `read_file.offset` is reported, and so is the reverse,
while `read_file.limit` against that same `offset` binding is not: the answer comes
from the [comparability table](#the-bound-values-type-is-load-bearing) rather than
from whether the two names differ. This is the one rule resting on **two** schemas'
claims, so the weaker decides: an error only where both forbid extra arguments, an
advisory wherever either permits them. An extraction no schema describes still has a
type — `tool`, `text` and `result` are text and a bare `field: args` is the argument
mapping — and a predicate carrying a `regex` beside its reference is left to the rule
above, which reports that same mistake at the extraction's address.

**A capture is text only where the value beneath it is**: a `pattern` narrows a
string and yields nothing off anything else, so a capture over an argument the schema
types `integer` / `number` / `boolean` / `array` / `object` binds no name on any
trajectory, and that is reported at the extraction's `pattern` key — the key the
author deletes to fix it — rather than at its `field`.

**A binder reading `field: result` makes its pack records-dependent.** `result`
comes from the tool-call record wherever one exists and from the answering
`role: tool` message otherwise ([G6b](#guarantees)), and on a **failed** call those
two differ by the `Error: ` prefix the message carries — so a binder over a failure
extracts one text on a fresh run and a prefixed one on a bundle re-graded without
its `tool_log.yaml` sidecar. A binder whose `match` also carries a `status`
predicate is undecidable there outright, `status` being a field only the record
holds. A binder over `args` has neither split. Not a finding: the gate reads the
block, not the bundle it will be graded against. It is stated here because it is
the kind of consequence a re-graded bundle otherwise surfaces months later.

**A block that scores nothing is rejected.** `trace_checks` declaring neither
`constraints` nor `alternatives` asserts nothing; `alternatives` carrying fewer than
two paths is the flat form written the long way round; and an `id` repeated anywhere
in the block's one id space makes two sub-check results indistinguishable. A
`weight` beside [`severity: gate`](#severity--a-check-that-must-hold) is rejected for
the neighbouring reason — a gate enters neither the numerator nor the denominator, so
the weight is a declared key nothing reads. Two more are rejected for what they do to
the fold rather than to the block: `on_missing: pass` beside a gate, which opens it on
every trial whose anchor matched nothing; and a route whose decision set — the shared
constraints plus its own — has no scored member while another route's does, which is a
constant `1.0` standing in front of every scored sibling. All six are load errors
naming what to write instead.

**An ordering over one matcher is rejected unless some trajectory decides it.**
Writing the same matcher on both sides of `before`, or forbidding the very events an
`absent_before` / `absent_between` window is measured from, usually yields a
constant: nothing follows the last of a matched set and nothing precedes the first.
Ten of the 38 quantifier combinations still say something, and the rest are load
errors.

The readings below are what the evaluator answers under the default `on_missing`,
measured at zero to four matching calls:

| shape | survives | what it reads as |
|---|---|---|
| `before`, same matcher both sides | `left ∈ {first, any}` **and** `right ∈ {last, any}` | the events occur at least twice |
| `immediately_before`, same matcher both sides | as above | the events occur at least twice — except `first` before `last`, which reads **exactly** twice, since a third match sits between them |
| `absent_before`, forbidding its own anchor | `anchor: last` | the events occur once |
| `absent_between`, forbidding its own anchors | `start: first`, `end: last` | the events occur exactly twice |

Twenty-seven of the 28 rejected shapes are constants — false at every trajectory —
and are rejected naming the quantifiers that would express the intent instead. The
twenty-eighth is rejected for a different reason: **`absent_before` forbidding its
own anchor, anchored `first`, is not a constant.** Nothing precedes the first of the
matched events, so the constraint reduces to *the events occurred at all* — a
`present` constraint written the long way round, which is what its message tells the
author to write. The rejection is against pathological authoring, not against a check
no trajectory moves.

**What no static rule can answer is whether a constraint separates anything.** The
gate reads the block, not the trials: a correctly authored constraint that passes every
trial the pack ever ran adds no signal to the pack, and nothing about the block says
so. That question is empirical, and [`tolokaforge retrace`](TRACE_REPLAY.md) answers it
over a recorded corpus for free — per constraint, whether any trial it evaluated
disagreed with any other, and how much of the corpus could decide it at all.

### Which keys a grading block refuses

A `grading.yaml` has two tiers of key, and they answer a misspelling differently.

**Inside a typed block, an unknown key is a load error.** All five —
`combine`, `state_checks`, `transcript_rules`, `trace_checks` and `llm_judge` — refuse
a key their model does not declare, on every construction path. Nearly every field in
them carries a default, so a dropped key would substitute one silently: the mis-keyed
rule or source simply leaves the fold, and the surviving weight renormalises to a score
the author never asked for. For three of them — `combine`, `state_checks` and
`transcript_rules` — `tolokaforge validate` says more than
the model's bare `extra_forbidden` can: the file, the offending key, its closest
declared field and the whole accepted set. `trace_checks` draws that bare refusal, and
`llm_judge` draws it only on the `rubric` / `model_ref` shapes its own migration names,
which are the shapes `validate` constructs it for at all.

**One tier further down, the positions those blocks nest get the same message.** Two
shapes reach it. A `required_actions` or `communicate_info` element refuses a key it
does not declare, and `validate` names it with the element's index —
`transcript_rules.required_actions[0]` — beside the closest declared field and that
element's accepted set (`action_id`, `requestor`, `name`, `arguments`, `compare_args`
for one; `info`, `required` for the other). A block a field holds whole —
`state_checks.hash` and `transcript_rules.tool_expectations` — is named by its dotted
path and answered the same way: `state_checks.hash accepts: enabled,
expect_initial_state, golden_actions, weight, description`. Every
field at this tier has
a default a dropped key would substitute silently: `compare_args` resolving to `None`
compares **every** declared argument, so a `compare_arg` typo makes the check strictly
harder than its author wrote it and fails trials that satisfy what they wrote, and a key
`hash` does not declare requests *nothing*, leaving the hash unscored while the trial
grades on whatever survives beside it.

`state_checks` has two exceptions, and they are not leniency. A **populated**
`env_assertions` or `db_hash_check` draws the migration message naming the check that
replaces it, which the unknown-key refusal knows nothing about; an **inert** one
(`env_assertions: []` / `db_hash_check: false`) is dropped, so a recorded trial bundle
serialized against the old schema still loads.

`state_checks.hash` has one, and it splits the two differently. `expected_state_hash` is
dropped by the block **whatever its value**, so the recorded bundles that stored a digest
still load and nothing downstream reads it; the migration message naming both replacements
is raised instead by the three reads a *pack* passes through — `tolokaforge validate`,
`NativeAdapter.get_grading_config` and `NativeAdapter.to_task_description` — because an
author can act on it where a recorded trial cannot. A stored digest is written in one
substrate's hash algebra and the other cannot compare against it (#915), which is why the
replacements name a state rather than a digest.

**The block-name tier is lenient.** `GradingConfig` and the `project.yaml` twin
`GradingDefaults` ignore a key they do not declare, so `state_cheks:` for
`state_checks:` drops a whole grading component. `tolokaforge validate` catches that
**when the correct name is weighted in `combine.weights`** — the weight then names a
component the pack no longer configures, which is its own error — and says nothing
when the block was never weighted. #533 owns the tier; #874 owns its `project.yaml`
instance.

**`custom_checks` key *names* are refused only at grade time.** Its *shape* is refused at
load, on every surface, like every other grading key — see
[§ What shape a grading key must be](#what-shape-a-grading-key-must-be). But
`GradingConfig.custom_checks` is a raw `dict[str, Any]`, so no key *name* inside it is
checked at authoring time: a misspelled `timout_seconds` passes `tolokaforge validate`
(measured). The gate does read the block's `enabled` key — the two rows naming
`custom_checks` in the findings table above are its rules — and nothing else in it. The
`CustomChecksConfig` that
*does* refuse it is `extra="forbid"` and is constructed when the suite runs — core-side
in the grading engine, runner-side at grade time — so the author hears it after the
trial is paid for. #873 owns closing that gap.

**A dict-typed field's contents are values, not keys**, so no `extra` setting reaches
them: the `state_checks.jsonpaths[*]` operators are policed by their own rules instead
(see [§ The `jsonpaths` assertion vocabulary](#the-jsonpaths-assertion-vocabulary)).

**What this means for a pack read by an engine of another release.** An engine from
this release onward **refuses** a grading key its own model does not declare, so a pack
written for a later release fails to load on it instead of grading with the key
silently ignored. An engine older than this release ignores such a key — a model's
`extra` setting is fixed when the engine is built, so no already-shipped engine changes
what it does. The runner-side half of the same skew — a key one substrate declares and
the other does not, rejected at `RegisterTrial` — is
[`RUNNER.md`](RUNNER.md#engine--image-version-lock).

### What shape a grading key must be

The tier above the key names: every key a `grading.yaml` may carry — `combine`,
`state_checks`, `transcript_rules`, `trace_checks`, `llm_judge` and `custom_checks` —
is a **mapping, or nothing at all**. A bare key with nothing under it is the *absent*
block: the file reads exactly as one that never declared the key, which for `combine`
means every field falls through to its default and for the other five means the
component is absent (`GradingConfig.state_checks is None`).

An **empty mapping** is a different shape from a bare key, and this gate is not what
answers it — the rules policing a block's *contents* are. Measured: `state_checks: {}`,
`transcript_rules: {}` and `custom_checks: {}` are refused by the rule that a block
declaring nothing asserts nothing (the findings table above), `trace_checks: {}` by its
own model, and `combine: {}` / `llm_judge: {}` are accepted.

Any other shape is refused in one sentence naming the grading file, the key, what it
received and how to write it. **The refusal is total over every grading key, and it is
the same sentence on every surface that loads a pack for validation or for a run**:
`tolokaforge validate`, `NativeAdapter.get_grading_config` and
`NativeAdapter.to_task_description`. So a de-indented block is answered identically
whether an author validates the pack, a run's pre-flight reads it, or the description
build lowers it onto the wire — including on `tolokaforge run-trial`, which runs no
pre-flight of its own and is protected by the read site. Every offending key is named in
one raise, so a file that lost its indentation in more than one place is fixed in a
single pass. A whole `grading.yaml` that is not a mapping at all draws the same refusal,
naming the file and its shape.

The refusal never consults truthiness, and that is the point. Writing a check directly
under `state_checks:` instead of under one of the block's own keys makes the block a
list, and `state_checks: []` is the same authoring mistake as
`state_checks: [{path: "$.db.orders[0].status"}]`. Only the second crashes whoever
indexes it; the first reads as a block that scores nothing — the pack builds a
description, a trial is scheduled and paid for, and the mistake surfaces while artifacts
are written. That is the quieter and far more expensive failure, which is why both are
refused at load.

The migration for either is the same: indent the block's own keys one level under the
key rather than writing its contents beside it.

**One value below the key names carries its own shape rule.**
`state_checks.hash.golden_actions` is neither a grading key nor a block — it is a declared
field of the `hash` block, annotated to claim nothing about the value it holds or the
elements inside it (#907) — so the refusal above says nothing about it. It is the list of
actions to replay, or there is no replay: a falsy value loads at
every read site as nothing to replay, and a truthy value that is not a list can be replayed
by neither substrate and is refused by the golden-replay precondition, at the authoring
gate and again at each substrate's own read of the block — core reaching that read without
passing through this loader at all.
[§ Hash-Based Grading](#hash-based-grading-tau-bench-compatible) carries the shape, the
element rule beside it, and what a falsy source then *grades* as, which the two substrates
answer differently.

One shape is still answered differently per surface: an **empty** `grading.yaml`. A file
with no content is not content of the wrong type, so `validate` accepts it while
`get_grading_config` raises an `AttributeError` naming neither the file nor a fix
(#879 owns that tier).

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
  deterministic oracle (`golden_actions`, `expect_initial_state`, `jsonpath_checks`) is
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

**Where the gate is applied.** `aggregate_rubric` reports the gate as
`gate_failed` beside a weighted average it does not touch, so the aggregate on
its own says nothing about the gate: on a rubric whose non-required criteria all
scored full marks, a trial that *failed* a required criterion still aggregates to
`score: 1.0`. Zeroing the component is
[`compose_runner_trial_verdict`](#score-combination)'s, and it is what the wire
grade and the reasons string carry — measured on the five bundles under
`tests/data/migration_corpora/notes_duplicate_check/not_met/`, whose `grade.yaml`
records `components.llm_judge: 0.0` where the aggregate alone gives `1.0`. Read the
aggregate's `score` without the gate and every one of them reads as a trial that
aced the rubric it failed.

`trace_checks` states the same concept as
[`severity: gate`](#severity--a-check-that-must-hold), with the same semantics. Two
spellings because the two vocabularies are authored separately; one behaviour,
because a gate that meant something different in each would be a trap.

### The two weighting layers

Weights act at **two distinct levels**, and they compose multiplicatively:

1. **Per-criterion `weight`** (inside the rubric) — sets each criterion's share
   of the **judge component score**. Non-required criteria aggregate as
   `Σ(weight · score) / Σ(weight)` → a single `llm_judge` score in `[0, 1]`.
   Required criteria are gates, not weighted contributors.
2. **`weights.llm_judge`** (top-level `combine`) — scales that whole judge
   component against the other components in the final-score formula below.

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
the other four components don't express: arithmetic over final DB
rows, invariants that span multiple tables, transcript patterns tied to
computed values. Each `@check` returns `CheckPassed` / `CheckFailed` /
`CheckSkipped`; per-check results ride the wire as `CustomCheckResult`
entries and the aggregate `CheckResultSet.aggregate_score` fills the
`custom_checks` component.

`aggregate_score` averages the checks that reached a verdict and excludes the
skips, so a suite whose **every** check skipped — and one whose file declared no
check — decided nothing. Both substrates leave the component **unscored** there
rather than folding the `0.0` that averaging nothing produces: a component scored
against no evidence fails the trial for the author's unmet precondition rather than
for anything the agent did. The fold then decides and says so, naming
`custom_checks` among the components that produced no verdict. A suite that could
not *run* is a different answer and keeps its `0.0` under `fail_on_error: true` —
checks meant to decide the trial and unable to are a failure, not an absence.

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
| any reason, with `grading_error` set | ungradeable | Grading refused, so no verdict exists. Counted for the same reason a harness error is — the fault is ours — and reported separately as `ungradeable`. This is read **before** the reason, so a refusal is never traded for an exclusion |

The asymmetry decides every borderline case: misclassifying an agent failure as
infrastructure raises every published number with nothing in the output to show
it, while misclassifying infrastructure as an agent failure lowers them by a
bounded amount that `infrastructure_aborts` makes visible. So an unrecognised
termination reason is counted, and a rate-limit-shaped message with no typed
exception behind it terminates as `error` rather than buying its way out of the
benchmark.

`outcomes_by_reason` records every observed reason with the class it was counted
as, so any of these judgements can be recomputed from a finished run's aggregate
without a rerun. An ungradeable trial's row is keyed `ungradeable_<reason>` —
`ungradeable_agent_done` for the common case — which keeps one key mapping to
exactly one class while leaving the reason legible, so the graded and ungradeable
halves of one reason stay separable from the aggregate alone. See
[`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md) § Run-level
metric denominators and [`docs/ANALYTICS.md`](ANALYTICS.md) § The denominator:
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

`db_probes` is the sole state source for a task that declares it: a probe declared
beside a non-empty `jsonpaths`, or beside a `hash` block that is enabled with a source,
is **refused** — those sources score the same component, so one verdict would fill it
and discard the other. There are two fixes, and which one you want depends on what
should decide the state: **drop the probes** and let the hash and `jsonpaths` grade it,
or **drop the other source** and let the probe grade it. The probe is the only one of
the three that reads the live substrate through an independent role, and the only one
core cannot read at all — so a pack you grade outside the runner keeps its verdict by
taking the first fix, and a pack whose real oracle is the database takes the second.
The refusal is at load and on both substrates, from one message:
core raises where the grading config is built and the runner at `RegisterTrial`, so no
trial is paid for first. `tolokaforge validate` reports it earlier still, as the same
load error: it constructs the core `state_checks` config on every declared block before
it runs the gate (see
[What is validated before a run](#what-is-validated-before-a-run)). A run's
pre-flight resolves each pack's description before it reaches the gate, so the pass
stops at the first pack carrying the shape. The fold is
the last line of defence behind all of them: `resolve_state_checks_component` raises on a
probe score arriving beside a hash or JSONPath verdict, so a config that reached grading
without passing a gate — one built directly against the runner, or recorded before the
rule — fails loud rather than discarding a verdict
(see [`GRPC_PROTOCOL.md`](GRPC_PROTOCOL.md#gradetrial-error-semantics)). Runner-side the
probe score *is* the `state_checks` component, and it combines with `transcript_rules` /
`llm_judge` through the normal weighted combine below.

**It is runner-only, so core declines to score a probe-only pack.** The DSN resolves
inside the task's docker network, which the runner container joins and the host-side
`GradingEngine` does not, so core has no probe evaluator and the pack's only state
source produces no core-side verdict: `state_checks` is left **unevaluated** there
rather than filled by whatever else the block happens to carry. Grading such a pack
outside the runner therefore decides it on its remaining components — which is why
`tolokaforge validate` and the host-side helpers are not a substitute for a real
runner-side grade on these packs.

A probe can encode **policy correctness**, not just existence: assert the
specific value a policy selects (`resolution_path == "reschedule"`) rather than
that any well-formed row was written, so an agent that takes a plausible-but-wrong
path grades down even though its row parses. The
[`multi_service_helpdesk_workflow`](../examples/native/multi_service_helpdesk_workflow/)
pack is the adversarial example — three resolution paths look defensible; the
probe passes only for the one the after-hours policy permits.

---

## Score Combination

The block declares exactly three keys — `method`, `weights` and `pass_threshold` —
and any other key is refused at load. Every field here has a default, so a key the
block does not declare would grade the pack by a value nobody wrote: `pass_treshold:
0.95` folds at `0.8`. The refusal lives on the model, so it holds wherever the block
is constructed, `project.yaml`'s `task_defaults.grading_defaults.combine` included —
a project declaring an unknown key there fails project load, naming the dotted path
to it.

For the block inside a `grading.yaml`, `tolokaforge validate` says what the model
alone cannot: the file, the offending key, its closest declared field and the whole
accepted set, so the fix needs no trip to the schema. A typo in a `project.yaml` is
answered one step earlier and by the model alone — that file fails to load before any
task under it is read, so the message is the dotted path above rather than the
did-you-mean.

`combine.method` names the rule that folds the scored components into one score and
one pass flag. Three methods are supported, and both substrates dispatch on the same
closed set — anything else fails the load, naming what an author may write instead.

| `method` | score | `binary_pass` |
|---|---|---|
| `weighted` (default) | the weighted mean below | `score >= pass_threshold` |
| `all` | the **weakest** component's score | every component `>= pass_threshold` |
| `any` | the **strongest** component's score | **any** component `>= pass_threshold` |

> **`any` inflates a score and can pass a failing trial.** It reports the best
> component and ignores the rest, so a trial whose other declared, weighted
> components all scored `0.0` still passes with a full `1.0` — including one that
> failed its state hash. On components scoring `0.0` and `1.0` at
> `pass_threshold: 0.8`, `weighted` gives `(0.5, False)`, `all` gives `(0.0, False)`
> and `any` gives `(1.0, True)`. Declare it only when one satisfied component is
> genuinely the whole objective.

`all` and `any` compare each component to `pass_threshold` and never scale it by
`combine.weights` — measured, weights of `0.9`/`0.1` and of `1.0`/`1.0` give both
methods the same answer on the same components. What they aggregate is the map of
components, and **every component in that map carries a share the author declared.**

A pack configuring a component and declaring no weight for it — or weighting one it never
configures — is refused before the run, in both directions, against the effective
`combine`. See [What is validated before a run](#what-is-validated-before-a-run). The gate
reaches an authored `grading.yaml`; a `GradingConfig` built in process and a config
**recorded** before the rule existed and re-folded offline by `reconcile` reach no gate, so
both folds guard the same rule themselves: a component a substrate scored whose share
`combine.weights` does not declare raises on both substrates, naming the component and both
one-line fixes. Neither may pick a value — `1.0` invents a share the author never gave the
component and `0.0` discards a verdict the substrate produced.

**A fold with no weighted scored component decides rather than aggregating**, because min,
max and a mean over an empty map have no answer:

- **Nothing configured and nothing weighted** is `(1.0, True)`. Nothing was asked for, so
  nothing is owed — the shape a deliberately non-scoring pack declares.
- **Anything else** is `(0.0, False)` with a reason naming what the config asked for: the
  components that produced no verdict, the scored components whose shares sum to zero, or
  the weight keys naming nothing the config configured. A fail here never names nothing:
  a verdict reached without a component's reasons to explain it is one the author cannot
  act on, and a `0.0` beside components that all read as passing contradicts itself.

The zero-total-weight half is **`weighted`-only**. Under `all` and `any` the shares are
structurally unread — the shared dispatch aggregates the component set — so a share of
`0.0` there is an inert key rather than a statement about the fold, and a component scored
`0.0` at weight `0.0` still fails. Measured: at `weights: {state_checks: 0.0}`, `weighted`
gives `(0.0, False)` on both a satisfying and a violating trial while `all` and `any` give
`(1.0, True)` and `(0.0, False)` respectively — the component's own verdict, unchanged.

`combine.method` and `combine.weights` are `BOTH_SIGNAL_PARITY` for a reason that is
architectural rather than a defect, and the two rows in
[`key_manifest.py`](../tolokaforge/core/grading/key_manifest.py) carry it as their
`reason`: core produces no `llm_judge` component and cannot produce a
`state_checks.db_probes` one — both `RUNNER_ONLY` by design — so on a judge- or
probe-graded pack core's map is empty where the runner's is scored. Since `all` and `any`
aggregate that map alone, the disagreement is a verdict flip rather than a magnitude. The
canonical differential therefore proves the dispatch over deterministic components, which
is the whole of what is provable for these keys.

`combine_method` is one of the keys that lock an engine to a runner image presenting
it: see [§ Runner-engine version lock](#runner-engine-version-lock).

The `weighted` mean:

```
final_score = (state_score       * W_state
             + transcript_score  * W_transcript
             + trace_score       * W_trace
             + judge_score       * W_judge
             + custom_score      * W_custom)
              / (W_state + W_transcript + W_trace + W_judge + W_custom)

binary_pass = (final_score >= pass_threshold) AND (no required rubric criterion gated)
```

A component that was not evaluated is **excluded** from both the numerator and
the denominator — this includes an `llm_judge` component whose judge ERRORED
(see [LLM Judge](#llm-judge-rubric-grading)): a broken judge is never folded in
as a `0.0`. An *evaluated* component that `combine.weights` declares no weight for is
neither excluded nor defaulted: the fold raises on both substrates, per the rule above.

**Where the runner-side verdict is composed.** The runner folds a trial through
`compose_runner_trial_verdict`
([`tolokaforge/runner/grading.py`](../tolokaforge/runner/grading.py)), which wraps
`combine_grade_components` and applies **both gates** around it: the judge
component is zeroed where a required criterion failed, and a failed judge or
trace gate then forces `binary_pass` false whatever the threshold. It returns the
gated judge component beside `(score, binary_pass)`, because that component — not
the judge's raw aggregate — is what the wire grade and the reasons carry. One
runner-side home, so an offline recomputation reaches the runner's verdict without
repeating either gate: `tolokaforge reconcile`'s counterfactual
([`docs/RUBRIC_MIGRATION.md`](RUBRIC_MIGRATION.md)) is that caller, and it is what
[#775](https://github.com/toloka/tolokaforge/issues/775) would call.

**Core composes its own**, in
[`tolokaforge/core/grading/combine.py`](../tolokaforge/core/grading/combine.py),
with its own trace-gate forcing, and produces no `llm_judge` component at all —
so there is no judge gate for it to apply and nothing shared to extract. Two
substrates, two compositions, one behaviour where both can be asked; the
canonical differential above is what holds them to it.

**Configured but unevaluated fails loud, for every component.** A component is
configured when the pack writes its `grading.yaml` section — and, where that section
carries its own enable flag, when the flag is on, so `custom_checks: {enabled: false}`
is an explicit opt-out asking for nothing. If every configured component then comes
back unevaluated, the trial scores `(0.0, False)` with a reason naming them, rather
than a silent `(1.0, True)` — so a pack weighted entirely on one component that never
ran fails instead of passing on nothing. One predicate answers this question for the
authoring gate and for both folds, so the three cannot disagree about what the author
asked for.

### What a component is

`GRADE_COMPONENTS` in
[`tolokaforge/core/grading/grade_components.py`](../tolokaforge/core/grading/grade_components.py)
is the single enumeration of the grading components, and every site that has to
name them all reads it: the weighted fold on both substrates, the
configured-but-unevaluated check, the wire message, and the lowering of a wire
grade back into scores. Each entry declares four names for one component — the
`combine.weights` key (which is also the proto field and the wire dict key), the
`grading.yaml` section that configures it, the core `GradeComponents` attribute,
and the runner's `*_score` attribute. `state_checks` declares no runner
attribute: the runner has no single field for it, because hash, JSONPath and DB
probes are folded into that slot first (see
[Substrate Grading](#substrate-grading-state_checksdb_probes)).

The five components are `state_checks`, `transcript_rules`, `trace_checks`,
`llm_judge` and `custom_checks`. Adding a sixth means adding an entry; the
canonical suite fails a registry that disagrees with the core model, the wire
descriptor, the runner's fields or the config sections.

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

- **Use `state_checks` (weight 1.0) for deterministic tasks.** State checks are objective and reproducible. They verify that the agent actually changed the environment correctly. **Not on a task whose correct outcome is to change nothing** — a refusal-style task's expected final state equals its initial state, so an agent that did nothing at all scores `1.0` on state alone. Weight `transcript_rules` alongside it and declare a `min_assistant_turns` floor, which fails a trial that produced no assistant turns (see [§ Turn bounds](#turn-bounds)). A block with no evaluable source — only `id_fields`, or an empty `jsonpaths` list — asserts nothing and is refused before the run.
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
