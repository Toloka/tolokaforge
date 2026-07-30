# Custom Checks

Tolokaforge supports Python-based custom checks for grading beyond JSONPath and transcript rules.

## Usage

Add a `checks.py` file in your task directory and reference it in `grading.yaml`:

```yaml
custom_checks:
  enabled: true
  checks_file: "checks.py"
  timeout_s: 30
```

Inside `checks.py`, implement functions that receive a `CheckContext` and return `CheckResult` objects.

See `tolokaforge/core/grading/checks_interface.py` for the API.

## Interface version contract

`checks.py` declares its authoring interface via the `@init` decorator:

```python
@init(interface_version="1.0")
def setup(ctx):
    ...
```

The runner validates this at **trial registration**, not at trial end. When a
pack sets `custom_checks.enabled: true`, `RegisterTrial` loads `checks.py` far
enough to read the declared `interface_version` — an unsupported version (or a
module that fails to import) rejects the trial immediately, before the agent
loop runs. The rejection error names both the declared version and the runner's
`SUPPORTED_VERSIONS` set so the pack author sees exactly what to change.

Supported versions live in `tolokaforge.core.grading.checks_interface.SUPPORTED_VERSIONS`.
