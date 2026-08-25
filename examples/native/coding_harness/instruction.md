# Task: Fix the Factorial Bug

The Python module `/work/factorial.py` implements a `factorial` function that
is incorrect for most inputs. Investigate the code and fix the bug so the
test suite in `/work/tests/` passes.

## Environment

- Python 3.11
- `pytest` is installed

## Files

- `/work/factorial.py` — implementation to fix
- `/work/tests/test_factorial.py` — test suite that must pass

## How to verify

Run the tests inside the container:

```bash
pytest /work/tests -v
```

The pack's verifier writes the pass fraction to `/logs/verifier/reward.txt`;
a run that fixes every test scores `1.0`.
