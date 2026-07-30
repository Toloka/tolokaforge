# Custom Checks

`custom_checks` is the deterministic-Python grading extension. A task pack
declares a `checks.py` next to `task.yaml`, wires it in via `grading.yaml`,
and the runner executes those checks over the trial's `CheckContext`
(initial/final state + transcript + task metadata). Per-check results and
an aggregate score land on the grade next to `state_checks`,
`transcript_rules`, and `llm_judge`; the weight declared in
`combine.weights.custom_checks` scales that score in the weighted final.
See [GRADING.md](GRADING.md#custom-checks) for how the four components
combine.

Use `custom_checks` for the scoring shape no declarative primitive
expresses — arithmetic over final DB rows, invariants that span multiple
tables, transcript patterns tied to computed values. Reach for
`state_checks.jsonpaths` first (JSONPath equals/contains is enough for
most tasks) and reserve `custom_checks` for the deterministic-Python gap.

> **Live reference:** `examples/native/custom_checks/` ships a runnable
> pack that reconciles a customer balance from a transaction list and
> asserts the arithmetic result — the shape this seam exists for.

## Schema

`custom_checks:` is a sibling of `state_checks:` / `transcript_rules:` /
`llm_judge:` under `grading.yaml`.

```yaml
custom_checks:
  enabled: true             # required; false (default) disables the block
  file: "checks.py"         # relative to task dir (default: "checks.py")
  interface_version: "1.0"  # required to match SUPPORTED_VERSIONS
  timeout_seconds: 30       # per-check wall clock (default: 30.0)
  weight: 1.0               # weight in the aggregate CheckResultSet (default: 1.0)
  fail_on_error: true       # true (default): executor errors ⇒ 0.0; false ⇒ excluded
  relative_imports:         # extra sys.path entries (relative to task dir)
    - "../.."
```

The block is validated into
[`CustomChecksConfig`](../tolokaforge/core/grading/checks_interface.py)
at both `RegisterTrial` and `GradeTrial` time.

## Authoring API — `@init` + `@check`

Import the decorators, `CheckContext`, and the result types from
`tolokaforge.core.grading.checks_interface`. Import the framework helpers
(`find_by_key`, `tool_was_called`, `text_contains_any`, …) from
`tolokaforge.core.grading.checks_helpers`.

```python
from tolokaforge.core.grading.checks_interface import (
    CheckContext, CheckPassed, CheckFailed, CheckSkipped, check, init,
)
from tolokaforge.core.grading.checks_helpers import find_by_key, tool_was_called

_ctx: CheckContext | None = None


@init(interface_version="1.0")
def _load_context(ctx: CheckContext) -> None:
    global _ctx
    _ctx = ctx


@check
def balance_matches_transaction_net() -> CheckPassed | CheckFailed:
    assert _ctx is not None
    customers = _ctx.final_state.data.get("customers", [])
    transactions = _ctx.final_state.data.get("transactions", [])
    customer = find_by_key(customers, "id", "C-1")
    if customer is None:
        return CheckFailed("customer 'C-1' missing")

    net = sum(
        (t["amount"] if t["kind"] == "credit" else -t["amount"])
        for t in transactions
    )
    expected = customer["opening_balance"] + net
    if customer["balance"] == expected:
        return CheckPassed(f"balance {customer['balance']} == {expected}")
    return CheckFailed(
        f"balance {customer['balance']} != {expected}",
        details={"expected": expected, "actual": customer["balance"]},
    )
```

The rules:

- `@init(interface_version="1.0")` marks a single module-level setup
  function. It receives the `CheckContext` once, before any `@check`
  runs, and typically stashes it as module state for the `@check`
  functions to read.
- `@check` marks a zero-argument function that returns one of
  `CheckPassed`, `CheckFailed`, or `CheckSkipped`. The function name
  becomes the `check_name` on the result. Each `@check` is executed
  under the configured `timeout_seconds`.
- `CheckPassed(message, score=1.0, details={})` and
  `CheckFailed(message, score=0.0, details={})` accept a positional
  message and optional score / details dict. `CheckSkipped(message)` is
  excluded from the aggregate score. Scores are clamped `[0, 1]`.
- `SUPPORTED_VERSIONS` lives in
  `tolokaforge.core.grading.checks_interface`. A declared
  `interface_version` outside that set is rejected at `RegisterTrial`
  (see below).

### `CheckContext` — the evidence surface

`CheckContext` is the sole input to `@init`. It carries:

| Field | Shape | Source |
|---|---|---|
| `initial_state` | `EnvironmentState(data=dict)` | Author-declared `initial_state.json_db` at task load. |
| `final_state` | `EnvironmentState(data=dict)` | Runner-side: `db_client.get_state(trial_id)` post-trial. |
| `transcript` | `Transcript(messages=[Message])` | `llm_messages_json` decoded to `Message`/`ToolCall`. |
| `task` | `TaskContext(task_id, name, description, domain, tags)` | `TaskDescription` metadata. |

`ctx.final_state.data` is shaped by the canonical transform in
[`build_check_context`](../tolokaforge/core/grading/checks_helpers.py):
when the runner returns a nested state, an `"agent"` dict wins over a
`"db"` dict, else the flat wire dict is used; a `"filesystem"` key is
merged in when the chosen level lacks one. Both the host `GradingEngine`
and the runner call the same helper, so a check reads identical evidence
from either grading path.

`ctx.effects` is a legacy alias for `ctx.final_state.data`.
`ctx.tool_calls` is `ctx.transcript.all_tool_calls`. `ctx.response` is
the last assistant message content.

## Interface version contract

When `custom_checks.enabled` is `true`, `RegisterTrial` loads `checks.py`
far enough to read the declared `@init(interface_version=…)` and rejects
the trial *before* the agent loop runs if the version is outside
`SUPPORTED_VERSIONS` or the module (or any `relative_imports` target)
fails to load. The rejection error names both the declared version and
the supported set so the pack author sees exactly what to change.

## Network doctrine — checks do not initiate network

Custom checks are pure functions over `CheckContext`: the harness hands
each check state dicts, a transcript, and task metadata, and nothing
else. Do not construct HTTP clients, open sockets, or shell out to
network tools inside a `@check`. The enforcement boundary is the runner
container's `no_internet` NetworkPolicy (ADR-0018 / #581): external
egress is denied at the container level, so a check that tries an
outbound call fails loudly rather than reaching the internet.

Two consequences follow:

- Checks must not depend on external services being reachable. A check
  reading a config from a remote endpoint would fail on the runner even
  when the config is correct — because the boundary denies the request.
- The in-process executor is **not** a per-check sandbox: `timeout` runs
  through a `ThreadPoolExecutor` that cannot kill a runaway thread, and
  checks share the runner container's filesystem + Python process.
  Author checks defensively (no monkey-patching, no shared mutable
  globals across checks). True per-check sandboxing is tracked in
  [#673](https://github.com/tolokasoft1/tolokaforge/issues/673); a
  subprocess-isolated `CheckExecutor` lands as a new implementation of
  the ADR-0012 Protocol without touching any caller.

## Delivery — `checks.py` travels in `tool_artifacts`

`NativeAdapter` bundles `checks.py` (and every `relative_imports` target
directory) into `TaskDescription.tool_artifacts` whenever
`custom_checks.enabled` is true — independent of whether the task also
ships an MCP server. The runner extracts the bundle to the trial's
`artifacts_dir`, adds that directory to `sys.path`, and resolves
`custom_checks.file` against it. Packs work identically with or without
`tools.agent.mcp_server`.

## Grade output

Each `@check` produces one `CustomCheckResult` on the wire:

```
message CustomCheckResult {
  string check_name = 1;   // the @check function name
  string status = 2;       // "passed" | "failed" | "error" | "skipped"
  double score = 3;        // [0, 1]; skipped checks are excluded
  string message = 4;      // human-readable one-liner
  string details_json = 5; // JSON-encoded details dict, "" when empty
}
```

The host parses that list into `Grade.custom_checks_details`
(`core/models.py:373`) alongside the aggregate score on
`Grade.components.custom_checks`. When the top-level executor errors
(module load failure, timeout, executor crash), a sentinel result under
`check_name="__executor__"` preserves the audit and the aggregate score
follows `fail_on_error`: `0.0` when true, excluded from the combine when
false.

## Choosing a test tier

- **Unit** (`tests/unit/grading/`) — pin the *check logic itself*
  against a hand-built `CheckContext` and the in-process `CheckRunner`
  (see `test_custom_checks_runner.py`). This is where per-check
  arithmetic lives; the executor runs in-process, no runner needed.
- **Canonical** (`tests/canonical/`) — pin the *seam*
  (`test_check_executor_contract.py` pins the `CheckExecutor` Protocol
  boundary + `InMemoryCheckExecutor` semantics per ADR-0012).
- **Integration** (`tests/integration/`, `@pytest.mark.requires_docker`)
  — pin the *wire*
  (`test_custom_checks_e2e.py` drives the reference pack through the
  Docker runner and asserts per-check `CustomCheckResult`s, aggregate
  score, and the applied weight — the acceptance criterion for the
  seam).

`checks.py` itself belongs to the *task pack*, not the test suite; the
unit tier is for the framework, not the pack's grader.

## See also

- [`examples/native/custom_checks/`](../examples/native/custom_checks/)
  — runnable reference pack (ledger reconciliation).
- [ADR-0012 — the `CheckExecutor` Protocol seam](adr/0012-custom-checks-extension.md).
- [ADR-0018 — network policy (`no_internet` invariant)](adr/0018-network-policy.md).
- [GRADING.md — the four-component grade](GRADING.md#custom-checks).
