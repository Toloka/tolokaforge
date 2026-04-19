# Grading System

Tolokaforge evaluates agent performance across three dimensions:

1. **State Checks** - Final environment state verification (hash-based or JSONPath)
2. **Transcript Rules** - Process constraints (required phrases, tool usage, turn limits)
3. **LLM Judge** - Subjective quality assessment via rubric

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

binary_pass = (final_score >= pass_threshold)
```

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

## LLM Judge Evaluation in Runner

The `llm_judge` grading component is evaluated by the **Runner** container (not the orchestrator). This keeps all grading co-located with execution and eliminates round-trips.

### How It Works

1. Task `grading.yaml` declares an `llm_judge` section with `model_ref`, `rubric`, and optional `output_schema`.
2. The orchestrator derives which API keys the judge model needs from `model_ref` (e.g., `openrouter/anthropic/claude-sonnet-4-6` → `OPENROUTER_API_KEY`).
3. Those keys are passed to the Runner container via `ServiceDefinition.secret_keys` → container environment variables, using `SecretManager.to_env_dict()`.
4. The Runner calls [litellm](https://docs.litellm.ai/) directly with the configured model to evaluate the agent's transcript against the rubric.
5. The judge score is combined with `state_checks` and `transcript_rules` per the `combine.weights` configuration.

### Configuration

```yaml
llm_judge:
  model_ref: "openrouter/anthropic/claude-sonnet-4-6"
  rubric: |
    Evaluate whether the agent completed the customer's request correctly.
    Score 1.0 for full completion, 0.5 for partial, 0.0 for failure.
  output_schema:
    type: object
    properties:
      score:
        type: number
      reasoning:
        type: string
    required: [score, reasoning]

combine:
  weights: { state_checks: 0.6, transcript_rules: 0.2, llm_judge: 0.2 }
  pass_threshold: 0.75
```

### CI Portability

Public examples may use `mock/mock-judge` as `model_ref` so CI can run without live judge inference. For real evaluations, replace it with your production judge model.

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
