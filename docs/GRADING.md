# Grading System

Tolokaforge evaluates agent performance across three dimensions:

1. **State Checks** - Final environment state verification (hash-based or JSONPath)
2. **Transcript Rules** - Process constraints (required phrases, tool usage, turn limits)
3. **LLM Judge** - Per-criterion rubric grading by a read-only agentic judge

Scores are weighted and combined into a final score. See [REFERENCE.md](REFERENCE.md) for `grading.yaml` schema.

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
would treat `"0042"`-style values as numbers. This key is honored identically on
both grading substrates (the core `GradingEngine`/`to_hashable` path and the
runner gRPC/`compute_stable_hash` path).

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
that bypass `NativeAdapter.to_task_description`.

**Tables materialized only by `initialization_actions`**: the cross-check reads
`initial_state.tables` (typically populated from `initial_state.json_db`). A
table that first appears only via an `initialization_action` won't be visible to
the check — an `id_fields` entry for such a table needs `relaxed_validation:
true` today. Add the table to `initial_state.json_db` (even with an empty list)
if you want the strict check to accept it.

**Runner-engine version lock**: `id_fields` and `relaxed_validation` are declared
on the runner-side `StateChecksConfig` (`extra="forbid"`), so a new engine emitting
these keys requires a runner image built from the same release. Old engine + new
runner is safe (core-side `extra="ignore"`).

### Best Practices

- Filter non-deterministic fields (timestamps, UUIDs) before hashing
- Prefer golden-action replay over storing a hash literal; if you must store one,
  recompute it whenever the hashing algorithm changes (see the callout above)
- Fold numeric strings per-field (`numeric_string_fields`), never as a global switch
- Declare non-`id` primary keys per table (`id_fields`); leave `id`-keyed tables unset
- Use `relaxed_validation` only as a short-lived escape hatch for legacy tasks
- Combine with JSONPath assertions using `weight: 0.8` for flexibility

### Multiple legal final states (alternative golden paths)

Some domains admit more than one policy-correct final shape — e.g. a policy that
allows the agent to either create one combined case or split it into two.
`state_checks.hash.alternative_golden_actions` lets a task ship additional golden
paths so any of them can satisfy the state check.

```yaml
state_checks:
  hash:
    enabled: true
    weight: 1.0
    golden_actions:                       # variant 0 (primary)
      - { name: create_case, kwargs: {...} }
    alternative_golden_actions:           # optional; each entry is variant 1..N
      - - { name: create_case_a, kwargs: {...} }
        - { name: create_case_b, kwargs: {...} }
```

Grading replays each variant on a fresh initial state, hashes the resulting
state, and passes if the trial's final hash matches ANY variant. On mismatch,
the reported diff is against the variant with the smallest row-level distance
from the trial state — triage stays focused on the variant the trial came
closest to satisfying regardless of variant count.

- **Ordering is authoring order.** `golden_actions` is variant 0; alternatives
  are variant 1..N in the order listed. The grading reasons string carries the
  matched variant index for analytics (`matched golden variant 1 of 3`).
- **Broken variants fail loud.** A variant whose replay raises contributes its
  error to the grade reasons on every outcome — a broken shipped golden is
  never masked by a matching alternate. Grading returns 0.0 only if every
  variant fails to replay.
- **Absent alternatives = single-variant behaviour.** Tasks that ship no
  `alternative_golden_actions` execute the pre-existing single-variant hash
  code path unchanged.
- **Bundle layout.** Adapters emitting native-format bundles write each
  variant to `fixtures/golden_actions_variant_{n}.json` starting at `n=1`,
  alongside the primary `fixtures/golden_actions.json`.
- **Not supported with `env_assertions` / `db_hash_check`.** The tau2
  environment evaluator compares against a single hash; mixing it with
  `alternative_golden_actions` raises a fail-loud config error at grading
  time. Drop one or the other.

Design bias: use this sparingly. Every extra variant that survives review is a
statement that the domain policy is genuinely permissive; if a task can be
tightened by amending policy text to mandate one shape, prefer that over
shipping alternates. Grading cost scales linearly with variant count — each
variant runs a full reset + replay + snapshot + hash cycle — so a task with N
alternates roughly triples-and-adds a grade's runtime vs. a single-golden
task on the pessimistic (no-match) path. Match short-circuits at the first
matching variant.

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

## pass@k Metrics

Estimates probability that at least 1 of k attempts succeeds.

### Formula

Given `n` trials with `c` successes:

```
pass@k = 1 - C(n - c, k) / C(n, k)
```

Where `C(a, b)` is binomial coefficient "a choose b".

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
final_score = (state_score * W_state + transcript_score * W_transcript + judge_score * W_judge)
              / (W_state + W_transcript + W_judge)

binary_pass = (final_score >= pass_threshold) AND (no required rubric criterion gated)
```

A component that was not evaluated is **excluded** from both the numerator and
the denominator — this includes an `llm_judge` component whose judge ERRORED
(see [LLM Judge](#llm-judge-rubric-grading)): a broken judge is never folded in
as a `0.0`.

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
- [CUSTOM_CHECKS.md](CUSTOM_CHECKS.md) - Custom Python validation
- [TASKS.md](TASKS.md) - Task authoring guide with difficulty design patterns
