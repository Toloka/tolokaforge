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
    model_ref: openrouter/anthropic/claude-sonnet-4.5   # required; a separate fixed judge model
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

* **A separate, fixed judge model.** `model_ref` is required and independent of
  the agent under test — this prevents self-grading bias and keeps the judge
  constant across agent comparisons. The judge builds its own LLM client via the
  agent's provider-correct capability path (so tool schemas/calls are correct
  for any provider).
* **Author-written reference channel.** The judge sees only the rubric's
  `reference` and per-criterion `expected` — author-written *for grading*. The
  deterministic oracle (`golden_actions`, `expected_hash`, `jsonpath_checks`) is
  **never** piped to the judge: that would cause path-matching bias and defeat
  path-independence. The judge's input surface is exactly
  `{agent_system_prompt, transcript, rubric, read-only tools}`.
* **Harness-owned read-only tools.** The judge gets a fixed read-only allowlist —
  DB reads (`get_db_state` / `query_db`), `search_kb` (when the task is
  RAG-backed), `read_file` (only when the agent produced a workspace), and the
  rubric-derived `submit_report`. No `write`, no `compute`, no reuse of the
  agent's tools.
* **Single call, per-criterion output.** The judge inspects the final state, then
  calls `submit_report` once with `{met, score, justification}` for every
  criterion (its arg schema is generated from the rubric and validated with
  Pydantic).

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

---

## Grading for RL Training

Tasks used for RL training need grading that produces a meaningful signal — not always 1.0 or always 0.0.

### Principles

- **Use `state_checks` (weight 1.0) for deterministic tasks.** State checks are objective and reproducible. They verify that the agent actually changed the environment correctly.
- **Reserve `llm_judge` for genuinely subjective tasks.** An LLM judge giving 0.7 for "attempted the task" masks real failures. Don't use it as padding.
- **CI portability:** public examples may use `mock/mock-judge` so CI can run without live judge inference; for real evaluations replace it with your production judge model.
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
