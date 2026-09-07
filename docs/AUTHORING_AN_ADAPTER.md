# Authoring a TolokaForge Adapter

An **adapter** is a task loader that translates a source format the engine does
not read natively (a tau-bench pack, a terminal-bench directory, a proprietary
JSON layout) into the `TaskConfig` / `AdapterEnvironment` shape the engine
orchestrates against. You write one when you want to run TolokaForge against a
benchmark whose on-disk layout is not `task.yaml` + `grading.yaml`. A
**third-party adapter** is a separate pip-installable Python package that
depends on `tolokaforge` and registers itself through the
`tolokaforge.adapters` entry-point group; the engine discovers it at import
time and everything downstream of `get_adapter(<name>)` sees a first-class
citizen.

This document is the ordered top-of-funnel walkthrough. Follow the eight steps
in order and you end with a registered, tested, CI-covered adapter package.
Each step names the identifier a reader would grep for and links out to the
deep-dive doc for the concern the step raises. Do not duplicate deep-dive
content back into this doc; open the linked section instead.

## The eight-step checklist

### 1. Scaffold the package

A minimum `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tolokaforge-adapter-my-benchmark"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["tolokaforge"]

[project.entry-points."tolokaforge.adapters"]
my_benchmark = "tolokaforge_adapter_my_benchmark.adapter:MyBenchmarkAdapter"

[tool.hatch.build.targets.wheel]
packages = ["src/tolokaforge_adapter_my_benchmark"]
```

The shape mirrors the shipped
[`external_adapters/tolokaforge-adapter-terminal-bench/pyproject.toml`](../external_adapters/tolokaforge-adapter-terminal-bench/pyproject.toml).
`tolokaforge` is a hard dependency: the entry-point cannot load otherwise, and
step 6's reusable test suite is imported from it.

### 2. Subclass `BaseAdapter`

Implement the ten required methods (`get_task_ids`, `get_task`,
`get_task_dir`, `create_environment`, `get_tools`, `get_registry_tools`,
`get_system_prompt`, `get_grading_config`, `reset_environment`,
`compute_golden_hash`) — deep signatures live in
[`docs/ADAPTER_INTERFACE.md § Required Methods`](ADAPTER_INTERFACE.md#required-methods),
items 1 – 10.

Then consider four **optional overrides** that live on `BaseAdapter` and are
**not** members of `AdapterGradingContract` (step 4 covers the Protocol
methods; these are addressed here because they ride on the base class):

- `convert_to_native(task_id) -> NativeTaskBundle` (item 11) — emit a native
  `task.yaml` + `grading.yaml` bundle for disk serialisation. Default raises
  `NotImplementedError`; override only when downstream tooling reads the
  native bundle.
- `grading_combine_layer() -> CombineLayer` (item 12) — what your projects
  supply beneath a task's own `combine` block. Default:
  `CombineLayer.unresolvable()`.
- `grading_hash_source_layer(task, task_dir) -> HashSourceLayer` (item 13) —
  a **classmethod**. What you supply beneath a task's authored
  `state_checks.hash` block. Default: `HashSourceLayer.unresolvable()`.
- `fingerprint() -> dict[str, Any] | None` (item 17) — the payload the engine
  writes under `adapter_fingerprints[<adapter type>]` on
  `engine_run_state.json`. Default: `None`.

If the tasks stand up a compose stack, also override
`docker_stack_requirements() -> DockerStackRequirements` — see
[`docs/ADAPTER_INTERFACE.md § Docker stack requirements`](ADAPTER_INTERFACE.md#docker-stack-requirements).

If the adapter drives a coding-harness CLI (`claude-code`, `codex`,
`kimi-code`, …), mix in
[`CodingHarnessAdapterMixin`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/adapter_support.py)
alongside `BaseAdapter`. The mixin flips `supports_coding_harness = True`
(the gate the orchestrator's config-validation reads) and ships seven helpers
covering registry resolution, command assembly, the metadata handshake, the
bash tool schema payload, the `test_execution` grading payload, the
standalone install-script Dockerfile layer, and — load-bearing on step 6 —
the instance-aware `preferred_grader_kind()` default. That default is exactly
what step 6's `expected_preferred_grader_kind` fixture locks against under an
active harness. See
[`tolokaforge_coding_harnesses/README.md § Adopting the mixin`](../tolokaforge_coding_harnesses/README.md#adopting-the-mixin).

### 3. Declare capability flags

Three `ClassVar[bool]` slots on the class body sit atop `BaseAdapter`'s
`False` default. Flip only what applies:

```python
class MyBenchmarkAdapter(BaseAdapter):
    requires_docker_cli_in_runner: ClassVar[bool] = False
    grades_from_task_grading_file: ClassVar[bool] = False
    syncs_adapter_env_to_state: ClassVar[bool] = False
```

- `requires_docker_cli_in_runner` — flip to `True` when grading runs commands
  against Docker daemons (build/exec); the harness reads the flag to decide
  whether the runner stack must carry a Docker socket bind.
- `grades_from_task_grading_file` — flip to `True` when the adapter grades
  from a `grading:` block a task names on disk. Native-shaped adapters flip
  this; adapters that synthesise grading config from their own fixtures leave
  it `False`. The `grading:` presence gate reads the flag to decide whether
  an absent block is refused or an unchecked pronouncement.
- `syncs_adapter_env_to_state` — flip to `True` when the conductor should
  sync `AdapterEnvironment` into runner state (Tau-family); adapters whose
  runner owns state end-to-end leave it `False`.

Deep contract:
[`docs/ADAPTER_INTERFACE.md § Capability flags`](ADAPTER_INTERFACE.md#capability-flags).

### 4. Override the six `AdapterGradingContract` method slots

The Protocol at
[`tolokaforge.adapters.AdapterGradingContract`](../tolokaforge/adapters/grading_contract.py)
declares exactly six methods, mapped to the numbered list in
[`docs/ADAPTER_INTERFACE.md § Optional Methods`](ADAPTER_INTERFACE.md#optional-methods):

- `grading_tool_inventory(task, task_dir) -> ToolInventory` (item 14) — the
  tool set this adapter presents at runtime. Default:
  `ToolInventory.unresolvable()`.
- `grading_replay_world(task, task_dir) -> ReplayWorld` (item 15) — the
  initial-state + `mcp_server` a golden-action replay executes against.
  Default: `ReplayWorld.unresolvable()`.
- `grading_seeded_tables(task, task_dir) -> SeededTablesLayer` (item 16) —
  the tables this adapter seeds, keyed by `state_checks.id_fields`. Default:
  `SeededTablesLayer.unresolvable()`.
- `grading_source(task, task_dir) -> GradingSource` (item 18) — the grading
  block one run of *task* reads, resolved against this adapter. Default:
  `GradingSourceKind.UNINTERROGABLE` with `path=None` and a non-empty
  reason.
- `emit_runner_grading_payload(task_id) -> dict[str, Any]` (item 19) — the
  payload this adapter emits for the runner's `RunnerGradingConfig`
  construction. Default: `{}`.
- `preferred_grader_kind() -> str` (item 20) — the registered
  `tolokaforge.grader_kinds` key this adapter prefers. Default:
  `"composite"`.

Override only the slots whose default your adapter diverges from. The
Protocol is `runtime_checkable`, so `isinstance(adapter, AdapterGradingContract)`
holds structurally for any subclass of `BaseAdapter`.

**Explicit exclusion.** Item 13 (`grading_hash_source_layer`) is **not** a
member of `AdapterGradingContract` — it is a classmethod default on
`BaseAdapter`. Step 2 above is where you override it. This is the item-range
correction the sibling
[`docs/ADAPTER_INTERFACE.md § AdapterGradingContract`](ADAPTER_INTERFACE.md#adaptergradingcontract)
section names.

Under an active coding harness, `preferred_grader_kind()` alignment with the
`grading_method` `emit_test_execution_grading()` puts on the emitted
`RunnerGradingConfig` is the load-bearing invariant. See
[`docs/CODING_HARNESSES.md § Grader-kind alignment`](CODING_HARNESSES.md#grader-kind-alignment).

### 5. Register the entry-point

The `[project.entry-points."tolokaforge.adapters"]` block from step 1 is what
makes the engine discover the class. The registered name (`my_benchmark`
above) is the string a run config's `harness_adapter.type` reads:

```yaml
evaluation:
  harness_adapter:
    type: "my_benchmark"
```

Duplicate-name (two installed distributions registering the same key) and
broken-import (an entry-point whose target module raises on load) fail-loud
semantics live in
[`docs/ADAPTER_ARCHITECTURE.md § Fail-loud registry pattern`](ADAPTER_ARCHITECTURE.md#fail-loud-registry-pattern).
Both are handled by the shared
`tolokaforge.core.plugin_registry.discover_entry_points` primitive; the
adapter package does not implement its own scan.

### 6. Pin conformance with `AdapterGradingContractSuite`

Subclass
[`AdapterGradingContractSuite`](../tolokaforge/testing/adapters/grading_contract.py)
from `tolokaforge.testing.adapters` and hand it two fixtures. The subclass
runs 13 test methods against those fixtures — the six
`AdapterGradingContract` methods from step 4, the three capability flags
from step 3, three cross-cutting invariants (`grading_source` classmethod-
dispatch parity, emit-payload schema, preferred-kind registry resolution),
and one alignment invariant (`preferred_grader_kind()` equals the kind
`emit_test_execution_grading()` puts on `RunnerGradingConfig` under an
active harness).

```python
import pytest
from tolokaforge.core.models import TaskConfig
from tolokaforge.testing.adapters import AdapterGradingContractSuite

from tolokaforge_adapter_my_benchmark.adapter import MyBenchmarkAdapter


class TestMyBenchmarkAdapterGradingContract(AdapterGradingContractSuite):
    expected_requires_docker_cli_in_runner = True
    expected_preferred_grader_kind = "test_execution"

    @pytest.fixture
    def adapter(self) -> MyBenchmarkAdapter:
        return MyBenchmarkAdapter({"base_dir": ".", "tasks_glob": "tasks/*"})

    @pytest.fixture
    def task_and_dir(self, adapter: MyBenchmarkAdapter) -> tuple[TaskConfig, Path]:
        task_id = adapter.get_task_ids()[0]
        return adapter.get_task(task_id), adapter.get_task_dir(task_id)
```

Override the four `expected_*` class attributes only when your adapter's
declaration diverges from the shipped defaults:

- `expected_requires_docker_cli_in_runner: bool = False`
- `expected_grades_from_task_grading_file: bool = False`
- `expected_syncs_adapter_env_to_state: bool = False`
- `expected_preferred_grader_kind: str = "composite"`

When step 2 mixed in `CodingHarnessAdapterMixin`, the mixin's instance-aware
`preferred_grader_kind()` default is exactly what
`expected_preferred_grader_kind` locks against — set it to
`"test_execution"` on adapters whose harness-mode grading writes a reward
file, matching the mixin's return under `self.agent_harness != ENGINE_LOOP`.

Deep contract:
[`docs/ADAPTER_INTERFACE.md § AdapterGradingContractSuite`](ADAPTER_INTERFACE.md#adaptergradingcontractsuite-reusable-pytest-suite).
Worked example:
[`external_adapters/tolokaforge-adapter-terminal-bench/tests/test_terminal_bench_grading_contract.py`](../external_adapters/tolokaforge-adapter-terminal-bench/tests/test_terminal_bench_grading_contract.py).

### 7. Install and run tests locally

Install the adapter into a venv that already has `tolokaforge` and run the
test suite:

```bash
pip install -e .
pytest tests/
```

The engine dependency in `pyproject.toml` (declared in step 1) is what
lets the entry-point resolve and the suite import land. If the suite raises
`ModuleNotFoundError: tolokaforge`, the venv is missing the engine and the
entry-point cannot have been discovered either.

### 8. Wire CI

The adapter package's CI runs the same command as step 7:

```yaml
- name: Test adapter
  run: pytest tests/
```

Because the suite subclass ships inside the adapter repository, an upstream
engine change that breaks any of the 13 pinned invariants (a new required
`AdapterGradingContract` slot, a tightened capability-flag semantics, a
grader-kind registration rename) fails the adapter's CI on the next
`tolokaforge` dependency bump — not on the next real run. Pin the engine
loosely (`tolokaforge>=X`) in `pyproject.toml` if you want to opt into fast
drift signal, tightly (`tolokaforge==X.Y.Z`) if you would rather age the
adapter alongside a specific engine cut.

## What lives elsewhere

| Concern | Deep-dive doc |
| --- | --- |
| Method contract (all 20 items, capability flags, Protocol, suite) | [docs/ADAPTER_INTERFACE.md](ADAPTER_INTERFACE.md) |
| Architecture, entry-point discovery, fail-loud registry, `uv sync --extra <name>` install extras | [docs/ADAPTER_ARCHITECTURE.md](ADAPTER_ARCHITECTURE.md) |
| Coding-harness capability (`CodingHarnessAdapterMixin`) | [docs/CODING_HARNESSES.md](CODING_HARNESSES.md), [tolokaforge_coding_harnesses/README.md § Adopting the mixin](../tolokaforge_coding_harnesses/README.md#adopting-the-mixin) |
| Working out-of-tree adopter | [external_adapters/tolokaforge-adapter-terminal-bench/](../external_adapters/tolokaforge-adapter-terminal-bench/) |
| Native-adapter deep reference | [docs/NATIVE_ADAPTER.md](NATIVE_ADAPTER.md) |

## Reference implementations

- **`NativeAdapter`** — [`tolokaforge/adapters/native.py`](../tolokaforge/adapters/native.py).
  Built-in, discovered before the entry-point scan, and the shipped adopter
  of `CodingHarnessAdapterMixin`. Read it for the canonical shape of every
  method slot.
- **`TerminalBenchAdapter`** —
  [`external_adapters/tolokaforge-adapter-terminal-bench/`](../external_adapters/tolokaforge-adapter-terminal-bench/).
  The shipped out-of-tree adopter. Its
  [`pyproject.toml`](../external_adapters/tolokaforge-adapter-terminal-bench/pyproject.toml),
  [`AdapterGradingContractSuite` subclass](../external_adapters/tolokaforge-adapter-terminal-bench/tests/test_terminal_bench_grading_contract.py),
  and
  [README](../external_adapters/tolokaforge-adapter-terminal-bench/README.md)
  are the worked example this checklist is calibrated against.
- **`TauAdapter`** / **`TlkMcpCoreAdapter`** — plugin-package adopters
  shipped alongside the engine. See
  [`docs/ADAPTER_ARCHITECTURE.md § Adapter-Specific Details`](ADAPTER_ARCHITECTURE.md#adapter-specific-details).
