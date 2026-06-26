# Grading Verification Report

This document describes the grading system verification performed on 2026-03-09.

## Summary

The grading system has been verified to work correctly for:
- ✅ Binary reward (hash match → score=1.0, hash mismatch → score=0.0)
- ✅ Error detection (technical errors vs task failures)
- ✅ Results saved correctly to grade.yaml and trajectory.yaml
- ⚠️ LLM judge (placeholder - not implemented)
- ⚠️ Transcript rules (implemented but rarely used)
- ⚠️ Custom checks (implemented but rarely used)

---

## A) How Grading Works (Actual Code Path)

### 1. Stable Hash Computation

The grading system uses SHA-256 hash comparison to determine if the agent achieved the correct final state.

**Code path:**
1. [`tolokaforge/core/hash.py`](../tolokaforge/core/hash.py) - `compute_stable_hash()`
2. [`tolokaforge/core/grading/state_checks.py`](../tolokaforge/core/grading/state_checks.py) - `consistent_hash()`, `to_hashable()`

**Algorithm:**
```python
# From tolokaforge/core/hash.py
def compute_stable_hash(state: dict, unstable_fields: list[str] | None = None) -> str:
    # 1. Filter out unstable fields (timestamps, auto-generated IDs)
    if unstable_fields:
        state = filter_unstable_fields(state, unstable_fields)
    
    # 2. Convert datetime objects to ISO format strings
    serializable_state = _convert_datetime_to_str(state)
    
    # 3. Serialize to JSON with canonical format
    json_str = json.dumps(serializable_state, sort_keys=True, separators=(",", ":"), default=str)
    
    # 4. Compute SHA-256 hexdigest
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()
```

**Tau-bench compatible algorithm** (used by StateChecker):
```python
# From tolokaforge/core/grading/state_checks.py
def to_hashable(item):
    """Convert to hashable representation (tau-bench compatible)"""
    if isinstance(item, dict):
        return tuple((key, to_hashable(value)) for key, value in sorted(item.items()))
    elif isinstance(item, list):
        return tuple(to_hashable(element) for element in item)
    elif isinstance(item, set):
        return tuple(sorted(to_hashable(element) for element in item))
    else:
        return item

def consistent_hash(value) -> str:
    """Compute SHA256 hash"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
```

### 2. Golden Set Comparison

**Code path:**
1. [`external_adapters/tolokaforge-adapter-tlk-mcp-core/src/tolokaforge_adapter_tlk_mcp_core/adapter.py`](../external_adapters/tolokaforge-adapter-tlk-mcp-core/src/tolokaforge_adapter_tlk_mcp_core/adapter.py) - `grade()` method
2. [`tolokaforge/core/orchestrator.py`](../tolokaforge/core/orchestrator.py) - `_run_trial()` method

**Flow:**
```
1. Load testcase with golden_path (expected tool interactions)
2. Execute golden_path on fresh database → expected_state
3. Compute expected_hash = compute_stable_hash(expected_state)
4. Agent executes trial → actual_state
5. Compute actual_hash = compute_stable_hash(actual_state)
6. Compare: actual_hash == expected_hash
   - Match: score=1.0, binary_pass=True
   - Mismatch: score=0.0, binary_pass=False, compute state_diff
```

### 3. Score Assignment

**From [`external_adapters/tolokaforge-adapter-tlk-mcp-core/src/tolokaforge_adapter_tlk_mcp_core/adapter.py`](../external_adapters/tolokaforge-adapter-tlk-mcp-core/src/tolokaforge_adapter_tlk_mcp_core/adapter.py:1115-1139):**
```python
def grade(self, task_id, trajectory, final_state, env):
    expected_stable = self._compute_expected_state(task_id)
    expected_hash = compute_stable_hash(expected_stable)
    
    actual_stable = get_stable_state(db)
    actual_hash = compute_stable_hash(actual_stable)
    
    if actual_hash == expected_hash:
        return Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(state_checks=1.0),
            reasons=f"State: stable hash matches ({expected_hash[:16]}...)",
        )
    else:
        state_diff = calculate_state_diff(expected_stable, actual_stable)
        return Grade(
            binary_pass=False,
            score=0.0,
            components=GradeComponents(state_checks=0.0),
            reasons=f"State: stable hash mismatch (expected {expected_hash[:16]}..., got {actual_hash[:16]}...)",
            state_diff=state_diff,
        )
```

---

## B) What Works

### Binary Reward (Hash Match/Mismatch)

**Verified from output files:**

**REWARD=1 (Pass):**
```yaml
# output/consulting_v1_docker_20260308_062221/trials/SWA-001/0/grade.yaml
binary_pass: true
score: 1.0
components:
  state_checks: 1.0
  transcript_rules: null
  llm_judge: null
  custom_checks: null
reasons: 'State: stable hash matches (ac5bb22c824492a6...)'
state_diff: null
```

**REWARD=0 (Fail):**
```yaml
# output/sandbox_example_docker_20260308_065713/trials/DC-F-001/0/grade.yaml
binary_pass: false
score: 0.0
components:
  state_checks: 0.0
  transcript_rules: null
  llm_judge: null
  custom_checks: null
reasons: 'State: stable hash mismatch (expected 2bccddd768afdcca..., got 7725d81e6eee63f9...)'
state_diff:
  diff: |
    --- expected_state+++ actual_state@@ -23595,15 +23595,6 @@   ],
       "fsl_service_appointments": [
         {
    -      "id": "APPT-00000000",
    ...
  diff_lines: 36
  has_diff: true
```

### Error Detection

**Technical errors are distinguished from task failures:**

**From [`tolokaforge/core/orchestrator.py`](../tolokaforge/core/orchestrator.py:1450-1464):**
```python
# Check if trial completed successfully - ERROR/TIMEOUT trials should auto-fail
if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
    grade = Grade(
        binary_pass=False,
        score=0.0,
        components=GradeComponents(state_checks=0.0),
        reasons=f"Trial failed with status: {trajectory.status.value}",
    )
```

**Verified from output files:**
```yaml
# output/consulting_v1_docker_20260308_062014/trials/SWA-001/0/grade.yaml
binary_pass: false
score: 0.0
reasons: 'Trial failed with status: error'

# output/consulting_v1_docker_20260308_062014/trials/SWA-001/0/trajectory.yaml
status: error
termination_reason: error
messages:
- role: system
  content: 'Trial initialization error: LLM API call failed: litellm.APIError: APIError:
    OpenrouterException - {"error":{"message":"Key limit exceeded..."}'
```

### Retry Logic

**From [`tolokaforge/core/orchestrator.py`](../tolokaforge/core/orchestrator.py:114-126):**
```python
@staticmethod
def _is_retryable_trajectory(trajectory: Trajectory) -> bool:
    """Classify retryable infrastructure failures."""
    if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
        return True
    if trajectory.termination_reason in (
        TerminationReason.RATE_LIMIT,
        TerminationReason.API_ERROR,
        TerminationReason.TIMEOUT,
        TerminationReason.ERROR,
    ):
        return True
    return False
```

**Retry behavior:**
- Retryable failures are re-queued via `run_queue.mark_failed(lease.id, reason, retryable=True)`
- Max retries configured via `config.orchestrator.max_attempt_retries`
- Completed trials (even with score=0) are NOT retried

### Results Saved Correctly

Output files are written by [`tolokaforge/core/output_writer.py`](../tolokaforge/core/output_writer.py):
- `grade.yaml` - Grading results
- `trajectory.yaml` - Full conversation and tool calls
- `metrics.yaml` - Performance metrics
- `env.yaml` - Final environment state
- `task.yaml` - Task configuration

---

## C) What's Missing / Placeholder

### Custom Checks placeholder

```python
custom_checks=-1.0,  # Not implemented yet
```

### Transcript Rules

**Status: Implemented and functional**

**Implementation at:**
- [`tolokaforge/runner/grading.py`](../tolokaforge/runner/grading.py:182-224) - `evaluate_transcript_rules()`

**Supported rule types:**
- `must_contain` - Check if assistant message contains text
- `must_not_contain` - Check no assistant message contains text
- `required_tool_call` - Check tool was called with arguments
- `max_turns` - Verify conversation under turn limit

**Current behavior:**
- `transcript_rules` field in `GradeComponents` is usually `null`
- Rules are evaluated if configured in `grading.yaml`

### Custom Checks

**Status: Implemented but not used in Docker architecture**

**Implementation at:**
- [`tolokaforge/core/grading/check_runner.py`](../tolokaforge/core/grading/check_runner.py) - `CheckRunner`, `run_custom_checks()`
- [`tolokaforge/core/grading/checks_interface.py`](../tolokaforge/core/grading/checks_interface.py) - Interface definitions

**Current behavior:**
- `custom_checks` field in `GradeComponents` is always `null`
- Runner service sets `custom_checks=-1.0` (not implemented)

---

## D) LLM Judge (rubric grading)

**Status: live, computed by the Runner.** When `grading.llm_judge` is
configured, `GradeTrial`
([`tolokaforge/runner/service.py`](../tolokaforge/runner/service.py)) runs a
read-only agentic rubric judge on the shared tool-calling loop and fills
`GradeComponents.llm_judge` plus per-criterion `criterion_results`, a
`judge_status`, and a `JudgeReport` (the judge's own usage + transcript). See
[GRADING.md](GRADING.md#llm-judge-rubric-grading) for the full mechanism and
[OUTPUT_FORMAT.md](OUTPUT_FORMAT.md) for the on-disk shape.

Key properties (do NOT re-introduce the old behaviour):

- **Structured rubric.** `grading.llm_judge.rubric` is a structured `Rubric`
  (per-criterion `kind` / `weight` / `required` + an author-written `reference`),
  not free text. There is no `output_schema` — the judge's structured-output
  schema is derived from the rubric's criteria.
- **Fail loud, never a neutral fallback.** If the judge malfunctions (retry /
  wall-time exhaustion or a crash) it emits `judge_status: errored` with **no
  score** — the `llm_judge` component is left unscored and excluded from the
  weighted combine. It **never** falls back to `0.0` or `0.5` (AGENTS.md rule 1).
- **Required criteria are pure gates.** A failed `required` criterion fails the
  rubric outright; required criteria are excluded from the weighted average.

---

## Test Coverage

Tests added in [`tests/integration/test_grading_correctness.py`](../tests/integration/test_grading_correctness.py):

| Test Class | Tests | Status |
|------------|-------|--------|
| `TestGoldenMatchScoresOne` | 3 | ✅ Pass |
| `TestGoldenMismatchScoresZero` | 4 | ✅ Pass |
| `TestErrorTrialDetected` | 4 | ✅ Pass |
| `TestLLMJudgePlaceholderStatus` | 3 | ✅ Pass |
| `TestTranscriptRulesEvaluation` | 6 | ✅ Pass |
| `TestStableHashComputation` | 4 | ✅ Pass |

**Total: 24 tests passing**

Run tests:
```bash
uv run pytest tests/integration/test_grading_correctness.py -v
```

---

## References

- [GRADING.md](GRADING.md) - Grading system overview
- [REFERENCE.md](REFERENCE.md) - Configuration schemas
- [custom_checks.md](custom_checks.md) - Custom Python validation
