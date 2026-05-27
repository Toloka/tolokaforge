# Golden Trials

Golden trials are reference runs used to validate deterministic grading and infrastructure stability.

## When to Use

- Regression testing after changes to tools or grading
- Verifying that a task still passes with known-good behavior

## Typical Workflow

1. Run a task with a known model/config.
2. Save the resulting trajectory and final state under `golden_trials/`.
3. Compare future runs against the golden outputs.

See `docs/GRADING.md` for hash-based grading and `docs/OUTPUT_FORMAT.md` for output structure.
