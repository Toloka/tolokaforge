# Logging Documentation

## Overview

TolokaForge uses structured logging throughout the codebase for better debugging and monitoring.

## Using the Logger

### Basic Usage

```python
from tolokaforge.core.logging import get_logger

logger = get_logger("my_module")

# Log messages
logger.info("Processing started")
logger.debug("Detailed debug info")
logger.warning("Something unexpected happened")
logger.error("Critical error occurred")
```

### Logging with Context

```python
logger.info("Task started", task_id="task-123", trial_index=0)
logger.error("Tool failed", tool="get_user", error="timeout")
logger.debug("State updated", changes=5, duration_s=0.5)
```

### Trial Logging

```python
from tolokaforge.core.logging import init_trial_logger

# Initialize logger for a specific trial
trial_id = f"{task_id}:{trial_index}"
logger = init_trial_logger(trial_id, verbose=False, strict=False)

logger.info("Trial execution started")
# ... trial code ...
logger.info("Trial execution completed")

# Save logs to file
from pathlib import Path
logger.save_to_file(Path("output/trials/task-123/0/logs.json"))
```

## Log Levels

### DEBUG
**When to use:** Detailed execution information, useful for debugging

**Examples:**
```python
logger.debug("Tool executed successfully", tool="create_order", duration_s=0.3)
logger.debug("MCP module loaded", has_get_tool_schema=True)
logger.debug("State synchronized", tables=5)
```

**Visibility:** Only shown with `--verbose` flag

### INFO
**When to use:** Normal operation milestones

**Examples:**
```python
logger.info("Trial execution started", task_id="task-123")
logger.info("Agent signaled completion")
logger.info("Trial graded", score=0.95, binary_pass=True)
```

**Visibility:** Always shown (default level)

### WARNING
**When to use:** Recoverable issues that don't stop execution

**Examples:**
```python
logger.warning("Tool execution failed", tool="get_user", error="not found")
logger.warning("Golden action failed", action="create_order", error="timeout")
logger.warning("No grading config found", path="/path/to/grading.yaml")
```

**Visibility:** Always shown

### ERROR
**When to use:** Critical failures

**Examples:**
```python
logger.error("State hash mismatch", expected="abc123", actual="def456")
logger.error("Trial initialization error", error="config missing")
logger.error("Failed to load MCP server", path="/path/to/mcp_server.py")
```

**Visibility:** Always shown
**Behavior in strict mode:** Raises `RuntimeError` immediately

## CLI Flags

### --verbose
Enables DEBUG level logging:

```bash
# Normal mode (INFO and above)
tolokaforge run --config config.yaml

# Verbose mode (DEBUG and above)
tolokaforge run --config config.yaml --verbose
```

**Use cases:**
- Debugging task execution
- Understanding tool behavior
- Analyzing performance
- Troubleshooting MCP integration

### --strict
Raises RuntimeError on ERROR logs:

```bash
# Normal mode (logs errors, continues)
tolokaforge run --config config.yaml

# Strict mode (raises on errors)
tolokaforge run --config config.yaml --strict
```

**Use cases:**
- CI/CD pipelines (fail fast)
- Development (catch errors immediately)
- Debugging specific failures
- Testing error handling

### Combined
```bash
tolokaforge run --config config.yaml --verbose --strict
```

**Use cases:**
- Maximum visibility + fail-fast behavior
- Debugging complex issues
- Validating golden sets

## Log Output Structure

### Console Output
```
2025-11-24 15:22:50 - trial-123:0 - INFO - Starting trial execution (task_id=task-123,  trial_index=0, max_turns=50)
2025-11-24 15:22:51 - trial-123:0 - DEBUG - Agent response received (turn=0, tokens_input=8450, tokens_output=215)
2025-11-24 15:22:52 - trial-123:0 - WARNING - Tool execution failed (tool=get_user, error=not found)
2025-11-24 15:22:53 - trial-123:0 - INFO - Trial execution finished (status=completed, turns=5, latency_s=3.2)
```

### JSON Output (logs.json)
```json
{
  "trial_id": "task-123:0",
  "total_logs": 45,
  "logs": [
    {
      "timestamp": "2025-11-24T15:22:50.185275",
      "level": "INFO",
      "module": "task-123:0",
      "message": "Starting trial execution",
      "context": {
        "task_id": "task-123",
        "trial_index": 0,
        "max_turns": 50
      }
    }
  ]
}
```

## Logger Registry

The logging module maintains a global registry of loggers to avoid duplicates:

```python
from tolokaforge.core.logging import get_logger

# Same name returns same instance
logger1 = get_logger("module_name")
logger2 = get_logger("module_name")
assert logger1 is logger2  # True

# Different configs create separate instances
logger_info = get_logger("module", level=logging.INFO, strict=False)
logger_debug = get_logger("module", level=logging.DEBUG, strict=True)
assert logger_info is not logger_debug  # True
```

### Clearing Registry (Testing)
```python
from tolokaforge.core.logging import clear_logger_registry

# Clear all cached loggers (useful in tests)
clear_logger_registry()
```

## Strict Mode Behavior

### Normal Mode (strict=False)
```python
logger = get_logger("module", strict=False)
logger.error("Something failed", error="timeout")
# Logs the error, continues execution
```

### Strict Mode (strict=True)
```python
logger = get_logger("module", strict=True)
logger.error("Something failed", error="timeout")
# Raises: RuntimeError: [STRICT MODE] Something failed (error=timeout)
```

### Trial-Level Strict Mode
```python
# In runner.py
try:
    logger.error("Tool execution failed")
    # In strict mode, this raises immediately
except RuntimeError as e:
    # Trial marked as TrialStatus.ERROR
    # Exception propagates up
    raise
```

## Best Practices

### 1. Use Appropriate Log Levels
```python
# ✅ Good
logger.debug("Processing item", item_id=item_id)  # Details
logger.info("Task completed")  # Milestones
logger.warning("Retrying failed request")  # Recoverable
logger.error("Database connection failed")  # Critical

# ❌ Bad
logger.info("Processing item 1")  # Too detailed for INFO
logger.error("User not found")  # Not critical (use WARNING)
```

### 2. Include Context
```python
# ✅ Good
logger.error("Tool failed", tool="get_user", error="timeout", retry=3)

# ❌ Bad
logger.error(f"Tool get_user failed with timeout on retry 3")
```

### 3. Log Before Raising
```python
# ✅ Good
if not config_file.exists():
    logger.error("Config file not found", path=str(config_file))
    raise FileNotFoundError(f"Config not found: {config_file}")

# ❌ Bad
if not config_file.exists():
    raise FileNotFoundError(f"Config not found: {config_file}")
```

### 4. Use Structured Data
```python
# ✅ Good - Structured context
logger.info("Trial completed", 
           task_id=task_id,
           trial_index=trial_idx,
           duration_s=duration,
           score=score)

# ❌ Bad - String formatting
logger.info(f"Trial {task_id}:{trial_idx} completed in {duration}s with score {score}")
```

## Querying Logs

### Find All Errors
```python
import json

with open("logs.json") as f:
    logs = json.load(f)

errors = [log for log in logs['logs'] if log['level'] == 'ERROR']
for error in errors:
    print(f"{error['timestamp']}: {error['message']}")
    print(f"  Context: {error['context']}")
```

### Filter by Time Range
```python
from datetime import datetime

with open("logs.json") as f:
    logs = json.load(f)

start_time = datetime.fromisoformat("2025-11-24T15:00:00")
end_time = datetime.fromisoformat("2025-11-24T16:00:00")

filtered = [log for log in logs['logs'] 
           if start_time <= datetime.fromisoformat(log['timestamp']) <= end_time]
```

### Find Specific Tool Calls
```python
with open("logs.json") as f:
    logs = json.load(f)

tool_logs = [log for log in logs['logs'] 
            if log['context'].get('tool') == 'create_order']
```

## Common Patterns

### Module Logger
```python
from tolokaforge.core.logging import get_logger

class MyModule:
    def __init__(self):
        self.logger = get_logger("my_module")
    
    def process(self, data):
        self.logger.info("Processing started", count=len(data))
        try:
            result = self._do_work(data)
            self.logger.info("Processing completed", result_count=len(result))
            return result
        except Exception as e:
            self.logger.error("Processing failed", error=str(e))
            raise
```

### Trial Logger
```python
from tolokaforge.core.logging import init_trial_logger

class TrialRunner:
    def run(self, verbose=False, strict=False):
        trial_id = f"{self.task_id}:{self.trial_index}"
        self.logger = init_trial_logger(trial_id, verbose, strict)
        
        self.logger.info("Starting trial", max_turns=self.max_turns)
        # ... execution ...
        self.logger.info("Trial finished", turns=self.turns)
```

### Exception Logging
```python
try:
    result = risky_operation()
except ValueError as e:
    logger.warning("Invalid value", error=str(e), value=value)
    # Continue with default
    result = default_value
except Exception as e:
    logger.error("Unexpected error", error=str(e), error_type=type(e).__name__)
    # In strict mode, this raises
    # Otherwise, handle or re-raise
    raise
```

## Thread Safety

The StructuredLogger is thread-safe:
- Each logger instance is independent
- Registry uses unique keys (name:level:strict)
- Logs collected in memory per thread
- Written to file at end of trial

## Performance

- **Memory:** ~100 log entries per trial (~10 KB)
- **CPU:** Minimal overhead (< 1% runtime)
- **I/O:** Single write at end of trial
- **Storage:** ~20 KB per logs.json file

## Troubleshooting

### Logs not appearing
**Check log level:**
```python
# DEBUG logs only visible in verbose mode
logger.debug("Detail")  # Not shown by default

# Use --verbose flag
tolokaforge run --config config.yaml --verbose
```

### Too many logs
**Reduce verbosity:**
```python
# Don't log in tight loops
for i in range(10000):
    logger.debug(f"Processing {i}")  # ❌ Too much

# Log summary instead
logger.debug("Processing items", count=10000)  # ✅ Better
```

### Strict mode too strict
**Use appropriate levels:**
```python
# ❌ Don't use ERROR for warnings
logger.error("Retrying request")  # Raises in strict mode

# ✅ Use WARNING for recoverable issues
logger.warning("Retrying request")  # Doesn't raise
```

## Integration with Existing Code

### Replace print() statements
```python
# Before
print(f"Starting task {task_id}")
print(f"DEBUG: Loaded {count} items")
print(f"WARNING: File not found: {path}")

# After
from tolokaforge.core.logging import get_logger
logger = get_logger("module_name")

logger.info("Starting task", task_id=task_id)
logger.debug("Loaded items", count=count)
logger.warning("File not found", path=path)
```

### Silent error handling
```python
# Before (silent failure - BAD!)
try:
    result = operation()
except Exception:
    pass  # ❌ Error swallowed

# After (proper logging - GOOD!)
try:
    result = operation()
except Exception as e:
    logger.error("Operation failed", error=str(e))
    # In strict mode, this raises
    # Otherwise, handle appropriately
```

## Examples

### Complete Trial Logging
```python
from tolokaforge.core.logging import init_trial_logger
from tolokaforge.core.output_writer import OutputWriter

class TrialRunner:
    def run(self, verbose=False, strict=False):
        # Initialize logger
        trial_id = f"{self.task_id}:{self.trial_index}"
        self.logger = init_trial_logger(trial_id, verbose, strict)
        
        self.logger.info(
            "Starting trial",
            task_id=self.task_id,
            trial_index=self.trial_index
        )
        
        try:
            # Execute trial
            for turn in range(max_turns):
                self.logger.debug("Processing turn", turn=turn)
                # ... turn logic ...
                
            status = TrialStatus.COMPLETED
            self.logger.info("Trial completed", turns=turn+1)
            
        except TimeoutError:
            status = TrialStatus.TIMEOUT
            self.logger.warning("Trial timeout")
            
        except Exception as e:
            status = TrialStatus.ERROR
            self.logger.error("Trial error", error=str(e))
            if strict:
                raise
        
        # Save logs
        writer = OutputWriter(output_dir)
        writer.write_logs(self.logger)
        
        return trajectory
```

### Module-Level Logging
```python
from tolokaforge.core.logging import get_logger

class GradingEngine:
    def __init__(self):
        self.logger = get_logger("grading")
    
    def grade(self, trajectory):
        self.logger.info("Starting grading", task_id=trajectory.task_id)
        
        # State checks
        self.logger.debug("Checking state")
        state_score = self.check_state()
        
        if state_score < 1.0:
            self.logger.warning("State check failed", score=state_score)
        
        self.logger.info("Grading completed", final_score=score)
        return grade
```

## Configuration

### Change Log Level Programmatically
```python
import logging
from tolokaforge.core.logging import get_logger

# Create logger with custom level
logger = get_logger("module", level=logging.DEBUG, strict=False)
```

### Enable Strict Mode
```python
from tolokaforge.core.logging import get_logger

# Strict mode enabled
logger = get_logger("module", strict=True)

# This will raise
logger.error("Critical error")  # Raises RuntimeError
```

## Analyzing Logs

### Count Errors
```python
import json

with open("logs.json") as f:
    data = json.load(f)

error_count = sum(1 for log in data['logs'] if log['level'] == 'ERROR')
warning_count = sum(1 for log in data['logs'] if log['level'] == 'WARNING')

print(f"Errors: {error_count}, Warnings: {warning_count}")
```

### Find Slow Operations
```python
import json

with open("logs.json") as f:
    data = json.load(f)

slow_ops = [log for log in data['logs'] 
           if log['context'].get('duration_s', 0) > 1.0]

for op in slow_ops:
    print(f"{op['message']}: {op['context']['duration_s']}s")
```

### Debug Tool Failures
```python
import json

with open("logs.json") as f:
    data = json.load(f)

# Find all tool-related errors
tool_errors = [log for log in data['logs'] 
              if log['level'] == 'ERROR' and 'tool' in log['context']]

for error in tool_errors:
    print(f"Tool: {error['context']['tool']}")
    print(f"Error: {error['context']['error']}")
```

## Testing

### Test with Logging
```python
from tolokaforge.core.logging import StructuredLogger, clear_logger_registry
import pytest

@pytest.fixture(autouse=True)
def clear_loggers():
    clear_logger_registry()
    yield
    clear_logger_registry()

def test_my_function():
    logger = StructuredLogger("test")
    
    # Your code that logs
    my_function(logger)
    
    # Verify logs
    logs = logger.get_logs()
    assert len(logs) == 3
    assert logs[0]['level'] == 'INFO'
```

### Test Strict Mode
```python
def test_error_handling():
    logger = StructuredLogger("test", strict=True)
    
    with pytest.raises(RuntimeError, match="STRICT MODE"):
        logger.error("Test error")
```

## Migration Guide

### From print() to logger

1. **Import logger:**
```python
from tolokaforge.core.logging import get_logger
logger = get_logger("module_name")
```

2. **Replace print statements:**
```python
# Before
print(f"Processing task {task_id}")

# After
logger.info("Processing task", task_id=task_id)
```

3. **Add context:**
```python
# Before
print(f"Tool {tool} failed with error: {error}")

# After
logger.error("Tool failed", tool=tool, error=error)
```

4. **Use appropriate levels:**
```python
# Before
print(f"DEBUG: {detail}")
print(f"WARNING: {issue}")

# After
logger.debug("Detail", ...)
logger.warning("Issue", ...)
```

## Summary

- ✅ Use `get_logger()` for module-level logging
- ✅ Use `init_trial_logger()` for trial-specific logging
- ✅ Include context in all log calls
- ✅ Use appropriate log levels
- ✅ Log before raising exceptions
- ✅ Use `--verbose` for debugging
- ✅ Use `--strict` for fail-fast behavior
- ✅ Save logs with `logger.save_to_file()`