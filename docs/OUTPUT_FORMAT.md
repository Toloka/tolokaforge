# Output Format

TolokaForge writes trial results to 6 focused YAML files per trial, organized for easy analysis and debugging.

## Directory Structure

```
output/
└── trials/
    └── {task_id}/
        └── {trial_index}/
            ├── task.yaml       # Task metadata & grading config
            ├── trajectory.yaml # Conversation messages
            ├── env.yaml        # Final environment state
            ├── metrics.yaml    # Performance metrics
            ├── grade.yaml      # Grading results with diff
            └── logs.yaml       # Structured trial logs
```

## File Specifications

### task.yaml
Task metadata and configuration.

```yaml
task_id: "051fa6cb-a29e-4a0d-9ccf-e0f95802eee5"
trial_index: 0
category: "food_delivery"
description: "Task description text"
grading_config:
  state_checks: {...}
  transcript_rules: {...}
  combine: {...}
tools:
  agent: {enabled: [...]}
  user: {enabled: [...]}
policies: {...}
```

### trajectory.yaml
Conversation history with status.

```yaml
task_id: "051fa6cb-a29e-4a0d-9ccf-e0f95802eee5"
trial_index: 0
start_ts: "2025-11-17T20:05:49.934649"
end_ts: "2025-11-17T20:08:44.074081"
status: "completed"  # completed | failed | timeout | error
messages:
  - role: "user"
    content: "..."
    ts: "2025-11-17T20:05:50.000000"
  - role: "assistant"
    content: "..."
    tool_calls: [...]
    ts: "2025-11-17T20:05:51.000000"
```

### env.yaml
Final environment state after trial execution.

```yaml
agent: {...}       # Agent-side database state
user:
  device: {...}    # User device state
db: {...}          # Full database state
filesystem: {...}  # File system state
mock_web_url: "..."
```

### metrics.yaml
Performance metrics and tool usage statistics.

```yaml
latency_total_s: 174.14
turns: 14
api_calls: 14
tokens_input: 118022
tokens_output: 3011
cost_usd_est: 0.127055
tool_calls: 7
tool_success_rate: 1.0
stuck_detected: false
tool_usage:
  - tool: "get_user_details"
    count: 2
    success: 2
    fail: 0
  - tool: "create_order"
    count: 2
    success: 2
    fail: 0
```

### grade.yaml
Grading results with detailed state diffs on failures.

```yaml
binary_pass: false
score: 0.0
components:
  state_checks: 0.0
  transcript_rules: null
  llm_judge: null
reasons: "State: State hash mismatch. Diff: ..."
state_diff:  # Present when state check fails
  diff: |
    --- expected_state
    +++ actual_state
    @@ -1,10 +1,10 @@
     {
       "orders": {
    -    "order_1": {
    +    "order_2": {
           "status": "completed"
         }
       }
     }
  diff_lines: 200
  has_diff: true
```

### logs.yaml
Structured logs from trial execution.

```yaml
trial_id: "051fa6cb-a29e-4a0d-9ccf-e0f95802eee5:0"
total_logs: 45
logs:
  - timestamp: "2025-11-17T20:05:49.934649"
    level: "INFO"
    module: "runner"
    message: "Starting trial execution"
    context:
      task_id: "..."
      trial_index: 0
  - timestamp: "2025-11-17T20:05:51.234567"
    level: "ERROR"
    module: "grading"
    message: "State hash mismatch in golden set grading"
    context:
      expected: "1d57efd98..."
      actual: "7d10f0521..."
```

## Reading Output Files

### Python Example

```python
from pathlib import Path
import yaml

def load_trial(trial_dir: Path) -> dict:
    """Load all trial data"""
    data = {}
    
    with open(trial_dir / "task.yaml") as f:
        data["task"] = yaml.safe_load(f)
    
    with open(trial_dir / "trajectory.yaml") as f:
        data["trajectory"] = yaml.safe_load(f)
    
    with open(trial_dir / "env.yaml") as f:
        data["env_state"] = yaml.safe_load(f)
    
    with open(trial_dir / "metrics.yaml") as f:
        data["metrics"] = yaml.safe_load(f)
    
    # Optional files
    if (trial_dir / "grade.yaml").exists():
        with open(trial_dir / "grade.yaml") as f:
            data["grade"] = yaml.safe_load(f)
    
    if (trial_dir / "logs.yaml").exists():
        with open(trial_dir / "logs.yaml") as f:
            data["logs"] = yaml.safe_load(f)
    
    return data
```

### Analysis Example

```python
def analyze_failures(output_dir: Path):
    """Find and analyze failed trials"""
    for grade_file in output_dir.glob("trials/*/0/grade.yaml"):
        with open(grade_file) as f:
            grade = yaml.safe_load(f)
        
        if not grade["binary_pass"]:
            task_id = grade_file.parent.parent.name
            print(f"Failed: {task_id} (score={grade['score']:.2f})")
            
            # Show state diff if available
            if grade.get("state_diff") and grade["state_diff"]["has_diff"]:
                print(f"State diff ({grade['state_diff']['diff_lines']} lines):")
                print(grade["state_diff"]["diff"])
```

## CLI Flags

### Verbose Mode
Enable DEBUG level logging:

```bash
tolokaforge run --config config.yaml --verbose
```

Output shows detailed execution information:
```
2025-11-24 20:30:15 - orchestrator - DEBUG - Registered agent tools (count=15)
2025-11-24 20:30:18 - task-123:0 - DEBUG - Agent response received (tokens=1234)
```

### Strict Mode
Raise errors immediately on ERROR level logs:

```bash
tolokaforge run --config config.yaml --strict
```

First ERROR log raises RuntimeError and stops execution.

### Combined
Use both flags together:

```bash
tolokaforge run --config config.yaml --verbose --strict
```

## Benefits

1. **Better Organization**: 6 focused files instead of 1 monolithic file
2. **Faster Loading**: Load only the files you need
3. **Better Debugging**: Structured logs with context
4. **Richer Information**: Task metadata, detailed diffs, tool usage breakdown
5. **Better Format**: YAML is more human-readable than JSON
6. **Smaller Size**: ~45% reduction (no duplicate metadata)

## File Sizes

Typical trial output:
```
task.yaml:       ~5 KB
trajectory.yaml: ~50 KB
env.yaml:        ~1.2 MB
metrics.yaml:    ~3 KB
grade.yaml:      ~80 KB
logs.yaml:       ~20 KB
Total:           ~1.36 MB (vs ~2.5 MB in old format)
```

## Common Use Cases

### Analyzing Grading Failures

```bash
# View grade with diff
cat output/trials/task-123/0/grade.yaml

# Extract just the diff
cat output/trials/task-123/0/grade.yaml | yq '.state_diff.diff'
```

### Debugging Tool Failures

```bash
# View error logs
cat output/trials/task-123/0/logs.yaml | yq '.logs[] | select(.level == "ERROR")'

# View tool usage stats
cat output/trials/task-123/0/metrics.yaml | yq '.tool_usage[]'
```

### Performance Analysis

```bash
# View all metrics
cat output/trials/task-123/0/metrics.yaml

# Extract cost
cat output/trials/task-123/0/metrics.yaml | yq '.cost_usd_est'
```

## See Also

- [LOGGING.md](LOGGING.md) - Structured logging system
- [tests/README.md](../tests/README.md) - Test suite documentation
- [REFERENCE.md](REFERENCE.md) - Technical reference