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
1. Adapter `grade()` method (e.g., `FrozenMcpCoreAdapter` or `NativeAdapter`)
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

**Example hash-based grading logic:**
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

### LLM Judge

**Status: Implemented but not used in Docker architecture**

**Implementation exists at:**
- [`tolokaforge/core/grading/judge.py`](../tolokaforge/core/grading/judge.py) - `LLMJudge` class
- [`tolokaforge/core/grading/combine.py`](../tolokaforge/core/grading/combine.py:222-232) - Integration

**Current behavior:**
- `llm_judge` field in `GradeComponents` is always `null`
- Runner service sets `llm_judge=-1.0` (not computed)
- The `LLMJudge` class is functional but not invoked in Docker runtime

**From [`tolokaforge/runner/service.py`](../tolokaforge/runner/service.py:814-815):**
```python
llm_judge=-1.0,  # Not computed by Runner
custom_checks=-1.0,  # Not implemented yet
```

**To enable LLM judge:**
1. Configure in `grading.yaml`:
   ```yaml
   grading:
     combine:
       weights: { state_checks: 0.7, llm_judge: 0.3 }
     llm_judge:
       model_ref: "anthropic/claude-3-sonnet"
       rubric: "Evaluate the agent's helpfulness..."
       output_schema:
         type: object
         properties:
           score: { type: number }
           reasons: { type: string }
   ```
2. Modify Runner service to invoke `LLMJudge.grade()` during `GradeTrial` RPC

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

## D) Recommendations for Implementing LLM Fallback

If LLM judge is needed for subjective evaluation:

### 1. Add LLM Judge to Runner Service

Modify [`tolokaforge/runner/service.py`](../tolokaforge/runner/service.py) `GradeTrial` RPC:

```python
# After hash-based grading
if grading_config.llm_judge:
    from tolokaforge.core.grading.judge import LLMJudge
    
    judge = LLMJudge(grading_config.llm_judge.model_config)
    judge_score, judge_reasons = judge.grade(
        messages=llm_messages,
        rubric=grading_config.llm_judge.rubric,
        output_schema=grading_config.llm_judge.output_schema,
        task_description=task_description.description,
    )
    components.llm_judge = judge_score
```

### 2. Update GradingConfig Schema

Ensure [`tolokaforge/runner/models.py`](../tolokaforge/runner/models.py) `LLMJudgeConfig` includes:
- `model_ref` - Model identifier (e.g., "anthropic/claude-3-sonnet")
- `rubric` - Grading rubric text
- `output_schema` - JSON schema for judge response
- `temperature` - Sampling temperature (default: 0.0)

### 3. Handle Judge Failures Gracefully

```python
try:
    judge_score, judge_reasons = judge.grade(...)
except Exception as e:
    logger.warning(f"LLM judge failed: {e}")
    judge_score = 0.5  # Neutral fallback
    judge_reasons = f"Judge failed: {e}"
```

### 4. Consider Cost/Latency

- LLM judge adds API call latency (~1-5s)
- Consider caching judge results for identical transcripts
- Use cheaper models for judge (e.g., claude-3-haiku) vs agent

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
