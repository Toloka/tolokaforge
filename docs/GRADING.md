# Grading System

Tolokaforge evaluates agent performance across three dimensions:

1. **State Checks** - Final environment state verification (hash-based or JSONPath)
2. **Transcript Rules** - Process constraints (required phrases, tool usage, turn limits)
3. **LLM Judge** - Per-criterion rubric grading by a read-only agentic judge

Scores are weighted and combined into a final score. See [REFERENCE.md](REFERENCE.md) for `grading.yaml` schema.

---

## Hash-Based Grading (Tau-Bench Compatible)

Hash grading compares SHA256 of final state against a pre-computed golden hash.

### Algorithm

```python
import hashlib
from typing import Any, Dict, List, Set, Tuple, Union

ToHashable = Union[str, int, float, Dict[str, "ToHashable"], List["ToHashable"], Set["ToHashable"]]
Hashable = Union[str, int, float, Tuple["Hashable"], Tuple[Tuple[str, "Hashable"]]]

def to_hashable(item: ToHashable) -> Hashable:
    """Convert to hashable representation (tau-bench compatible)"""
    if isinstance(item, dict):
        return tuple((key, to_hashable(value)) for key, value in sorted(item.items()))
    elif isinstance(item, list):
        return tuple(to_hashable(element) for element in item)
    elif isinstance(item, set):
        return tuple(sorted(to_hashable(element) for element in item))
    else:
        return item

def consistent_hash(value: Hashable) -> str:
    """Compute SHA256 hash"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

# Usage:
# golden_hash = consistent_hash(to_hashable(final_state))
```

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

### Best Practices

- Filter non-deterministic fields (timestamps, UUIDs) before hashing
- Document how golden hash was computed
- Combine with JSONPath assertions using `weight: 0.8` for flexibility

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
  `{agent_system_prompt, transcript, rubric, read-only tools}`.
* **Harness-owned read-only tools.** The judge gets a fixed read-only allowlist —
  DB reads (`get_db_state` / `query_db`), a KB search mirroring the agent's
  (`search_kb` for rag-service or the reused `search_policy` for TypeSense — see
  *Judge KB faithfulness* below), `read_file` (only when the agent produced a
  workspace), and the rubric-derived `submit_report`. No `write`, no `compute`.
* **Single call, per-criterion output.** The judge inspects the final state, then
  calls `submit_report` once with `{met, score, justification}` for every
  criterion (its arg schema is generated from the rubric and validated with
  Pydantic).

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

**Seeing which backend was used.** The judge's `reasons` (surfaced into the grade
output's `reasons`) always ends with a `Judge KB: …` note — `Judge KB: search_kb`,
`Judge KB: search_policy`, or `Judge KB: none offered`. The `JudgeResult` also
carries the structured `kb_tools_offered` tuple. This is the visible "graded
with / without KB" signal. "none offered" is **observability, not an error** — we
cannot statically know whether a given rubric needs a KB, so a KB-less judge
still `COMPLETED`; the note simply makes the gap auditable. The judge's own
`judge_trajectory.yaml` records which KB tools it actually *called*.

**Honest limitation.** The `search_policy` reuse path is validated only against a
fake reconstructed tool in unit tests; real TypeSense retrieval is exercised only
in a deployed mcp_core environment (mcp_core is not importable in this repo).
Likewise the mcp_core TypeSense client handle registered at trial setup is not
torn down at cleanup — a documented, bounded pre-existing leak (no confirmable
deregister API in mcp_core's registry); see the runner's `cleanup_trial`.

### Fail-loud: the ERRORED status

If the judge malfunctions — repeated malformed `submit_report` past its retry
budget, turn / wall-time exhaustion, or a crash — it produces **no score** and
marks the grade `judge_status: errored`. It **never** falls back to `0.0` or
`0.5` (AGENTS.md rule 1). An errored `llm_judge` component is left *unscored* and
**excluded from the weighted combine** — it is not read as a zero. Reviewers see
`judge_status: errored` in `grade.yaml`; downstream analytics must branch on it.

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
