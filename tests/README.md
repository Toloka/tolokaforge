# Tolokaforge Test Suite

## Overview

The test suite is organized into **3 categories**: unit, canonical, and integration.

| Category | Directory | Speed | External deps | Marker |
|----------|-----------|-------|---------------|--------|
| Unit | `tests/unit/` | Fast (< 1s each) | None | `@pytest.mark.unit` |
| Canonical | `tests/canonical/` | Fast (< 5s each), except the packaging/entry-point smoke tests that build a wheel and install it into a scratch venv (~10–25s) | Golden snapshots; the packaging/entry-point smoke tests also require the `uv` CLI (they skip loud without it) | `@pytest.mark.canonical` |
| Integration | `tests/integration/` | Slow (5-60s each) | Docker, API keys | `@pytest.mark.integration` |

Current baseline: run the lane you care about (`mcp__dev__run_tests marker=unit`,
`marker=canonical`) — the counts move with every merge, so they are not written down
here.

## Running Tests

Unit and canonical tests run without API keys or Docker:

```bash
# All non-integration tests
uv run pytest tests/unit/ tests/canonical/ -v

# Unit tests only
uv run pytest tests/ -v -m unit

# Canonical tests only
uv run pytest tests/ -v -m canonical

# Regenerate golden snapshots
uv run pytest tests/canonical/ --update-canon -v
```

Integration tests need `.env` variables (API keys, service URLs) — use `scripts/with_env.sh`. They run in parallel with `-n auto` (pytest-xdist):

```bash
# Integration tests (needs Docker + API keys in .env)
scripts/with_env.sh uv run pytest tests/ -v -m integration -n auto

# Full suite
scripts/with_env.sh uv run pytest tests/ -v
```

Under `-n auto`, `tests/integration/reset_recipes/conftest.py` assigns each xdist worker a unique `COMPOSE_PROJECT_NAME` for the reset-recipe suite, whose stacks all share the `compose` basename and would otherwise collide across workers. The rest of the integration suite derives per-test project names from slug-encoded `make_project_temp_dir` basenames, so it stays in disjoint namespaces without the env pin.

### Regenerating the well-formed judge payload fixture

`tests/unit/grading/data/wellformed_submit_report.json` is a real judge's
well-formed `submit_report` payload (markers matching their verdicts). The unit
test `tests/unit/grading/test_rubric.py::TestWellFormedLivePayload` re-validates
it with no spend. To recapture it from a live judge run (real provider, small
spend), set `TF_CAPTURE_JUDGE_PAYLOAD=1` — the mid-tier (gpt-4.1-mini) case of
the marker acceptance test writes the fixture:

```bash
TF_CAPTURE_JUDGE_PAYLOAD=1 scripts/with_env.sh uv run pytest \
  tests/integration/test_rubric_judge_live.py::test_rubric_judge_live_markers_match_verdicts \
  -m integration
```

The rejection fixtures alongside it (`ae_bdg_*_submit_report.json`) are **frozen
historical captures** — see `tests/unit/grading/data/README.md`; never
regenerate those.

### Live rubric-judge retry recovery

`tests/integration/test_rubric_judge_live.py::test_rubric_judge_live_recovers_through_forced_retry`
drives a real OpenAI-family judge through the `submit_report` retry path. The
model's tool call is real on both turns; the test forces exactly one validation
rejection (by patching `parse_submit_report` as bound in the judge module) so the
retry fires deterministically, then asserts the live provider accepts the
repaired tool-call/tool-result sequence (COMPLETED, no 400). Key-gated on
`OPENROUTER_API_KEY` / `OPENAI_API_KEY`; skips without a key.

## Directory Structure

```
tests/
├── conftest.py              # Shared fixtures, auto-skip requires_api hook
├── unit/                    # Pure-logic tests, no I/O
│   ├── grading/             # Grading subsystem tests
│   └── adapters/            # Adapter unit tests
├── canonical/               # Golden snapshot + packaging/discovery smoke tests
│   ├── conftest.py          # --update-canon flag, canon_snapshot + built_wheel session fixtures
│   ├── test_adapter_convert_packaging.py    # wheel-inspection packaging smoke
│   ├── test_adapter_convert_entry_point.py  # entry-point discovery smoke (scratch-venv install)
│   ├── fixtures/            # tolokaforge-adapter-demo: demo conversion adapter installed into a scratch venv
│   └── snapshots/           # Committed golden JSON files
├── integration/             # Docker/API integration tests
│   └── docker/              # Docker foundation layer tests
├── data/                    # Test data
│   ├── tasks/               # Task fixtures (calc_basic, browser_basic, calc_custom_checks)
│   ├── grading_parity/      # Substrate-parity packs; own glob, outside tasks/**
│   ├── transcript_parity/   # transcript_rules differential packs; own glob, may author two keys
│   ├── projects/            # Full project snapshots (food_delivery_2, tau_retail_mini)
│   ├── migration_corpora/   # Judge-labelled trial bundles reconcile reads (notes_duplicate_check)
│   ├── migration_packs/     # Migration declarations reconcile resolves; shipped task_ids, never a default root
│   └── configs/             # Config fixtures
└── utils/                   # Shared test utilities
    ├── fixtures.py           # Common fixtures (mock_env_state, test_task_path, etc.)
    ├── validators.py         # Output validation helpers
    ├── mock_clients.py       # MockAsyncClient — canonical source
    ├── networks.py           # Docker network/volume fixtures
    ├── containers.py         # Docker container fixtures
    ├── docker_helpers.py     # Compose/daemon helpers for the Docker tiers
    ├── recorded_calls.py     # RecordedToolCall builders
    ├── runner_requests.py    # gRPC request + TaskDescription builders
    ├── servicer_runtime.py   # RuntimeBackend over the in-process servicer + the duplicate-call_id refusal
    ├── timelines.py          # Coherent TrialTimeline fixtures (message view + records)
    ├── trace_constraints.py  # One trace constraint evaluated, for single-verdict assertions
    ├── trace_checks_configs.py  # One authored trace_checks block spanning the whole vocabulary
    ├── trace_overrides.py    # A supplied constraint block, written to a file and loaded back
    ├── migration_packs.py    # A task directory a migration declaration is read out of
    ├── combine_method_verdicts.py  # The combine.method answer table both tiers hold
    ├── wire_grades.py        # A wire Grade driven through the real gRPC client lowering
    └── project_fixtures.py   # food_delivery_2 project data loaders
```

## Test Categories

### Unit Tests (`tests/unit/`)

Pure logic tests with no external dependencies. Mock everything.

- Grading checks: hash computation, JSONPath assertions, transcript rules
- Grading key ledger (`grading/test_grading_ledger.py`) — a failure means either a
  populated scored `grading.yaml` key reaches `GradeTrial` with no evaluator and no
  recorded skip, or the ledger rejects a config that grades correctly
- Tool registry: schema conversion, tool invocation
- Adapter output: task bundle generation, conversion logic
- CLI commands: status, validation paths

### Canonical Tests (`tests/canonical/`)

Compare output against committed golden snapshots in `snapshots/`.

- Adapter conversion output
- Grading pipeline results
- Custom checks with real project data (food_delivery_2)
- Golden-set hash grading verification
- Grading substrate parity (`test_grading_substrate_parity.py`) — a failure means a
  `grading.yaml` key is unaccounted for, claims a substrate that does not evaluate
  it, no longer survives adapter translation, or names a `runner_field` the runtime
  ledger cannot resolve. Fix the manifest entry in
  `tolokaforge/core/grading/key_manifest.py` or the drift it exposed; widening a
  frozen exemption set in the test module is the deliberate last resort.
  A **lock 15** failure is narrower: one ledger key's recording site was deleted,
  downgraded to a skip, filed `EVALUATED` for an evaluation that did not run, or its
  driver stopped populating the key. The sweep covers **every** ledger key naming a
  runner field — the hash family, the db probes and the judge included, not a subset
  — so the failing row names the key. Fix the evaluate-or-skip site in
  `_grade_trial_async` (or the evaluator it calls) rather than the assertion. A key
  missing from the lock's driver table fails
  `test_every_ledger_key_names_a_driver_that_can_populate_it` instead, which means a
  new ledger key arrived with no way to drive it: add a driver, never drop the row.
- Transcript rules substrate parity (`test_transcript_substrate_parity.py`) — one
  authored pack under `tests/data/transcript_parity/`, one trial, graded through both
  substrates' real adapter paths, must produce the same `transcript_rules` component
  and the value the row pins. The two implementations disagree today, so seventeen
  rows carry `xfail(strict=True, raises=AssertionError)`: `strict` fails the suite on a
  row that starts passing, and `raises` stops a row whose pack merely failed to load
  from reporting as the expected divergence. Two unmarked **anchor** rows, at two
  different scores, are the harness's own proof. A row pinning a value the runner does
  not produce raises `_FixtureDefect`, which no marker absorbs — that is the
  table being wrong, not the substrates disagreeing. Fix the divergence in
  `tolokaforge/core/grading/transcript.py`, never the pinned column.
- Trace timeline substrate parity (`test_trace_timeline_substrate_parity.py`) — one
  scripted tool-call sequence driven through each substrate's real recording path
  must build the same events. A failure means one substrate's recording drifted,
  so a trace check would mean different things depending on which substrate graded
  the trial. Both substrates then *grade* off that timeline, so a failure here also
  means both substrates' transcript rules are reading a trial the two views no
  longer agree on. Build a coherent message-view/record pair for a grading fixture
  with `tests/utils/timelines.py`; a record naming a call no message asked for is a
  reconciliation failure, not a shortcut. `build_timeline` lands every call on the
  last assistant turn, while `build_turn_timeline` takes the calls per turn — which
  is what an ordering or turn-window property needs.
- Schema version stamps documented (`test_schema_version_stamps_documented.py`) — the
  stamps `docs/OUTPUT_FORMAT.md` § Schema Version Stamps publishes, both the table's
  rows and the bare `schema_version: N` literals the prose repeats, must equal the
  constants that write them. Every other stamp test compares a stamp against its own
  constant, so a bump nobody documented reds nothing; the table is the second source. A
  failure means the constants moved and the docs table follows — never the other way
  round.
- Gate semantics parity (`test_gate_semantics_parity.py`) — the judge's required
  criterion and a trace check's `severity: gate` are one gate scored by two
  implementations, driven against one shared answer table. A failure names the cell
  where they disagree. No shared helper can replace it: the trace fold's weighted
  fraction carries one division and no branch, while the judge's must raise on a
  non-positive denominator. The same file holds `docs/GRADING.md`'s two gate sections
  to cross-referencing each other, so neither spelling can be documented alone.

Every substrate-parity pack lives in `tests/data/grading_parity/<task_id>/` and
authors a `task.yaml` and a `grading.yaml`. A pack that drives a differential adds
a `trial.yaml` holding one named case per trial it grades — conventionally
`satisfying` and `violating` — in the one shape its loader reads:

```yaml
satisfying:
  messages:
    - { role: user, content: "Refund PAY-1 if it is a duplicate." }
    - role: assistant
      content: "Looking that up."
      tool_calls:
        - tool_name: billing_api_get_payment
          executor: agent
          status: success
          arguments: { payment_id: "PAY-1" }
          output: '{"amount": 10}'
  state: {}
```

A tool call belongs to the message that requested it, so a case places its calls
across the turns that made them and the timeline's `turn_index` follows what the
author wrote. `output` is that call's own result text and defaults to `""`. Call
ids and `sequence` are assigned in document order, and `latency_seconds` is not
authorable — wall time is not compared across substrates, so a pinned value would
be one nothing reads. Any other key fails the load naming itself, because a
fixture key the loader ignores expresses less than its author wrote.

A pack whose key reads DB state declares its rows in `task.yaml`'s `initial_state`,
not only in the case's `state:` — the runner provisions a trial's DB service from
`initial_state`, and lock 15 grades every pack through `RegisterTrial`.

The pack directory is the author key with its dots replaced by underscores, so a
leaf key inside a list field gets its own pack:
`trace_checks.constraints.absent_before` is
`tests/data/grading_parity/trace_checks_constraints_absent_before/`. A pack under
test for one constraint kind authors **that kind alone** at the top level — a
second constraint beside it is a second check the violating trial could be
discriminated by — while the sub-terms of a composite kind belong inside its own
expression, where they are part of the constraint under test rather than next to
it. Write the two trials so that a build ignoring the thing under test would score
them *identically*: that is what makes discrimination evidence for the key rather
than for the pack.

`tests/data/transcript_parity/` is the second parity root, read only by
`test_transcript_substrate_parity.py` through its own glob. Its packs author a
`task.yaml` and a `grading.yaml` and **no `trial.yaml`**: each trial's messages and
its `tool_log` live in the module's table beside the score the row pins, because
whether the trial kept a record of the calls it declared is one of the things the
two substrates read differently, and a fixture loader deriving records from the
message view could not express its absence. A pack here may author **two** author
keys — a veto is only distinguishable from a fraction with a second rule beside it —
which is why these packs cannot live under `grading_parity`, whose one-key rule is
what keeps a violating trial attributable to the key under test.

Use `--update-canon` flag to regenerate snapshots after intentional changes.

### Integration Tests (`tests/integration/`)

Require Docker daemon, API keys, or both. Auto-skipped when prerequisites are missing.

- Docker container lifecycle and service health
- End-to-end runner pipeline (tau, tlk_mcp_core, native)
- LLM-judged grading with real providers
- Security: container isolation, network segmentation

A test that exercises a runner-side wire field needs `make docker-build-core`
first, because the field ships inside the image. Only a *missing* image skips: a
**stale** `tolokaforge-runner:latest` fails at `RegisterTrial` with a pydantic
extra-forbid error naming the field (the runner config models are
`extra="forbid"`), which points at the test rather than at the image. The builder
tags by content hash and does **not** move `:latest`, which is the tag the
testcontainer fixtures pin, so retag after building:
`docker tag <fresh-ref> tolokaforge-runner:latest` (#740).

Some suites in this tier are the `enforcing_test` a grading-key manifest entry
names — `test_docker_grading_hash_composition.py` for the `state_checks.hash`
family, `test_helpdesk_workflow_end_to_end.py` for `state_checks.db_probes`,
`test_rubric_judge_live.py` for `llm_judge`. `test_grading_substrate_parity.py`
resolves each nodeid without importing it, so renaming one of those test functions
fails the canonical tier naming the manifest entry.

Two members of this tier are **Docker-free and keyless** (they need only the `uv`
CLI): `test_plugin_discovery.py` and `test_external_harness_e2e.py`. Both install
the out-of-tree `tests/fixtures/tolokaforge_plugin_fixture` package into an
isolated `--target` (never the dev venv) and drive it in fresh subprocesses.

`test_external_harness_e2e.py` is the runtime-independence capstone: a single
downstream plug-in registers a runtime backend, grader, and conductor, and both
`tolokaforge.runner.run_trial` and the `tolokaforge run-trial` subprocess resolve
all three seams from installed metadata.
`test_run_trial_cli_over_downstream_plugins` compares the subprocess's `result`
wire line to a checked-in golden at
`tests/data/run_trial_capstone_golden.jsonl`. The fixture trajectory carries no
volatile field (`messages=[]`, pinned `start_ts`/`end_ts`, default `Metrics`), so
the golden is byte-stable with **no mask**. Regenerate it after an intentional
wire/fixture change with:

```bash
TOLOKAFORGE_REGENERATE_GOLDEN=1 scripts/with_env.sh uv run pytest \
  tests/integration/test_external_harness_e2e.py::test_run_trial_cli_over_downstream_plugins
```

If two regenerations differ, a volatile field leaked — pin it in the fixture, do
not add a mask.

## Pytest Markers

Defined in `pyproject.toml` under `[tool.pytest.ini_options]`:

| Marker | Description |
|--------|-------------|
| `unit` | Fast, isolated unit tests |
| `integration` | Tests requiring external services |
| `canonical` | Canonization snapshot tests |
| `slow` | Tests taking > 5 seconds |
| `requires_api` | Needs LLM API key — auto-skipped if none set |
| `requires_docker` | Needs Docker daemon |
| `docker` | Real container tests |
| `requires_postgres` | Needs Postgres instance |
| `grading` | Grading system tests |
| `security` | Security validation tests |
| `performance` | Performance benchmarks |
| `llm` | Calls real LLM providers |

All markers are enforced via `--strict-markers`.

## Key Fixtures

| Fixture | Source | Used by |
|---------|--------|---------|
| `mock_env_state` | `utils/fixtures.py` | Unit tests for user tools |
| `test_task_path` | `utils/fixtures.py` | Integration Docker service tests |
| `temp_output_dir` | `utils/fixtures.py` | Integration Docker service tests |
| `canon_snapshot` | `canonical/conftest.py` | All canonical tests |
| `food_delivery_2_*` | `canonical/conftest.py` | Canonical golden-set tests |
| `json_db_container` | `utils/containers.py` | Integration security tests |
| `runner_container` | `utils/containers.py` | Integration security tests |

## Writing New Tests

1. **Choose the right category**: unit for logic, canonical for regression snapshots, integration for real services.
2. **Naming**: `test_<component>_<behavior>.py` for files, `test_<action>` for methods.
3. **Use shared fixtures** from `conftest.py` — don't duplicate.
4. **Add markers**: every test file should use the appropriate `@pytest.mark.*`.
5. **Import `MockAsyncClient`** from `tests.utils.mock_clients` for new tests (don't create local copies).

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `food_delivery_2` tests skip | Run `git lfs pull` to fetch project data |
| Integration tests skip | Set API keys in `.env`, ensure Docker is running |
| `--strict-markers` error | Add new markers to `pyproject.toml` |
| Snapshot mismatch | Re-run with `--update-canon` if change is intentional |

## Test Philosophy

- **Zero `xfail`**: every test either passes or gets deleted.
- **Zero bare `@skip`**: use conditional markers (`requires_api`, `requires_docker`).
- **Canonical golden data** for regression detection — diffs are reviewable in PRs.
- **Auto-skip** for missing prerequisites instead of hard failures.
