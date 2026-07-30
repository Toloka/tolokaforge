# Custom checks example — ledger reconciliation

Reference pack that exercises the `custom_checks` grading extension:
`grading.yaml` declares `custom_checks.enabled: true` and a `checks.py` next
to the task. The runner runs those checks over the trial's final state +
transcript and folds a `custom_checks` component into the weighted grade
alongside `state_checks`. See [docs/custom_checks.md](../../../docs/custom_checks.md).

The task's grader computes the customer balance from the transaction list
(sum of credits minus sum of debits) and asserts the final DB balance
matches — arithmetic scoring on state + transcript that no declarative
`state_checks` primitive expresses. This is the deterministic-Python gap
the `custom_checks` seam exists for.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/custom_checks/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/custom_checks/run_config.yaml
```

## Tasks included

- `dataset/tasks/reconcile_ledger/` — reconcile customer `C-1`'s balance
  against a five-transaction list. `checks.py` verifies
  `balance == sum(credits) - sum(debits)` and that the agent's transcript
  enumerates every credit-transaction id.
