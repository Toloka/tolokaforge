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
